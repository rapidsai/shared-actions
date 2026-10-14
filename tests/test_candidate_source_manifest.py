# Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
"""Train source manifests never invent a mutable checkout identity."""

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).parents[1] / "release-candidate-dependencies" / "write_source_manifest.py"
_SPEC = importlib.util.spec_from_file_location("write_source_manifest", _SCRIPT)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError(f"unable to load source manifest validator: {_SCRIPT}")
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
source_manifest = _MODULE.source_manifest


def test_source_manifest_preserves_exact_frozen_repository_and_revision_pairs():
    train = {
        "github_repositories": {"zlib": "rapidsai/zlib", "cudf": "rapidsai/cudf"},
        "source_revisions": {"zlib": "b" * 40, "cudf": "a" * 40},
    }

    expected = {
        "schema_version": 1,
        "source_revisions": {"cudf": "a" * 40, "zlib": "b" * 40},
        "github_repositories": {"cudf": "rapidsai/cudf", "zlib": "rapidsai/zlib"},
    }
    if source_manifest(train) != expected:
        pytest.fail("source manifest did not preserve the frozen source mappings")


@pytest.mark.parametrize(
    "train",
    [
        {"source_revisions": {}, "github_repositories": {}},
        {"source_revisions": {"cudf": "a" * 40}, "github_repositories": {}},
        {"source_revisions": {"cudf": "main"}, "github_repositories": {"cudf": "rapidsai/cudf"}},
        {
            "source_revisions": {"cudf": "a" * 40},
            "github_repositories": {"cudf": "https://github.com/rapidsai/cudf"},
        },
    ],
)
def test_invalid_or_mutable_source_identity_fails_closed(train):
    with pytest.raises(ValueError):
        source_manifest(train)


def test_non_object_train_fails_with_a_clear_validation_error():
    with pytest.raises(ValueError, match="release train must be a JSON object"):
        source_manifest([])
