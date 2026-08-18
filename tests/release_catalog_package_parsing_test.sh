#!/usr/bin/env bash
# Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.

set -euo pipefail

repository_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
temporary_directory="$(mktemp -d)"
trap 'rm -rf "${temporary_directory}"' EXIT

RELEASE_ARTIFACTS=''
RELEASE_CATALOG_KEY="test:package-parsing"
RELEASE_ENTRIES_NAME="release-catalog-entries.json"
RELEASE_SOURCE_ARTIFACT_NAME="package-parsing-test"
RAPIDS_SHA="0123456789012345678901234567890123456789"
export RAPIDS_SHA RELEASE_ARTIFACTS RELEASE_CATALOG_KEY RELEASE_ENTRIES_NAME RELEASE_SOURCE_ARTIFACT_NAME

wheel_output_directory="${temporary_directory}/wheels"
wheel_staging_directory="${temporary_directory}/wheel-staging"
mkdir -p "${wheel_output_directory}" "${wheel_staging_directory}/libkvikio_cu12-26.8.0.dist-info"
printf '%s\n' \
  'Metadata-Version: 2.1' \
  'Name: libkvikio-cu12' \
  'Version: 26.8.0a32' \
  >"${wheel_staging_directory}/libkvikio_cu12-26.8.0.dist-info/METADATA"
(
  cd "${wheel_staging_directory}"
  zip -qr "${wheel_output_directory}/libkvikio_cu12-26.8.0a32-py3-none-manylinux_2_28_x86_64.whl" .
)

RELEASE_ARTIFACT_DIRECTORY="${wheel_output_directory}"
export RELEASE_ARTIFACT_DIRECTORY
"${repository_root}/release-catalog/materialize.sh"
jq -e '
  [.entries[] | {path, package}] == [{
    path: "libkvikio_cu12-26.8.0a32-py3-none-manylinux_2_28_x86_64.whl",
    package: {ecosystem: "wheel", name: "libkvikio-cu12", version: "26.8.0a32"}
  }]
' "${wheel_output_directory}/release-catalog-entries.json" >/dev/null

conda_output_directory="${temporary_directory}/conda"
tar_bz2_staging_directory="${temporary_directory}/tar-bz2-staging"
conda_staging_directory="${temporary_directory}/conda-staging"
mkdir -p \
  "${conda_output_directory}/linux-64" \
  "${conda_output_directory}/noarch" \
  "${tar_bz2_staging_directory}/info" \
  "${conda_staging_directory}/info"

jq -n \
  '{name: "rapids-dask-dependency", version: "26.08.0", build: "py_0", subdir: "noarch"}' \
  >"${tar_bz2_staging_directory}/info/index.json"
tar -cjf \
  "${conda_output_directory}/noarch/rapids-dask-dependency-26.08.0-py_0.tar.bz2" \
  -C "${tar_bz2_staging_directory}" \
  info/index.json

jq -n \
  '{name: "librmm", version: "26.08.00a32", build: "cuda12_260714_2f567060", subdir: "linux-64"}' \
  >"${conda_staging_directory}/info/index.json"
tar -cf "${temporary_directory}/info-librmm.tar" -C "${conda_staging_directory}" info/index.json
zstd -q -f "${temporary_directory}/info-librmm.tar" -o "${temporary_directory}/info-librmm.tar.zst"
(
  cd "${temporary_directory}"
  zip -q \
    "${conda_output_directory}/linux-64/librmm-26.08.00a32-cuda12_260714_2f567060.conda" \
    info-librmm.tar.zst
)

RELEASE_ARTIFACT_DIRECTORY="${conda_output_directory}"
export RELEASE_ARTIFACT_DIRECTORY
"${repository_root}/release-catalog/materialize.sh"
jq -e '
  (.entries | length == 2)
  and any(.entries[];
    .path == "linux-64/librmm-26.08.00a32-cuda12_260714_2f567060.conda"
    and .package == {
      ecosystem: "conda",
      name: "librmm",
      version: "26.08.00a32",
      build: "cuda12_260714_2f567060",
      platform: "linux-64"
    }
  )
  and any(.entries[];
    .path == "noarch/rapids-dask-dependency-26.08.0-py_0.tar.bz2"
    and .package == {
      ecosystem: "conda",
      name: "rapids-dask-dependency",
      version: "26.08.0",
      build: "py_0",
      platform: "noarch"
    }
  )
' "${conda_output_directory}/release-catalog-entries.json" >/dev/null

invalid_wheel_directory="${temporary_directory}/invalid-wheel"
mkdir -p "${invalid_wheel_directory}"
printf '%s\n' invalid >"${invalid_wheel_directory}/invalid.whl"
RELEASE_ARTIFACT_DIRECTORY="${invalid_wheel_directory}"
export RELEASE_ARTIFACT_DIRECTORY
if "${repository_root}/release-catalog/materialize.sh" 2>"${temporary_directory}/invalid-wheel-error"; then
  echo "materialize.sh unexpectedly accepted an invalid wheel" >&2
  exit 1
fi
grep -F 'wheel must contain exactly one .dist-info/METADATA file: invalid.whl' \
  "${temporary_directory}/invalid-wheel-error" >/dev/null
