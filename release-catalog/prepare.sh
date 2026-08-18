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

require_nonempty "RELEASE_ARTIFACT_TYPE" "${RELEASE_ARTIFACT_TYPE:-}"
require_nonempty "RELEASE_OUTPUT_DIRECTORY" "${RELEASE_OUTPUT_DIRECTORY:-}"

script_directory="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
artifacts="${RELEASE_ARTIFACTS:-}"

case "${RELEASE_ARTIFACT_TYPE}" in
  conda)
    if [[ -z "${artifacts}" ]]; then
      artifacts="$("${script_directory}/describe-conda.sh" "${RELEASE_OUTPUT_DIRECTORY}")"
    fi
    ;;
  wheel)
    if [[ -z "${artifacts}" ]]; then
      artifacts="$("${script_directory}/describe-wheels.sh" "${RELEASE_OUTPUT_DIRECTORY}")"
    fi
    ;;
  custom)
    require_nonempty "RELEASE_ARTIFACTS" "${artifacts}"
    require_nonempty "RELEASE_PACKAGE_IDENTITY_FILE" "${RELEASE_PACKAGE_IDENTITY_FILE:-}"
    ;;
  *)
    echo "artifact-type must be one of: conda, custom, wheel" >&2
    exit 1
    ;;
esac

printf 'artifacts=%s\n' "${artifacts}" >>"${GITHUB_OUTPUT}"
