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
  def package_object:
    type == "object"
    and (keys - ["ecosystem", "name", "version", "build", "platform"] | length == 0)
    and (.ecosystem | single_line_string)
    and (.name | single_line_string)
    and ((.version // "") | type == "string")
    and ((.build // "") | type == "string")
    and ((.platform // "") | type == "string");
  def artifact_package_object:
    type == "object"
    and (keys - ["ecosystem", "name", "version", "build", "platform"] | length == 0)
    and (to_entries | map(.value | single_line_string) | all);
  if type != "object" then
    ["release catalog configuration must be a JSON object"]
  else
    (keys - ["artifact_type", "release_catalog_key", "output_directory", "package", "package_file", "artifacts"]) as $unknown
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
            if ([has("package"), has("package_file")] | map(select(.)) | length) == 1 then empty
            else "exactly one of package or package_file is required for custom artifacts" end,
            if (has("package") | not) or (.package | package_object) then empty
            else "package must contain non-empty ecosystem and name strings and only version, build, or platform as optional string fields" end,
            if (has("package_file") | not) or (.package_file | single_line_string) then empty
            else "package_file must be a non-empty, single-line path relative to output_directory" end,
            if (.artifacts | type == "array" and length > 0) then empty
            else "artifacts must be a non-empty array for custom artifacts" end
          ]
        elif .artifact_type == "conda" or .artifact_type == "wheel" then
          [
            if (.output_directory | single_line_string) then empty
            else "output_directory is required for conda and wheel artifacts" end,
            if has("artifacts") or has("package") or has("package_file") then
              "artifacts, package, and package_file are only valid when artifact_type is custom"
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
                ($artifact | keys - ["path", "sbom", "provenance", "signature", "package"]) as $artifact_unknown
                | if ($artifact_unknown | length) > 0 then
                    "artifacts[\($index)] has unknown field(s): " + ($artifact_unknown | join(", "))
                  else empty end,
                  if ($artifact.path | single_line_string) then empty
                  else "artifacts[\($index)].path must be a non-empty, single-line string" end,
                  ($artifact | to_entries[]
                    | select(.key == "sbom" or .key == "provenance" or .key == "signature")
                    | select((.value | single_line_string) | not)
                    | "artifacts[\($index)].\(.key) must be a non-empty, single-line string"),
                  if ($artifact | has("package") | not) or ($artifact.package | artifact_package_object) then empty
                  else "artifacts[\($index)].package must contain only non-empty string identity overrides" end
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
  printf 'package=%s\n' "$(jq -c '.package // empty' <<<"${compact_config}")"
  printf 'package_file=%s\n' "$(jq -r '.package_file // empty' <<<"${compact_config}")"
  printf 'artifacts=%s\n' "$(jq -c '.artifacts // empty' <<<"${compact_config}")"
} >>"${GITHUB_OUTPUT}"
