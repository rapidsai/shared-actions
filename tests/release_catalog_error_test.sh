#!/usr/bin/env bash
# Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.

set -euo pipefail

repository_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
temporary_directory="$(mktemp -d)"
trap 'rm -rf "${temporary_directory}"' EXIT

# shellcheck source=release-catalog/error.sh
source "${repository_root}/release-catalog/error.sh"

RELEASE_CATALOG_SCRIPT_NAME="materialize.sh"
release_catalog_error "invalid artifact" 2>"${temporary_directory}/local-error"
grep -Fx '[materialize.sh] Error: invalid artifact' "${temporary_directory}/local-error"

GITHUB_ACTIONS="true"
RELEASE_CATALOG_ERROR_TITLE="Release catalog upload failed"
release_catalog_error $'invalid 100%\r\nartifact' 2>"${temporary_directory}/github-error"
grep -Fx '::error title=Release catalog upload failed::invalid 100%25%0D%0Aartifact' \
  "${temporary_directory}/github-error"
