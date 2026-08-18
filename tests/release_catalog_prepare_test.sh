#!/usr/bin/env bash
# Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.

set -euo pipefail

repository_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
temporary_directory="$(mktemp -d)"
trap 'rm -rf "${temporary_directory}"' EXIT

mkdir -p "${temporary_directory}/wheel/libkvikio_cu12-26.8.0.dist-info"
printf '%s\n' \
  'Metadata-Version: 2.1' \
  'Name: libkvikio-cu12' \
  'Version: 26.8.0' \
  >"${temporary_directory}/wheel/libkvikio_cu12-26.8.0.dist-info/METADATA"
(
  cd "${temporary_directory}/wheel"
  zip -qr "${temporary_directory}/libkvikio_cu12-26.8.0-py3-none-manylinux_2_28_x86_64.whl" .
)

GITHUB_OUTPUT="${temporary_directory}/wheel-output"
RELEASE_ARTIFACTS=''
RELEASE_ARTIFACT_TYPE=wheel
RELEASE_OUTPUT_DIRECTORY="${temporary_directory}"
RELEASE_PACKAGE=''
RELEASE_PACKAGE_FILE=''
export GITHUB_OUTPUT RELEASE_ARTIFACTS RELEASE_ARTIFACT_TYPE RELEASE_OUTPUT_DIRECTORY RELEASE_PACKAGE RELEASE_PACKAGE_FILE

"${repository_root}/release-catalog/prepare.sh"

grep -Fx 'package={"ecosystem":"wheel","name":"bundle"}' "${GITHUB_OUTPUT}"
artifacts="$(sed -n 's/^artifacts=//p' "${GITHUB_OUTPUT}")"
jq -e '
  . == [{
    path: "libkvikio_cu12-26.8.0-py3-none-manylinux_2_28_x86_64.whl",
    package: {ecosystem: "wheel", name: "libkvikio-cu12", version: "26.8.0"}
  }]
' <<<"${artifacts}" >/dev/null

GITHUB_OUTPUT="${temporary_directory}/custom-output"
RELEASE_ARTIFACTS="$(jq -cn '[{path: "bundle.tar.gz", sbom: "bundle.spdx.json"}]')"
RELEASE_ARTIFACT_TYPE=custom
RELEASE_PACKAGE=''
RELEASE_PACKAGE_FILE=release-package.json
export GITHUB_OUTPUT RELEASE_ARTIFACTS RELEASE_ARTIFACT_TYPE RELEASE_PACKAGE RELEASE_PACKAGE_FILE

"${repository_root}/release-catalog/prepare.sh"

grep -Fx 'artifacts=[{"path":"bundle.tar.gz","sbom":"bundle.spdx.json"}]' "${GITHUB_OUTPUT}"
grep -Fx 'package=' "${GITHUB_OUTPUT}"

GITHUB_OUTPUT="${temporary_directory}/invalid-output"
RELEASE_ARTIFACT_TYPE=archive
export GITHUB_OUTPUT RELEASE_ARTIFACT_TYPE
if "${repository_root}/release-catalog/prepare.sh" 2>"${temporary_directory}/invalid-error"; then
  echo "prepare.sh unexpectedly accepted an invalid artifact type" >&2
  exit 1
fi
grep -Fx 'artifact-type must be one of: conda, custom, wheel' "${temporary_directory}/invalid-error"
