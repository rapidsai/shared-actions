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
RELEASE_ARTIFACT_DIRECTORY="${temporary_directory}"
export GITHUB_OUTPUT RELEASE_ARTIFACTS RELEASE_ARTIFACT_DIRECTORY

"${repository_root}/release-catalog/prepare.sh"

artifacts="$(sed -n 's/^artifacts=//p' "${GITHUB_OUTPUT}")"
jq -e '
  . == [{
    path: "libkvikio_cu12-26.8.0-py3-none-manylinux_2_28_x86_64.whl",
    package: {ecosystem: "wheel", name: "libkvikio-cu12", version: "26.8.0"}
  }]
' <<<"${artifacts}" >/dev/null

conda_output_directory="${temporary_directory}/conda"
conda_staging_directory="${temporary_directory}/conda-staging"
mkdir -p "${conda_output_directory}/noarch" "${conda_staging_directory}/info"
jq -n \
  '{name: "rapids-dask-dependency", version: "26.08.0", build: "py_0", subdir: "noarch"}' \
  >"${conda_staging_directory}/info/index.json"
tar -cjf \
  "${conda_output_directory}/noarch/rapids-dask-dependency-26.08.0-py_0.tar.bz2" \
  -C "${conda_staging_directory}" \
  info/index.json

GITHUB_OUTPUT="${temporary_directory}/conda-output"
RELEASE_ARTIFACT_DIRECTORY="${conda_output_directory}"
export GITHUB_OUTPUT RELEASE_ARTIFACT_DIRECTORY

"${repository_root}/release-catalog/prepare.sh"

artifacts="$(sed -n 's/^artifacts=//p' "${GITHUB_OUTPUT}")"
jq -e '
  . == [{
    path: "noarch/rapids-dask-dependency-26.08.0-py_0.tar.bz2",
    package: {
      ecosystem: "conda",
      name: "rapids-dask-dependency",
      version: "26.08.0",
      build: "py_0",
      platform: "noarch"
    }
  }]
' <<<"${artifacts}" >/dev/null

GITHUB_OUTPUT="${temporary_directory}/custom-output"
RELEASE_ARTIFACT_DIRECTORY="${temporary_directory}"
printf '%s\n' bundle >"${temporary_directory}/bundle.tar.gz"
jq -n '{ecosystem: "archive", name: "bundle", version: "1.0"}' \
  >"${temporary_directory}/release-package-identity.json"
RELEASE_ARTIFACTS="$(jq -cn '[{path: "bundle.tar.gz", package_identity_file: "release-package-identity.json", sbom: "bundle.spdx.json"}]')"
export GITHUB_OUTPUT RELEASE_ARTIFACTS RELEASE_ARTIFACT_DIRECTORY

"${repository_root}/release-catalog/prepare.sh"

grep -Fx 'artifacts=[{"path":"bundle.tar.gz","package_identity_file":"release-package-identity.json","sbom":"bundle.spdx.json"}]' "${GITHUB_OUTPUT}"

GITHUB_OUTPUT="${temporary_directory}/explicit-wheel-output"
RELEASE_ARTIFACT_DIRECTORY="${temporary_directory}"
RELEASE_ARTIFACTS="$(jq -cn '[{path: "libkvikio_cu12-*.whl", signature: "libkvikio.sig"}]')"
export GITHUB_OUTPUT RELEASE_ARTIFACTS RELEASE_ARTIFACT_DIRECTORY
"${repository_root}/release-catalog/prepare.sh"
artifacts="$(sed -n 's/^artifacts=//p' "${GITHUB_OUTPUT}")"
jq -e '
  . == [{
    path: "libkvikio_cu12-*.whl",
    signature: "libkvikio.sig",
    package: {ecosystem: "wheel", name: "libkvikio-cu12", version: "26.8.0"}
  }]
' <<<"${artifacts}" >/dev/null

mixed_directory="${temporary_directory}/mixed"
mkdir -p "${mixed_directory}"
cp "${temporary_directory}/libkvikio_cu12-26.8.0-py3-none-manylinux_2_28_x86_64.whl" "${mixed_directory}/"
cp "${conda_output_directory}/noarch/rapids-dask-dependency-26.08.0-py_0.tar.bz2" "${mixed_directory}/"
GITHUB_OUTPUT="${temporary_directory}/mixed-output"
RELEASE_ARTIFACTS=''
RELEASE_ARTIFACT_DIRECTORY="${mixed_directory}"
export GITHUB_OUTPUT RELEASE_ARTIFACTS RELEASE_ARTIFACT_DIRECTORY
"${repository_root}/release-catalog/prepare.sh"
artifacts="$(sed -n 's/^artifacts=//p' "${GITHUB_OUTPUT}")"
jq -e '
  length == 2
  and ([.[].package.ecosystem] | sort) == ["conda", "wheel"]
' <<<"${artifacts}" >/dev/null

empty_directory="${temporary_directory}/empty"
mkdir -p "${empty_directory}"
GITHUB_OUTPUT="${temporary_directory}/empty-output"
RELEASE_ARTIFACT_DIRECTORY="${empty_directory}"
export GITHUB_OUTPUT RELEASE_ARTIFACT_DIRECTORY
if "${repository_root}/release-catalog/prepare.sh" 2>"${temporary_directory}/empty-error"; then
  echo "prepare.sh unexpectedly accepted an artifact directory without detectable artifacts" >&2
  exit 1
fi
grep -Fx 'artifact-directory contains no detectable Conda or wheel artifacts; explicitly selected artifacts require package_identity_file when their identity cannot be parsed' "${temporary_directory}/empty-error"

GITHUB_OUTPUT="${temporary_directory}/unsupported-output"
RELEASE_ARTIFACT_DIRECTORY="${temporary_directory}"
RELEASE_ARTIFACTS="$(jq -cn '[{path: "bundle.tar.gz"}]')"
export GITHUB_OUTPUT RELEASE_ARTIFACTS RELEASE_ARTIFACT_DIRECTORY
if "${repository_root}/release-catalog/prepare.sh" 2>"${temporary_directory}/unsupported-error"; then
  echo "prepare.sh unexpectedly accepted an unsupported artifact without identity" >&2
  exit 1
fi
grep -Fx 'artifact identity cannot be extracted; package_identity_file is required: bundle.tar.gz' "${temporary_directory}/unsupported-error"

GITHUB_OUTPUT="${temporary_directory}/conflicting-identity-output"
RELEASE_ARTIFACTS="$(jq -cn '[{path: "libkvikio_cu12-*.whl", package_identity_file: "release-package-identity.json"}]')"
export GITHUB_OUTPUT RELEASE_ARTIFACTS
if "${repository_root}/release-catalog/prepare.sh" 2>"${temporary_directory}/conflicting-identity-error"; then
  echo "prepare.sh unexpectedly accepted an identity file for a parseable artifact" >&2
  exit 1
fi
grep -Fx 'package_identity_file is not allowed when wheel identity can be extracted: libkvikio_cu12-*.whl' "${temporary_directory}/conflicting-identity-error"
