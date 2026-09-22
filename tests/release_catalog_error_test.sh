#!/usr/bin/env bash
# Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.

set -euo pipefail

repository_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
temporary_directory="$(mktemp -d)"
trap 'rm -rf "${temporary_directory}"' EXIT

# shellcheck source=/dev/null
source "${repository_root}/release-catalog/error.sh"

export RELEASE_CATALOG_SCRIPT_NAME="materialize.sh"
export GITHUB_ACTIONS=""
release_catalog_error "invalid artifact" 2>"${temporary_directory}/local-error"
grep -Fxq '[materialize.sh] Error: invalid artifact' "${temporary_directory}/local-error"

export GITHUB_ACTIONS="true"
export RELEASE_CATALOG_ERROR_TITLE="Release catalog upload failed"
release_catalog_error $'invalid 100%\r\nartifact' 2>"${temporary_directory}/github-error"
grep -Fxq '::error title=Release catalog upload failed::invalid 100%25%0D%0Aartifact' \
  "${temporary_directory}/github-error"
