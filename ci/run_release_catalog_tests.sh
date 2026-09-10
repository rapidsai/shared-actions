#!/usr/bin/env bash
# Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.

set -euo pipefail

repository_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

"${repository_root}/tests/release_catalog_config_test.sh"
"${repository_root}/tests/release_catalog_discovery_test.sh"
"${repository_root}/tests/release_catalog_error_test.sh"
"${repository_root}/tests/release_catalog_package_parsing_test.sh"
"${repository_root}/tests/release_catalog_test.sh"
