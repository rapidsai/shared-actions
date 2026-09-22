#!/usr/bin/env bash
# Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.

set -euo pipefail

export RELEASE_CATALOG_SCRIPT_NAME="validate-config.sh"
export RELEASE_CATALOG_ERROR_TITLE="Invalid release catalog configuration"
# shellcheck source=release-catalog/error.sh
source "$(dirname "${BASH_SOURCE[0]}")/error.sh"

# Validate the public JSON input before materialize.sh reads any build output.
# Structural errors are emitted as GitHub Actions annotations so callers see
# every invalid field in one run. Validated values are written to GITHUB_OUTPUT
# for the dispatch action's later materialization step.

config="${RELEASE_CATALOG_CONFIG:-}"
if [[ -z "${config}" ]]; then
  release_catalog_error "release catalog configuration must be a non-empty JSON object"
  exit 1
fi

if ! compact_config="$(jq -ce . <<<"${config}" 2>/dev/null)"; then
  parse_error="$(jq -ce . <<<"${config}" 2>&1 || true)"
  release_catalog_error "release catalog configuration must be valid JSON: ${parse_error}"
  exit 1
fi

validation_errors="$(jq -r '
  def single_line_string:
    type == "string" and length > 0 and (test("[\\r\\n]") | not);
  def relative_path:
    single_line_string
    and (startswith("/") | not)
    and (split("/") | index("..") | not);
  if type != "object" then
    ["release catalog configuration must be a JSON object"]
  else
    (keys - ["release_catalog_key", "artifact_directory", "artifacts"]) as $unknown
    | [
        if ($unknown | length) > 0 then
          "unknown field(s): " + ($unknown | join(", "))
        else empty end,
        if (.release_catalog_key | single_line_string) then empty
        else "release_catalog_key must be a non-empty, single-line string" end,
        if (.artifact_directory | relative_path) then empty
        else "artifact_directory must be a non-empty relative path without parent traversal" end
      ]
      + if has("artifacts") then
          [
            if (.artifacts | type == "array" and length > 0) then empty
            else "artifacts must be a non-empty array when supplied" end
          ]
        else [] end
      + if (.artifacts | type) == "array" then
          [
            .artifacts | to_entries[]
            | .key as $index
            | .value as $artifact
            | if ($artifact | type) != "object" then
                "artifacts[\($index)] must be an object"
              else
                ($artifact | keys - ["path", "package_identity_file"]) as $artifact_unknown
                | if ($artifact_unknown | length) > 0 then
                    "artifacts[\($index)] has unknown field(s): " + ($artifact_unknown | join(", "))
                  else empty end,
                  if ($artifact.path | relative_path) then empty
                  else "artifacts[\($index)].path must be a non-empty relative path without parent traversal" end,
                  ($artifact | to_entries[]
                    | select(.key == "package_identity_file")
                    | select((.value | relative_path) | not)
                    | "artifacts[\($index)].\(.key) must be a non-empty relative path without parent traversal")
              end
          ]
        else [] end
  end
  | .[]
' <<<"${compact_config}")"

if [[ -n "${validation_errors}" ]]; then
  while IFS= read -r validation_error; do
    release_catalog_error "${validation_error}"
  done <<<"${validation_errors}"
  exit 1
fi

if [[ -z "${GITHUB_OUTPUT:-}" ]]; then
  release_catalog_error "GITHUB_OUTPUT is required"
  exit 1
fi

{
  printf 'release_catalog_key=%s\n' "$(jq -r '.release_catalog_key' <<<"${compact_config}")"
  printf 'artifact_directory=%s\n' "$(jq -r '.artifact_directory' <<<"${compact_config}")"
  printf 'artifacts=%s\n' "$(jq -c '.artifacts // empty' <<<"${compact_config}")"
} >>"${GITHUB_OUTPUT}"
