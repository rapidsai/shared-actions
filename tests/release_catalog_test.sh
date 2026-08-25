#!/usr/bin/env bash
# Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.

set -euo pipefail

repository_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
temporary_directory="$(mktemp -d)"
trap 'rm -rf "${temporary_directory}"' EXIT

bundle_directory="${temporary_directory}/bundle"
mkdir -p "${bundle_directory}"
printf '%s\n' jar >"${bundle_directory}/cuvs-java-26.08.0.jar"
jq -n \
  '{ecosystem: "maven", name: "ai.rapids:cuvs-java", version: "26.08.0"}' \
  >"${bundle_directory}/cuvs-java-package-identity.json"

GITHUB_REPOSITORY="rapidsai/cuvs"
GITHUB_RUN_ATTEMPT="1"
GITHUB_RUN_ID="1234"
GITHUB_SHA="0123456789012345678901234567890123456789"
GITHUB_WORKFLOW_REF="rapidsai/cuvs/.github/workflows/build.yaml@refs/heads/release/26.08"
RELEASE_ARTIFACTS="$(jq -cn '[{path: "cuvs-java-*.jar", package_identity_file: "cuvs-java-package-identity.json"}]')"
RELEASE_ENTRIES_NAME="release-catalog-entries.json"
RELEASE_ARTIFACT_DIRECTORY="${bundle_directory}"
RELEASE_SOURCE_ARTIFACT_NAME="cuvs-java-cuda12.9.1"
RAPIDS_SHA="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
RELEASE_CATALOG_KEY="maven:cuvs-java"
export GITHUB_REPOSITORY GITHUB_RUN_ATTEMPT GITHUB_RUN_ID GITHUB_SHA GITHUB_WORKFLOW_REF
export RELEASE_ARTIFACTS RELEASE_ENTRIES_NAME RELEASE_ARTIFACT_DIRECTORY
export RAPIDS_SHA RELEASE_SOURCE_ARTIFACT_NAME RELEASE_CATALOG_KEY

"${repository_root}/release-catalog/materialize.sh"

canonical_bundle_directory="$(realpath "${bundle_directory}")"
entries_path="${canonical_bundle_directory}/release-catalog-entries.json"
test ! -e "${canonical_bundle_directory}/release-catalog-metadata.json"
jq -e '
  .schema_version == 1
  and .producer == "shared-workflows"
  and .source.artifact == "cuvs-java-cuda12.9.1"
  and .source.repository == "rapidsai/cuvs"
  and .source.sha == "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  and (.entries | length == 1)
  and .entries[0].release_catalog_key == "maven:cuvs-java"
  and .entries[0].path == "cuvs-java-26.08.0.jar"
  and .entries[0].package.name == "ai.rapids:cuvs-java"
  and .entries[0].sbom_kind == "generated-identity"
' "${entries_path}" >/dev/null

# Optional signatures must not cause the S3 uploader to request a literal
# `null` file. This mirrors its declared-file selection without requiring AWS.
mapfile -t upload_paths < <(jq -r '["release-catalog-entries.json"] + ([.entries[] | .path, .sbom, .provenance, .signature? | strings] | unique) | .[]' "${entries_path}")
test "${upload_paths[0]}" = "release-catalog-entries.json"
test "${upload_paths[1]}" = "cuvs-java-26.08.0.jar"
test "${#upload_paths[@]}" -eq 4
for upload_path in "${upload_paths[@]}"; do
  test -f "${canonical_bundle_directory}/${upload_path}"
done
if printf '%s\n' "${upload_paths[@]}" | grep -Fx null; then
  echo "release catalog uploader must not request a literal null file" >&2
  exit 1
fi

isolated_companion_directory="${temporary_directory}/isolated-companion"
mkdir -p "${isolated_companion_directory}"
cp "${entries_path}" "${isolated_companion_directory}/"
cp -R "${canonical_bundle_directory}/release-evidence" "${isolated_companion_directory}/"
while IFS= read -r evidence_path; do
  test -f "${isolated_companion_directory}/${evidence_path}"
done < <(jq -r '.entries[] | .sbom, .provenance' "${isolated_companion_directory}/release-catalog-entries.json")

multiple_identity_directory="${temporary_directory}/multiple-identities"
mkdir -p "${multiple_identity_directory}"
printf '%s\n' first >"${multiple_identity_directory}/first.tar.gz"
printf '%s\n' second >"${multiple_identity_directory}/second.jar"
jq -n '{ecosystem: "archive", name: "first", version: "1.0"}' \
  >"${multiple_identity_directory}/first.identity.json"
jq -n '{ecosystem: "maven", name: "example:second", version: "2.0"}' \
  >"${multiple_identity_directory}/second.identity.json"
RELEASE_ARTIFACTS="$(jq -cn '[
  {path: "first.tar.gz", package_identity_file: "first.identity.json"},
  {path: "second.jar", package_identity_file: "second.identity.json"}
]')"
RELEASE_ARTIFACT_DIRECTORY="${multiple_identity_directory}"
RELEASE_SOURCE_ARTIFACT_NAME="multiple-identities"
RAPIDS_SHA="eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
RELEASE_CATALOG_KEY="archive:multiple"
export RAPIDS_SHA RELEASE_ARTIFACTS RELEASE_ARTIFACT_DIRECTORY RELEASE_SOURCE_ARTIFACT_NAME RELEASE_CATALOG_KEY
"${repository_root}/release-catalog/materialize.sh"
jq -e '
  (.entries | length == 2)
  and .entries[0].package == {ecosystem: "archive", name: "first", version: "1.0"}
  and .entries[1].package == {ecosystem: "maven", name: "example:second", version: "2.0"}
' "${multiple_identity_directory}/release-catalog-entries.json" >/dev/null

generated_directory="${temporary_directory}/generated-bundle"
mkdir -p "${generated_directory}/linux-64"
printf '%s\n' conda >"${generated_directory}/linux-64/kvikio-26.08.00a32-cuda12_260714_2f567060.bin"
jq -n '{ecosystem: "conda", name: "kvikio", version: "26.08.00a32"}' \
  >"${generated_directory}/kvikio.identity.json"

RELEASE_ARTIFACTS="$(jq -cn '[{path: "linux-64/kvikio-*.bin", package_identity_file: "kvikio.identity.json"}]')"
RELEASE_ARTIFACT_DIRECTORY="${generated_directory}"
RELEASE_SOURCE_ARTIFACT_NAME="kvikio_conda_python_kvikio_x86_64_abi3_cu12"
RAPIDS_SHA="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
RELEASE_CATALOG_KEY="conda:kvikio"
export RAPIDS_SHA RELEASE_ARTIFACTS RELEASE_ARTIFACT_DIRECTORY RELEASE_SOURCE_ARTIFACT_NAME RELEASE_CATALOG_KEY

"${repository_root}/release-catalog/materialize.sh"

generated_entries_path="${generated_directory}/release-catalog-entries.json"
generated_sbom_path="$(jq -r '.entries[0].sbom' "${generated_entries_path}")"
generated_provenance_path="$(jq -r '.entries[0].provenance' "${generated_entries_path}")"
jq -e '
  .source.artifact == "kvikio_conda_python_kvikio_x86_64_abi3_cu12"
  and .source.sha == "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
  and .entries[0].release_catalog_key == "conda:kvikio"
  and .entries[0].path == "linux-64/kvikio-26.08.00a32-cuda12_260714_2f567060.bin"
  and .entries[0].package == {ecosystem: "conda", name: "kvikio", version: "26.08.00a32"}
  and .entries[0].sbom_kind == "generated-identity"
' "${generated_entries_path}" >/dev/null
jq -e '
  .spdxVersion == "SPDX-2.3"
  and .packages[0].name == "kvikio"
  and .packages[0].versionInfo == "26.08.00a32"
' "${generated_directory}/${generated_sbom_path}" >/dev/null
jq -e '
  .predicateType == "https://slsa.dev/provenance/v1"
  and .predicate.buildDefinition.externalParameters.release_catalog_key == "conda:kvikio"
  and .predicate.buildDefinition.resolvedDependencies[0].digest.gitCommit == "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
' "${generated_directory}/${generated_provenance_path}" >/dev/null
wheel_directory="${temporary_directory}/wheel-bundle"
mkdir -p "${wheel_directory}"
printf '%s\n' wheel >"${wheel_directory}/libkvikio_cu12-26.8.0a32.bin"
jq -n '{ecosystem: "wheel", name: "libkvikio-cu12", version: "26.8.0a32"}' \
  >"${wheel_directory}/libkvikio.identity.json"

RELEASE_ARTIFACTS="$(jq -cn '[{path: "libkvikio_cu12-*.bin", package_identity_file: "libkvikio.identity.json"}]')"
RELEASE_ARTIFACT_DIRECTORY="${wheel_directory}"
RELEASE_SOURCE_ARTIFACT_NAME="kvikio_wheel_cpp_libkvikio_x86_64_cu12"
RAPIDS_SHA="cccccccccccccccccccccccccccccccccccccccc"
RELEASE_CATALOG_KEY="wheel:kvikio"
export RAPIDS_SHA RELEASE_ARTIFACTS RELEASE_ARTIFACT_DIRECTORY RELEASE_SOURCE_ARTIFACT_NAME RELEASE_CATALOG_KEY

"${repository_root}/release-catalog/materialize.sh"

jq -e '
  .entries[0].release_catalog_key == "wheel:kvikio"
  and .entries[0].path == "libkvikio_cu12-26.8.0a32.bin"
  and .entries[0].package == {ecosystem: "wheel", name: "libkvikio-cu12", version: "26.8.0a32"}
  and .entries[0].sbom_kind == "generated-identity"
' "${wheel_directory}/release-catalog-entries.json" >/dev/null

missing_version_directory="${temporary_directory}/missing-version-bundle"
mkdir -p "${missing_version_directory}"
printf '%s\n' archive >"${missing_version_directory}/bundle.tar.gz"
jq -n \
  '{ecosystem: "archive", name: "bundle"}' \
  >"${missing_version_directory}/bundle-package-identity.json"

RELEASE_ARTIFACTS="$(jq -cn '[{path: "bundle.tar.gz", package_identity_file: "bundle-package-identity.json"}]')"
RELEASE_ARTIFACT_DIRECTORY="${missing_version_directory}"
RELEASE_SOURCE_ARTIFACT_NAME="bundle-archive"
RAPIDS_SHA="dddddddddddddddddddddddddddddddddddddddd"
RELEASE_CATALOG_KEY="archive:bundle"
export RAPIDS_SHA RELEASE_ARTIFACTS RELEASE_ARTIFACT_DIRECTORY RELEASE_SOURCE_ARTIFACT_NAME RELEASE_CATALOG_KEY

if "${repository_root}/release-catalog/materialize.sh" 2>"${temporary_directory}/missing-version-error"; then
  echo "materialize.sh unexpectedly accepted a custom artifact without a package version" >&2
  exit 1
fi
grep -Fx 'package identity for bundle.tar.gz must contain non-empty ecosystem, name, and version strings and only optional build or platform strings' "${temporary_directory}/missing-version-error"

if (unset RAPIDS_SHA; "${repository_root}/release-catalog/materialize.sh") 2>"${temporary_directory}/missing-rapids-sha-error"; then
  echo "materialize.sh unexpectedly accepted a missing RAPIDS_SHA" >&2
  exit 1
fi
grep -Fx 'RAPIDS_SHA must be a non-empty string' "${temporary_directory}/missing-rapids-sha-error"
