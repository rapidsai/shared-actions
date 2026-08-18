#!/usr/bin/env bash
# Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.

set -euo pipefail

emit_error() {
  local message="$1"
  printf '::error title=Invalid release catalog configuration::%s\n' "${message}" >&2
}

config="${RELEASE_CATALOG_CONFIG:-}"
if [[ -z "${config}" ]]; then
  emit_error "release catalog configuration must be a non-empty JSON object"
  exit 1
fi

if ! compact_config="$(jq -ce . <<<"${config}" 2>/dev/null)"; then
  parse_error="$(jq -ce . <<<"${config}" 2>&1 || true)"
  emit_error "release catalog configuration must be valid JSON: ${parse_error}"
  exit 1
fi

validation_errors="$(jq -r '
  def single_line_string:
    type == "string" and length > 0 and (test("[\\r\\n]") | not);
  if type != "object" then
    ["release catalog configuration must be a JSON object"]
  else
    (keys - ["artifact_type", "release_catalog_key", "output_directory", "package_identity_file", "artifacts"]) as $unknown
    | [
        if ($unknown | length) > 0 then
          "unknown field(s): " + ($unknown | join(", "))
        else empty end,
        if (.artifact_type == "conda" or .artifact_type == "custom" or .artifact_type == "wheel") then empty
        else "artifact_type must be one of: conda, custom, wheel" end,
        if (.release_catalog_key | single_line_string) then empty
        else "release_catalog_key must be a non-empty, single-line string" end,
        if (has("output_directory") | not) or (.output_directory | single_line_string) then empty
        else "output_directory must be a non-empty, single-line string when supplied" end
      ]
      + if .artifact_type == "custom" then
          [
            if (.package_identity_file | single_line_string) then empty
            else "package_identity_file is required for custom artifacts and must be a non-empty, single-line path relative to output_directory" end,
            if (.artifacts | type == "array" and length > 0) then empty
            else "artifacts must be a non-empty array for custom artifacts" end
          ]
        elif .artifact_type == "conda" or .artifact_type == "wheel" then
          [
            if (.output_directory | single_line_string) then empty
            else "output_directory is required for conda and wheel artifacts" end,
            if has("artifacts") or has("package_identity_file") then
              "artifacts and package_identity_file are only valid when artifact_type is custom"
            else empty end
          ]
        else [] end
      + if .artifact_type == "custom" and (.artifacts | type) == "array" then
          [
            .artifacts | to_entries[]
            | .key as $index
            | .value as $artifact
            | if ($artifact | type) != "object" then
                "artifacts[\($index)] must be an object"
              else
                ($artifact | keys - ["path", "sbom", "provenance", "signature"]) as $artifact_unknown
                | if ($artifact_unknown | length) > 0 then
                    "artifacts[\($index)] has unknown field(s): " + ($artifact_unknown | join(", "))
                  else empty end,
                  if ($artifact.path | single_line_string) then empty
                  else "artifacts[\($index)].path must be a non-empty, single-line string" end,
                  ($artifact | to_entries[]
                    | select(.key == "sbom" or .key == "provenance" or .key == "signature")
                    | select((.value | single_line_string) | not)
                    | "artifacts[\($index)].\(.key) must be a non-empty, single-line string")
              end
          ]
        else [] end
  end
  | .[]
' <<<"${compact_config}")"

if [[ -n "${validation_errors}" ]]; then
  while IFS= read -r validation_error; do
    emit_error "${validation_error}"
  done <<<"${validation_errors}"
  exit 1
fi

if [[ -z "${GITHUB_OUTPUT:-}" ]]; then
  emit_error "GITHUB_OUTPUT is required"
  exit 1
fi

{
  printf 'artifact_type=%s\n' "$(jq -r '.artifact_type' <<<"${compact_config}")"
  printf 'release_catalog_key=%s\n' "$(jq -r '.release_catalog_key' <<<"${compact_config}")"
  printf 'output_directory=%s\n' "$(jq -r '.output_directory // "."' <<<"${compact_config}")"
  printf 'package_identity_file=%s\n' "$(jq -r '.package_identity_file // empty' <<<"${compact_config}")"
  printf 'artifacts=%s\n' "$(jq -c '.artifacts // empty' <<<"${compact_config}")"
} >>"${GITHUB_OUTPUT}"
