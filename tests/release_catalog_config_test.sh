#!/usr/bin/env bash
# Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.

set -euo pipefail

repository_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
validator="${repository_root}/release-catalog/validate-config.sh"
temporary_directory="$(mktemp -d)"
trap 'rm -rf "${temporary_directory}"' EXIT

assert_invalid() {
  local name="$1"
  local config="$2"
  local expected="$3"
  local error_file="${temporary_directory}/${name}.error"

  if RELEASE_CATALOG_CONFIG="${config}" GITHUB_OUTPUT="${temporary_directory}/${name}.output" \
    "${validator}" 2>"${error_file}"; then
    echo "validate-config.sh unexpectedly accepted ${name}" >&2
    exit 1
  fi
  if ! grep -F -- "${expected}" "${error_file}" >/dev/null; then
    echo "validate-config.sh did not explain ${name}; output was:" >&2
    sed 's/^/  /' "${error_file}" >&2
    exit 1
  fi
}

valid_output="${temporary_directory}/valid.output"
RELEASE_CATALOG_CONFIG="$(<"${repository_root}/tests/release-catalog-config/package-identity-file.json")" \
  GITHUB_OUTPUT="${valid_output}" "${validator}"

grep -Fx 'release_catalog_key=maven:cuvs-java' "${valid_output}"
grep -Fx 'artifact_directory=java/cuvs-java/target' "${valid_output}"
grep -Fx 'artifacts=[{"path":"cuvs-java-*-x86_64-cuda*.jar","package_identity_file":"cuvs-java.release-package-identity.json"}]' "${valid_output}"

assert_invalid \
  malformed-json \
  '{"release_catalog_key":' \
  'release catalog configuration must be valid JSON'

assert_invalid \
  not-an-object \
  '[]' \
  'release catalog configuration must be a JSON object'

assert_invalid \
  missing-fields \
  '{}' \
  'release_catalog_key must be a non-empty, single-line string'

assert_invalid \
  top-level-package-identity-file \
  '{"release_catalog_key":"archive:smoke","package_identity_file":"package.json"}' \
  'unknown field(s): package_identity_file'

assert_invalid \
  unknown-field \
  '{"release_catalog_key":"archive:smoke","artifacts":[{"path":"smoke.tar.gz","package_identity_file":"package.json"}],"component_id":"archive:smoke"}' \
  'unknown field(s): component_id'

assert_invalid \
  artifact-type \
  '{"artifact_type":"custom","release_catalog_key":"archive:smoke","artifacts":[{"path":"smoke.tar.gz","package_identity_file":"package.json"}]}' \
  'unknown field(s): artifact_type'

assert_invalid \
  inline-package \
  '{"release_catalog_key":"archive:smoke","package":{"ecosystem":"archive","name":"smoke","version":"1.0"},"artifacts":[{"path":"smoke.tar.gz","package_identity_file":"package.json"}]}' \
  'unknown field(s): package'

assert_invalid \
  per-artifact-package \
  '{"release_catalog_key":"archive:smoke","artifacts":[{"path":"smoke.tar.gz","package":{"ecosystem":"archive","name":"smoke","version":"1.0"}}]}' \
  'artifacts[0] has unknown field(s): package'

assert_invalid \
  malformed-artifact \
  '{"release_catalog_key":"archive:smoke","artifacts":[{"file":"smoke.tar.gz","package_identity_file":false,"sbom":false}]}' \
  'artifacts[0] has unknown field(s): file, sbom'
grep -F 'artifacts[0].path must be a non-empty, single-line string' "${temporary_directory}/malformed-artifact.error" >/dev/null
grep -F 'artifacts[0].package_identity_file must be a non-empty, single-line string' "${temporary_directory}/malformed-artifact.error" >/dev/null

default_output="${temporary_directory}/default.output"
RELEASE_CATALOG_CONFIG='{"release_catalog_key":"conda:smoke"}' \
  GITHUB_OUTPUT="${default_output}" "${validator}"
grep -Fx 'release_catalog_key=conda:smoke' "${default_output}"
grep -Fx 'artifact_directory=.' "${default_output}"
grep -Fx 'artifacts=' "${default_output}"

standard_output="${temporary_directory}/standard.output"
RELEASE_CATALOG_CONFIG='{
  "release_catalog_key": "wheel:kvikio",
  "artifact_directory": "dist"
}' GITHUB_OUTPUT="${standard_output}" "${validator}"

grep -Fx 'release_catalog_key=wheel:kvikio' "${standard_output}"
grep -Fx 'artifact_directory=dist' "${standard_output}"
grep -Fx 'artifacts=' "${standard_output}"
