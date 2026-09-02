#!/usr/bin/env python3
# Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.

"""Verify valid and invalid release-catalog contracts against JSON Schema."""

from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator

ROOT = Path(__file__).parents[1]


def _load(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"schema fixture must be a JSON object: {path}")
    return value


def _validator(path: Path) -> Draft202012Validator:
    schema = _load(path)
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def main() -> None:
    config = _validator(ROOT / "release-catalog/config.schema.json")
    fixtures = ROOT / "tests/release-catalog-config"
    for path in sorted((fixtures / "valid").glob("*.json")):
        errors = list(config.iter_errors(_load(path)))
        if errors:
            raise ValueError(f"valid fixture rejected: {path}: {errors[0].message}")
    for path in sorted((fixtures / "invalid").glob("*.json")):
        if not list(config.iter_errors(_load(path))):
            raise ValueError(f"invalid fixture accepted: {path}")

    entries = _validator(ROOT / "release-catalog/entries.schema.json")
    example = _load(ROOT / "release-catalog/examples/cuvs-java/release-catalog-entries.json")
    errors = list(entries.iter_errors(example))
    if errors:
        raise ValueError(f"generated entries example rejected: {errors[0].message}")
    wrong_producer = {**example, "producer": "some-other-tool"}
    if not list(entries.iter_errors(wrong_producer)):
        raise ValueError("entries schema accepted an unknown producer")


if __name__ == "__main__":
    main()
