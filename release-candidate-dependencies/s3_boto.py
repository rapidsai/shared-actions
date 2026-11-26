# Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.

"""The candidate store in-process with boto3, for the reuse plan.

``row_reuse.S3`` starts the AWS CLI for every call. The plan makes about a dozen calls per row, and the process start
was most of each call: rows took 14 s each four at a time and 31-34 s each eight at a time on a 4-core runner (release
run 45736667710). One boto3 client, shared by the plan's workers (boto3 clients are thread-safe), makes each call a
request. The interface is ``row_reuse.S3``'s, so ``find_reuse`` and ``bind_reuse`` take either. One difference: only a
missing object reads as missing; any other error (access denied, throttling past retries) raises, so a misconfigured
role stops the plan instead of quietly rebuilding every row.
"""

from __future__ import annotations

import threading
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

_MISSING = {"404", "NoSuchKey", "NotFound"}


def _missing(error: ClientError) -> bool:
    return str(error.response.get("Error", {}).get("Code")) in _MISSING


class Boto3S3:
    def __init__(self, bucket: str, *, client: Any = None, max_connections: int = 64) -> None:
        self.bucket = bucket
        self.client = client or boto3.client(
            "s3", config=Config(max_pool_connections=max_connections, retries={"mode": "standard", "max_attempts": 5})
        )
        # row_reuse._completed caches completion records per store.
        self.completed: dict[tuple[str, str, str, str], bool] = {}
        self.completed_lock = threading.Lock()

    def keys(self, prefix: str) -> list[str]:
        keys: list[str] = []
        token = None
        while True:
            request = {"Bucket": self.bucket, "Prefix": prefix}
            if token:
                request["ContinuationToken"] = token
            page = self.client.list_objects_v2(**request)
            keys.extend(item["Key"] for item in page.get("Contents", []))
            if not page.get("IsTruncated"):
                return keys
            token = page["NextContinuationToken"]

    def head(self, key: str) -> dict[str, Any] | None:
        try:
            return self.client.head_object(Bucket=self.bucket, Key=key, ChecksumMode="ENABLED")
        except ClientError as error:
            if _missing(error):
                return None
            raise

    def get(self, key: str) -> bytes | None:
        try:
            return self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        except ClientError as error:
            if _missing(error):
                return None
            raise

    def put_once(self, key: str, data: bytes) -> None:
        try:
            self.client.put_object(Bucket=self.bucket, Key=key, Body=data, IfNoneMatch="*")
            return
        except ClientError as error:
            if str(error.response.get("Error", {}).get("Code")) not in {"PreconditionFailed", "412"}:
                raise
        if self.get(key) != data:
            raise SystemExit(f"s3://{self.bucket}/{key} already exists with different content")
