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
RELEASE_CATALOG_CONFIG="$(<"${repository_root}/tests/release-catalog-config/package-file.json")" \
  GITHUB_OUTPUT="${valid_output}" "${validator}"

grep -Fx 'release_catalog_key=maven:cuvs-java' "${valid_output}"
grep -Fx 'artifact_type=custom' "${valid_output}"
grep -Fx 'output_directory=java/cuvs-java/target' "${valid_output}"
grep -Fx 'package=' "${valid_output}"
grep -Fx 'package_file=cuvs-java.release-package.json' "${valid_output}"
grep -Fx 'artifacts=[{"path":"cuvs-java-*-x86_64-cuda*.jar","sbom":"cuvs-java.spdx.json"}]' "${valid_output}"

inline_output="${temporary_directory}/inline.output"
RELEASE_CATALOG_CONFIG="$(<"${repository_root}/tests/release-catalog-config/inline-package.json")" \
  GITHUB_OUTPUT="${inline_output}" "${validator}"

grep -Fx 'output_directory=.' "${inline_output}"
grep -Fx 'package={"ecosystem":"archive","name":"smoke","version":"1.0"}' "${inline_output}"
grep -Fx 'package_file=' "${inline_output}"

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
grep -F 'artifact_type must be one of: conda, custom, wheel' "${temporary_directory}/missing-fields.error" >/dev/null

assert_invalid \
  custom-missing-fields \
  '{"artifact_type":"custom","release_catalog_key":"archive:smoke"}' \
  'exactly one of package or package_file is required for custom artifacts'
grep -F 'artifacts must be a non-empty array for custom artifacts' "${temporary_directory}/custom-missing-fields.error" >/dev/null

assert_invalid \
  unknown-field \
  '{"artifact_type":"custom","release_catalog_key":"archive:smoke","package_file":"package.json","artifacts":[{"path":"smoke.tar.gz"}],"component_id":"archive:smoke"}' \
  'unknown field(s): component_id'

assert_invalid \
  conflicting-package-source \
  '{"artifact_type":"custom","release_catalog_key":"archive:smoke","package":{"ecosystem":"archive","name":"smoke"},"package_file":"package.json","artifacts":[{"path":"smoke.tar.gz"}]}' \
  'exactly one of package or package_file is required for custom artifacts'

assert_invalid \
  malformed-artifact \
  '{"artifact_type":"custom","release_catalog_key":"archive:smoke","package_file":"package.json","artifacts":[{"file":"smoke.tar.gz","sbom":false}]}' \
  'artifacts[0] has unknown field(s): file'
grep -F 'artifacts[0].path must be a non-empty, single-line string' "${temporary_directory}/malformed-artifact.error" >/dev/null
grep -F 'artifacts[0].sbom must be a non-empty, single-line string' "${temporary_directory}/malformed-artifact.error" >/dev/null

assert_invalid \
  standard-missing-output-directory \
  '{"artifact_type":"conda","release_catalog_key":"conda:smoke"}' \
  'output_directory is required for conda and wheel artifacts'

assert_invalid \
  standard-custom-fields \
  '{"artifact_type":"wheel","release_catalog_key":"wheel:smoke","output_directory":"dist","artifacts":[{"path":"smoke.whl"}]}' \
  'artifacts, package, and package_file are only valid when artifact_type is custom'

standard_output="${temporary_directory}/standard.output"
RELEASE_CATALOG_CONFIG='{
  "artifact_type": "wheel",
  "release_catalog_key": "wheel:kvikio",
  "output_directory": "dist"
}' GITHUB_OUTPUT="${standard_output}" "${validator}"

grep -Fx 'artifact_type=wheel' "${standard_output}"
grep -Fx 'release_catalog_key=wheel:kvikio' "${standard_output}"
grep -Fx 'output_directory=dist' "${standard_output}"
grep -Fx 'artifacts=' "${standard_output}"
