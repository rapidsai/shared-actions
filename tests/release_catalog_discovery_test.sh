#!/usr/bin/env bash
# Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.

set -euo pipefail

repository_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
temporary_directory="$(mktemp -d)"
trap 'rm -rf "${temporary_directory}"' EXIT

GITHUB_REPOSITORY="rapidsai/shared-actions"
GITHUB_RUN_ATTEMPT="1"
GITHUB_RUN_ID="1234"
GITHUB_WORKFLOW_REF="rapidsai/shared-actions/.github/workflows/pr.yml@refs/pull/136/merge"
RELEASE_CATALOG_KEY="test:discovery"
RELEASE_SOURCE_ARTIFACT_NAME="discovery-test"
RELEASE_SOURCE_SHA="0123456789012345678901234567890123456789"
export GITHUB_REPOSITORY GITHUB_RUN_ATTEMPT GITHUB_RUN_ID GITHUB_WORKFLOW_REF
export RELEASE_SOURCE_SHA RELEASE_CATALOG_KEY RELEASE_SOURCE_ARTIFACT_NAME

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

RELEASE_ARTIFACTS=''
RELEASE_ARTIFACT_DIRECTORY="${temporary_directory}"
export RELEASE_ARTIFACTS RELEASE_ARTIFACT_DIRECTORY

"${repository_root}/release-catalog/materialize.sh"

jq -e '
  [.entries[] | {path, package}] == [{
    path: "libkvikio_cu12-26.8.0-py3-none-manylinux_2_28_x86_64.whl",
    package: {ecosystem: "wheel", name: "libkvikio-cu12", version: "26.8.0"}
  }]
' "${temporary_directory}/release-catalog-entries.json" >/dev/null

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

RELEASE_ARTIFACT_DIRECTORY="${conda_output_directory}"
export RELEASE_ARTIFACT_DIRECTORY

"${repository_root}/release-catalog/materialize.sh"

jq -e '
  [.entries[] | {path, package}] == [{
    path: "noarch/rapids-dask-dependency-26.08.0-py_0.tar.bz2",
    package: {
      ecosystem: "conda",
      name: "rapids-dask-dependency",
      version: "26.08.0",
      build: "py_0",
      platform: "noarch"
    }
  }]
' "${conda_output_directory}/release-catalog-entries.json" >/dev/null

RELEASE_ARTIFACT_DIRECTORY="${temporary_directory}"
printf '%s\n' bundle >"${temporary_directory}/bundle.tar.gz"
jq -n '{ecosystem: "archive", name: "bundle", version: "1.0"}' \
  >"${temporary_directory}/release-package-identity.json"
RELEASE_ARTIFACTS="$(jq -cn '[{path: "bundle.tar.gz", package_identity_file: "release-package-identity.json"}]')"
export RELEASE_ARTIFACTS RELEASE_ARTIFACT_DIRECTORY

"${repository_root}/release-catalog/materialize.sh"

jq -e '
  .entries[0].path == "bundle.tar.gz"
  and .entries[0].package == {ecosystem: "archive", name: "bundle", version: "1.0"}
  and .entries[0].sbom_kind == "generated-identity"
' "${temporary_directory}/release-catalog-entries.json" >/dev/null

RELEASE_ARTIFACT_DIRECTORY="${temporary_directory}"
RELEASE_ARTIFACTS="$(jq -cn '[{path: "libkvikio_cu12-*.whl"}]')"
export RELEASE_ARTIFACTS RELEASE_ARTIFACT_DIRECTORY
"${repository_root}/release-catalog/materialize.sh"
jq -e '
  .entries[0].path == "libkvikio_cu12-26.8.0-py3-none-manylinux_2_28_x86_64.whl"
  and .entries[0].package == {ecosystem: "wheel", name: "libkvikio-cu12", version: "26.8.0"}
  and .entries[0].sbom_kind == "generated-identity"
' "${temporary_directory}/release-catalog-entries.json" >/dev/null

mixed_directory="${temporary_directory}/mixed"
mkdir -p "${mixed_directory}"
cp "${temporary_directory}/libkvikio_cu12-26.8.0-py3-none-manylinux_2_28_x86_64.whl" "${mixed_directory}/"
cp "${conda_output_directory}/noarch/rapids-dask-dependency-26.08.0-py_0.tar.bz2" "${mixed_directory}/"
RELEASE_ARTIFACTS=''
RELEASE_ARTIFACT_DIRECTORY="${mixed_directory}"
export RELEASE_ARTIFACTS RELEASE_ARTIFACT_DIRECTORY
"${repository_root}/release-catalog/materialize.sh"
jq -e '
  (.entries | length == 2)
  and ([.entries[].package.ecosystem] | sort) == ["conda", "wheel"]
' "${mixed_directory}/release-catalog-entries.json" >/dev/null

empty_directory="${temporary_directory}/empty"
mkdir -p "${empty_directory}"
RELEASE_ARTIFACT_DIRECTORY="${empty_directory}"
export RELEASE_ARTIFACT_DIRECTORY
if "${repository_root}/release-catalog/materialize.sh" 2>"${temporary_directory}/empty-error"; then
  echo "materialize.sh unexpectedly accepted an artifact directory without detectable artifacts" >&2
  exit 1
fi
grep -Fx 'artifact-directory contains no detectable Conda, wheel, or Maven JAR artifacts; explicitly selected artifacts require package_identity_file when their identity cannot be parsed' "${temporary_directory}/empty-error"

RELEASE_ARTIFACT_DIRECTORY="${temporary_directory}"
RELEASE_ARTIFACTS="$(jq -cn '[{path: "bundle.tar.gz"}]')"
export RELEASE_ARTIFACTS RELEASE_ARTIFACT_DIRECTORY
if "${repository_root}/release-catalog/materialize.sh" 2>"${temporary_directory}/unsupported-error"; then
  echo "materialize.sh unexpectedly accepted an unsupported artifact without identity" >&2
  exit 1
fi
grep -Fx 'artifact identity cannot be extracted; package_identity_file is required: bundle.tar.gz' "${temporary_directory}/unsupported-error"

RELEASE_ARTIFACTS="$(jq -cn '[{path: "libkvikio_cu12-*.whl", package_identity_file: "release-package-identity.json"}]')"
export RELEASE_ARTIFACTS
if "${repository_root}/release-catalog/materialize.sh" 2>"${temporary_directory}/conflicting-identity-error"; then
  echo "materialize.sh unexpectedly accepted an identity file for a parseable artifact" >&2
  exit 1
fi
grep -Fx 'package_identity_file is not allowed when wheel identity can be extracted: libkvikio_cu12-*.whl' "${temporary_directory}/conflicting-identity-error"
