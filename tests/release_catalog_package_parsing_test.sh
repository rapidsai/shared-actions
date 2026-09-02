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
RELEASE_ARTIFACTS=''
RELEASE_CATALOG_KEY="test:package-parsing"
RELEASE_ENTRIES_NAME="release-catalog-entries.json"
RELEASE_SOURCE_ARTIFACT_NAME="package-parsing-test"
RAPIDS_SHA="0123456789012345678901234567890123456789"
export GITHUB_REPOSITORY GITHUB_RUN_ATTEMPT GITHUB_RUN_ID GITHUB_WORKFLOW_REF
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

jar_output_directory="${temporary_directory}/jars"
jar_staging_directory="${temporary_directory}/jar-staging"
mkdir -p "${jar_output_directory}" "${jar_staging_directory}/META-INF/maven/ai.rapids/cuvs-java"
printf '%s\n' \
  'artifactId=cuvs-java' \
  'groupId=ai.rapids' \
  'version=26.08.0' \
  >"${jar_staging_directory}/META-INF/maven/ai.rapids/cuvs-java/pom.properties"
(
  cd "${jar_staging_directory}"
  zip -qr "${jar_output_directory}/cuvs-java-26.08.0.jar" .
)

RELEASE_ARTIFACT_DIRECTORY="${jar_output_directory}"
export RELEASE_ARTIFACT_DIRECTORY
"${repository_root}/release-catalog/materialize.sh"
jq -e '
  [.entries[] | {path, package}] == [{
    path: "cuvs-java-26.08.0.jar",
    package: {ecosystem: "maven", name: "ai.rapids:cuvs-java", version: "26.08.0"}
  }]
' "${jar_output_directory}/release-catalog-entries.json" >/dev/null

ambiguous_jar_directory="${temporary_directory}/ambiguous-jar"
ambiguous_jar_staging_directory="${temporary_directory}/ambiguous-jar-staging"
mkdir -p \
  "${ambiguous_jar_directory}" \
  "${ambiguous_jar_staging_directory}/META-INF/maven/example/first" \
  "${ambiguous_jar_staging_directory}/META-INF/maven/example/second"
printf '%s\n' 'groupId=example' 'artifactId=first' 'version=1.0' \
  >"${ambiguous_jar_staging_directory}/META-INF/maven/example/first/pom.properties"
printf '%s\n' 'groupId=example' 'artifactId=second' 'version=1.0' \
  >"${ambiguous_jar_staging_directory}/META-INF/maven/example/second/pom.properties"
(
  cd "${ambiguous_jar_staging_directory}"
  zip -qr "${ambiguous_jar_directory}/shaded.jar" .
)
RELEASE_ARTIFACT_DIRECTORY="${ambiguous_jar_directory}"
export RELEASE_ARTIFACT_DIRECTORY
if "${repository_root}/release-catalog/materialize.sh" 2>"${temporary_directory}/ambiguous-jar-error"; then
  echo "materialize.sh unexpectedly accepted a JAR with ambiguous Maven identity" >&2
  exit 1
fi
grep -F 'Maven JAR must contain exactly one META-INF/maven/<groupId>/<artifactId>/pom.properties file: shaded.jar' \
  "${temporary_directory}/ambiguous-jar-error" >/dev/null

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
