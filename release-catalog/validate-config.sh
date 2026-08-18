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
    (keys - ["release_catalog_key", "output_directory", "artifacts"]) as $unknown
    | [
        if ($unknown | length) > 0 then
          "unknown field(s): " + ($unknown | join(", "))
        else empty end,
        if (.release_catalog_key | single_line_string) then empty
        else "release_catalog_key must be a non-empty, single-line string" end,
        if (has("output_directory") | not) or (.output_directory | single_line_string) then empty
        else "output_directory must be a non-empty, single-line string when supplied" end
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
                ($artifact | keys - ["path", "package_identity_file", "sbom", "provenance", "signature"]) as $artifact_unknown
                | if ($artifact_unknown | length) > 0 then
                    "artifacts[\($index)] has unknown field(s): " + ($artifact_unknown | join(", "))
                  else empty end,
                  if ($artifact.path | single_line_string) then empty
                  else "artifacts[\($index)].path must be a non-empty, single-line string" end,
                  ($artifact | to_entries[]
                    | select(.key == "package_identity_file" or .key == "sbom" or .key == "provenance" or .key == "signature")
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
  printf 'release_catalog_key=%s\n' "$(jq -r '.release_catalog_key' <<<"${compact_config}")"
  printf 'output_directory=%s\n' "$(jq -r '.output_directory // "."' <<<"${compact_config}")"
  printf 'artifacts=%s\n' "$(jq -c '.artifacts // empty' <<<"${compact_config}")"
} >>"${GITHUB_OUTPUT}"
