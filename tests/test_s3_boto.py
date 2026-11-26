# Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.

"""The reuse plan's S3 store in-process with boto3, with the AWS CLI store's interface and semantics."""

from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest

boto3 = pytest.importorskip("boto3")
from botocore.exceptions import ClientError  # noqa: E402
from botocore.response import StreamingBody  # noqa: E402
from botocore.stub import ANY, Stubber  # noqa: E402

sys.path.insert(0, str(Path(__file__).parents[1] / "release-candidate-dependencies"))
import s3_boto  # noqa: E402

BUCKET = "candidate-store"


@pytest.fixture
def store():
    client = boto3.client("s3", region_name="us-east-2", aws_access_key_id="test", aws_secret_access_key="test")  # noqa: S106
    with Stubber(client) as stubber:
        yield s3_boto.Boto3S3(BUCKET, client=client), stubber
        stubber.assert_no_pending_responses()


def body(data: bytes) -> StreamingBody:
    return StreamingBody(io.BytesIO(data), len(data))


def test_keys_reads_every_page(store):
    s3, stubber = store
    stubber.add_response(
        "list_objects_v2",
        {"Contents": [{"Key": "p/a"}], "IsTruncated": True, "NextContinuationToken": "t"},
        {"Bucket": BUCKET, "Prefix": "p/"},
    )
    stubber.add_response(
        "list_objects_v2",
        {"Contents": [{"Key": "p/b"}], "IsTruncated": False},
        {"Bucket": BUCKET, "Prefix": "p/", "ContinuationToken": "t"},
    )
    assert s3.keys("p/") == ["p/a", "p/b"]  # noqa: S101


def test_an_empty_prefix_lists_nothing(store):
    s3, stubber = store
    stubber.add_response("list_objects_v2", {"IsTruncated": False}, {"Bucket": BUCKET, "Prefix": "none/"})
    assert s3.keys("none/") == []  # noqa: S101


def test_get_returns_the_bytes_or_none_when_missing(store):
    s3, stubber = store
    stubber.add_response("get_object", {"Body": body(b"data")}, {"Bucket": BUCKET, "Key": "k"})
    stubber.add_client_error(
        "get_object", "NoSuchKey", http_status_code=404, expected_params={"Bucket": BUCKET, "Key": "gone"}
    )
    assert s3.get("k") == b"data"  # noqa: S101
    assert s3.get("gone") is None  # noqa: S101


def test_head_asks_for_the_checksum_and_returns_none_when_missing(store):
    s3, stubber = store
    stubber.add_response(
        "head_object",
        {"ChecksumSHA256": "c2hh", "ContentLength": 3, "VersionId": "v1"},
        {"Bucket": BUCKET, "Key": "k", "ChecksumMode": "ENABLED"},
    )
    stubber.add_client_error(
        "head_object",
        "404",
        http_status_code=404,
        expected_params={"Bucket": BUCKET, "Key": "gone", "ChecksumMode": "ENABLED"},
    )
    head = s3.head("k")
    assert head is not None  # noqa: S101
    assert (head["ChecksumSHA256"], head["ContentLength"], head["VersionId"]) == ("c2hh", 3, "v1")  # noqa: S101
    assert s3.head("gone") is None  # noqa: S101


@pytest.mark.parametrize("operation", ["get_object", "head_object"])
def test_an_error_other_than_missing_fails_instead_of_reading_as_missing(store, operation):
    # Access denied read as "missing" would quietly rebuild everything; the plan must stop instead.
    s3, stubber = store
    stubber.add_client_error(operation, "AccessDenied", http_status_code=403)
    with pytest.raises(ClientError):
        s3.get("k") if operation == "get_object" else s3.head("k")


def test_put_once_writes_only_if_absent(store):
    s3, stubber = store
    stubber.add_response("put_object", {}, {"Bucket": BUCKET, "Key": "k", "Body": b"data", "IfNoneMatch": "*"})
    s3.put_once("k", b"data")


def test_put_once_accepts_the_same_bytes_already_there_and_refuses_others(store):
    s3, stubber = store
    for existing in (b"data", b"other"):
        stubber.add_client_error(
            "put_object",
            "PreconditionFailed",
            http_status_code=412,
            expected_params={"Bucket": BUCKET, "Key": "k", "Body": ANY, "IfNoneMatch": "*"},
        )
        stubber.add_response("get_object", {"Body": body(existing)}, {"Bucket": BUCKET, "Key": "k"})
    s3.put_once("k", b"data")
    with pytest.raises(SystemExit, match="already exists with different content"):
        s3.put_once("k", b"data")


def test_the_plan_uses_the_boto3_store():
    plan = (Path(__file__).parents[1] / "release-candidate-dependencies" / "reuse_plan.py").read_text()
    assert "from s3_boto import Boto3S3" in plan  # noqa: S101
    assert "store = Boto3S3(args.bucket)" in plan  # noqa: S101
