#!/usr/bin/env bash
# Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.

set -euo pipefail

require_nonempty() {
  local name="$1"
  local value="$2"
  if [[ -z "${value}" ]]; then
    echo "${name} must be a non-empty string" >&2
    exit 1
  fi
}

require_nonempty "RELEASE_OUTPUT_DIRECTORY" "${RELEASE_OUTPUT_DIRECTORY:-}"

script_directory="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
artifacts="${RELEASE_ARTIFACTS:-}"
if [[ ! -d "${RELEASE_OUTPUT_DIRECTORY}" ]]; then
  echo "output-directory does not exist or is not a directory: ${RELEASE_OUTPUT_DIRECTORY}" >&2
  exit 1
fi
output_directory="$(realpath "${RELEASE_OUTPUT_DIRECTORY}")"

ensure_relative_pattern() {
  local field="$1"
  local pattern="$2"
  if [[ "${pattern}" == /* || "${pattern}" == */../* || "${pattern}" == ../* || "${pattern}" == *"/.." ]]; then
    echo "${field} must be a relative path inside output-directory: ${pattern}" >&2
    exit 1
  fi
}

resolve_primary_file() {
  local pattern="$1"
  local -a matches=()

  ensure_relative_pattern "artifact path" "${pattern}"
  while IFS= read -r match; do
    matches+=("${match}")
  done < <(compgen -G "${output_directory}/${pattern}" || true)
  if [[ "${#matches[@]}" -ne 1 || ! -f "${matches[0]:-}" ]]; then
    echo "artifact path must resolve to exactly one file: ${pattern}" >&2
    exit 1
  fi

  local resolved
  resolved="$(realpath "${matches[0]}")"
  if [[ "${resolved}" != "${output_directory}"/* ]]; then
    echo "artifact path must resolve inside output-directory: ${pattern}" >&2
    exit 1
  fi
  printf '%s\n' "${resolved}"
}

if [[ -z "${artifacts}" ]]; then
  conda_descriptors='[]'
  wheel_descriptors='[]'
  if [[ -n "$(find "${output_directory}" -type f \( -name '*.conda' -o -name '*.tar.bz2' \) -print -quit)" ]]; then
    conda_descriptors="$("${script_directory}/describe-conda.sh" "${output_directory}")"
  fi
  if [[ -n "$(find "${output_directory}" -type f -name '*.whl' -print -quit)" ]]; then
    wheel_descriptors="$("${script_directory}/describe-wheels.sh" "${output_directory}")"
  fi
  artifacts="$(jq -cn --argjson conda "${conda_descriptors}" --argjson wheel "${wheel_descriptors}" '$conda + $wheel')"
  if [[ "$(jq 'length' <<<"${artifacts}")" -eq 0 ]]; then
    echo "output-directory contains no detectable Conda or wheel artifacts; explicitly selected artifacts require package_identity_file when their identity cannot be parsed" >&2
    exit 1
  fi
else
  prepared_artifacts='[]'
  while IFS= read -r descriptor; do
    primary_file="$(resolve_primary_file "$(jq -r '.path' <<<"${descriptor}")")"
    identity_file="$(jq -r '.package_identity_file // empty' <<<"${descriptor}")"
    package=''

    case "${primary_file}" in
      *.whl)
        if [[ -n "${identity_file}" ]]; then
          echo "package_identity_file is not allowed when wheel identity can be extracted: $(jq -r '.path' <<<"${descriptor}")" >&2
          exit 1
        fi
        package="$("${script_directory}/describe-wheels.sh" "${output_directory}" "${primary_file}" | jq -c '.[0].package')"
        ;;
      *.conda)
        if [[ -n "${identity_file}" ]]; then
          echo "package_identity_file is not allowed when Conda identity can be extracted: $(jq -r '.path' <<<"${descriptor}")" >&2
          exit 1
        fi
        package="$("${script_directory}/describe-conda.sh" "${output_directory}" "${primary_file}" | jq -c '.[0].package')"
        ;;
      *.tar.bz2)
        if conda_descriptor="$("${script_directory}/describe-conda.sh" "${output_directory}" "${primary_file}" 2>/dev/null)"; then
          if [[ -n "${identity_file}" ]]; then
            echo "package_identity_file is not allowed when Conda identity can be extracted: $(jq -r '.path' <<<"${descriptor}")" >&2
            exit 1
          fi
          package="$(jq -c '.[0].package' <<<"${conda_descriptor}")"
        elif [[ -z "${identity_file}" ]]; then
          echo "artifact is not a valid Conda package and requires package_identity_file: $(jq -r '.path' <<<"${descriptor}")" >&2
          exit 1
        fi
        ;;
      *)
        if [[ -z "${identity_file}" ]]; then
          echo "artifact identity cannot be extracted; package_identity_file is required: $(jq -r '.path' <<<"${descriptor}")" >&2
          exit 1
        fi
        ;;
    esac

    if [[ -n "${package}" ]]; then
      descriptor="$(jq -c --argjson package "${package}" '. + {package: $package}' <<<"${descriptor}")"
    fi
    prepared_artifacts="$(jq -cn --argjson current "${prepared_artifacts}" --argjson descriptor "${descriptor}" '$current + [$descriptor]')"
  done < <(jq -c '.[]' <<<"${artifacts}")
  artifacts="${prepared_artifacts}"
fi

printf 'artifacts=%s\n' "${artifacts}" >>"${GITHUB_OUTPUT}"
