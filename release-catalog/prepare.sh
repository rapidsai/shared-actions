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
package="${RELEASE_PACKAGE:-}"

case "${RELEASE_ARTIFACT_TYPE}" in
  conda)
    if [[ -z "${artifacts}" ]]; then
      artifacts="$("${script_directory}/describe-conda.sh" "${RELEASE_OUTPUT_DIRECTORY}")"
    fi
    if [[ -z "${package}" && -z "${RELEASE_PACKAGE_FILE:-}" ]]; then
      package='{"ecosystem":"conda","name":"bundle"}'
    fi
    ;;
  wheel)
    if [[ -z "${artifacts}" ]]; then
      artifacts="$("${script_directory}/describe-wheels.sh" "${RELEASE_OUTPUT_DIRECTORY}")"
    fi
    if [[ -z "${package}" && -z "${RELEASE_PACKAGE_FILE:-}" ]]; then
      package='{"ecosystem":"wheel","name":"bundle"}'
    fi
    ;;
  custom)
    require_nonempty "RELEASE_ARTIFACTS" "${artifacts}"
    ;;
  *)
    echo "artifact-type must be one of: conda, custom, wheel" >&2
    exit 1
    ;;
esac

if [[ -z "${package}" && -z "${RELEASE_PACKAGE_FILE:-}" ]]; then
  echo "one of release-package or release-package-file is required for custom artifacts" >&2
  exit 1
fi

printf 'artifacts=%s\n' "${artifacts}" >>"${GITHUB_OUTPUT}"
printf 'package=%s\n' "${package}" >>"${GITHUB_OUTPUT}"
