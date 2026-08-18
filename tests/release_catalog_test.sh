#!/usr/bin/env bash
# Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.

set -euo pipefail

repository_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
temporary_directory="$(mktemp -d)"
trap 'rm -rf "${temporary_directory}"' EXIT

bundle_directory="${temporary_directory}/bundle"
mkdir -p "${bundle_directory}"
printf '%s\n' jar >"${bundle_directory}/cuvs-java-26.08.0.jar"
printf '%s\n' sbom >"${bundle_directory}/cuvs-java-26.08.0.spdx.json"
printf '%s\n' provenance >"${bundle_directory}/cuvs-java-26.08.0.provenance.jsonl"
printf '%s\n' signature >"${bundle_directory}/cuvs-java-26.08.0.jar.asc"
jq -n \
  '{ecosystem: "maven", name: "ai.rapids:cuvs-java", version: "26.08.0"}' \
  >"${bundle_directory}/cuvs-java-package-identity.json"

GITHUB_OUTPUT="${temporary_directory}/github-output"
GITHUB_REPOSITORY="rapidsai/cuvs"
GITHUB_RUN_ATTEMPT="1"
GITHUB_RUN_ID="1234"
GITHUB_SHA="0123456789012345678901234567890123456789"
GITHUB_WORKFLOW_REF="rapidsai/cuvs/.github/workflows/build.yaml@refs/heads/release/26.08"
export GITHUB_OUTPUT
RELEASE_ARTIFACTS="$(jq -cn '[{path: "cuvs-java-*.jar", package_identity_file: "cuvs-java-package-identity.json", sbom: "cuvs-java-*.spdx.json", provenance: "cuvs-java-*.provenance.jsonl", signature: "cuvs-java-*.jar.asc"}]')"
RELEASE_ENTRIES_NAME="release-catalog-entries.json"
RELEASE_OUTPUT_DIRECTORY="${bundle_directory}"
RELEASE_SOURCE_ARTIFACT_NAME="cuvs-java-cuda12.9.1"
RELEASE_SOURCE_SHA="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
RELEASE_CATALOG_KEY="maven:cuvs-java"
export GITHUB_REPOSITORY GITHUB_RUN_ATTEMPT GITHUB_RUN_ID GITHUB_SHA GITHUB_WORKFLOW_REF
export RELEASE_ARTIFACTS RELEASE_ENTRIES_NAME RELEASE_OUTPUT_DIRECTORY
export RELEASE_SOURCE_ARTIFACT_NAME RELEASE_SOURCE_SHA RELEASE_CATALOG_KEY

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
  and .entries[0].sbom_kind == "producer-dependency"
' "${entries_path}" >/dev/null
grep -Fx "entries-path=${entries_path}" "${GITHUB_OUTPUT}"

supplied_sbom_path="$(jq -r '.entries[0].sbom' "${entries_path}")"
supplied_provenance_path="$(jq -r '.entries[0].provenance' "${entries_path}")"
supplied_signature_path="$(jq -r '.entries[0].signature' "${entries_path}")"
[[ "${supplied_sbom_path}" == release-evidence/*/sbom-* ]]
[[ "${supplied_provenance_path}" == release-evidence/*/provenance-* ]]
[[ "${supplied_signature_path}" == release-evidence/*/signature-* ]]
grep -Fx sbom "${canonical_bundle_directory}/${supplied_sbom_path}"
grep -Fx provenance "${canonical_bundle_directory}/${supplied_provenance_path}"
grep -Fx signature "${canonical_bundle_directory}/${supplied_signature_path}"

isolated_companion_directory="${temporary_directory}/isolated-companion"
mkdir -p "${isolated_companion_directory}"
cp "${entries_path}" "${isolated_companion_directory}/"
cp -R "${canonical_bundle_directory}/release-evidence" "${isolated_companion_directory}/"
while IFS= read -r evidence_path; do
  test -f "${isolated_companion_directory}/${evidence_path}"
done < <(jq -r '.entries[] | .sbom, .provenance, (.signature // empty)' "${isolated_companion_directory}/release-catalog-entries.json")

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
RELEASE_OUTPUT_DIRECTORY="${multiple_identity_directory}"
RELEASE_SOURCE_ARTIFACT_NAME="multiple-identities"
RELEASE_SOURCE_SHA="eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
RELEASE_CATALOG_KEY="archive:multiple"
export RELEASE_ARTIFACTS RELEASE_OUTPUT_DIRECTORY RELEASE_SOURCE_ARTIFACT_NAME RELEASE_SOURCE_SHA RELEASE_CATALOG_KEY
"${repository_root}/release-catalog/materialize.sh"
jq -e '
  (.entries | length == 2)
  and .entries[0].package == {ecosystem: "archive", name: "first", version: "1.0"}
  and .entries[1].package == {ecosystem: "maven", name: "example:second", version: "2.0"}
' "${multiple_identity_directory}/release-catalog-entries.json" >/dev/null

generated_directory="${temporary_directory}/generated-bundle"
mkdir -p "${generated_directory}/linux-64"
printf '%s\n' conda >"${generated_directory}/linux-64/kvikio-26.08.00a32-cuda12_260714_2f567060.conda"

RELEASE_ARTIFACTS="$(jq -cn '[{path: "linux-64/kvikio-*.conda", package: {ecosystem: "conda", name: "kvikio", version: "26.08.00a32"}}]')"
RELEASE_OUTPUT_DIRECTORY="${generated_directory}"
RELEASE_SOURCE_ARTIFACT_NAME="kvikio_conda_python_kvikio_x86_64_abi3_cu12"
RELEASE_SOURCE_SHA="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
RELEASE_CATALOG_KEY="conda:kvikio"
export RELEASE_ARTIFACTS RELEASE_OUTPUT_DIRECTORY RELEASE_SOURCE_ARTIFACT_NAME RELEASE_SOURCE_SHA RELEASE_CATALOG_KEY

"${repository_root}/release-catalog/materialize.sh"

generated_entries_path="${generated_directory}/release-catalog-entries.json"
generated_sbom_path="$(jq -r '.entries[0].sbom' "${generated_entries_path}")"
generated_provenance_path="$(jq -r '.entries[0].provenance' "${generated_entries_path}")"
jq -e '
  .source.artifact == "kvikio_conda_python_kvikio_x86_64_abi3_cu12"
  and .source.sha == "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
  and .entries[0].release_catalog_key == "conda:kvikio"
  and .entries[0].path == "linux-64/kvikio-26.08.00a32-cuda12_260714_2f567060.conda"
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
printf '%s\n' wheel >"${wheel_directory}/libkvikio_cu12-26.8.0a32-py3-none-manylinux_2_28_x86_64.whl"

RELEASE_ARTIFACTS="$(jq -cn '[{path: "libkvikio_cu12-*.whl", package: {ecosystem: "wheel", name: "libkvikio-cu12", version: "26.8.0a32"}}]')"
RELEASE_OUTPUT_DIRECTORY="${wheel_directory}"
RELEASE_SOURCE_ARTIFACT_NAME="kvikio_wheel_cpp_libkvikio_x86_64_cu12"
RELEASE_SOURCE_SHA="cccccccccccccccccccccccccccccccccccccccc"
RELEASE_CATALOG_KEY="wheel:kvikio"
export RELEASE_ARTIFACTS RELEASE_OUTPUT_DIRECTORY RELEASE_SOURCE_ARTIFACT_NAME RELEASE_SOURCE_SHA RELEASE_CATALOG_KEY

"${repository_root}/release-catalog/materialize.sh"

jq -e '
  .entries[0].release_catalog_key == "wheel:kvikio"
  and .entries[0].path == "libkvikio_cu12-26.8.0a32-py3-none-manylinux_2_28_x86_64.whl"
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
RELEASE_OUTPUT_DIRECTORY="${missing_version_directory}"
RELEASE_SOURCE_ARTIFACT_NAME="bundle-archive"
RELEASE_SOURCE_SHA="dddddddddddddddddddddddddddddddddddddddd"
RELEASE_CATALOG_KEY="archive:bundle"
export RELEASE_ARTIFACTS RELEASE_OUTPUT_DIRECTORY RELEASE_SOURCE_ARTIFACT_NAME RELEASE_SOURCE_SHA RELEASE_CATALOG_KEY

if "${repository_root}/release-catalog/materialize.sh" 2>"${temporary_directory}/missing-version-error"; then
  echo "materialize.sh unexpectedly accepted a custom artifact without a package version" >&2
  exit 1
fi
grep -Fx 'package identity for bundle.tar.gz must contain non-empty ecosystem, name, and version strings and only optional build or platform strings' "${temporary_directory}/missing-version-error"
