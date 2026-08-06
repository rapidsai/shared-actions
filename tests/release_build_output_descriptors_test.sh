#!/usr/bin/env bash
# Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.

set -euo pipefail

repository_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
temporary_directory="$(mktemp -d)"
trap 'rm -rf "${temporary_directory}"' EXIT

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

wheel_descriptors="$("${repository_root}/release-build-output/describe-wheels.sh" "${wheel_output_directory}")"
jq -e '
  . == [{
    path: "libkvikio_cu12-26.8.0a32-py3-none-manylinux_2_28_x86_64.whl",
    package: {ecosystem: "wheel", name: "libkvikio-cu12", version: "26.8.0a32"}
  }]
' <<<"${wheel_descriptors}" >/dev/null

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

conda_descriptors="$("${repository_root}/release-build-output/describe-conda.sh" "${conda_output_directory}")"
jq -e '
  length == 2
  and any(.[];
    .path == "linux-64/librmm-26.08.00a32-cuda12_260714_2f567060.conda"
    and .package == {
      ecosystem: "conda",
      name: "librmm",
      version: "26.08.00a32",
      build: "cuda12_260714_2f567060",
      platform: "linux-64"
    }
  )
  and any(.[];
    .path == "noarch/rapids-dask-dependency-26.08.0-py_0.tar.bz2"
    and .package == {
      ecosystem: "conda",
      name: "rapids-dask-dependency",
      version: "26.08.0",
      build: "py_0",
      platform: "noarch"
    }
  )
' <<<"${conda_descriptors}" >/dev/null
