#!/usr/bin/env bash
# Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.

# Shared error rendering for release-catalog scripts. GitHub Actions receives a
# workflow-command annotation; local callers receive a conventional stderr line.

release_catalog_error() {
  local message="$1"
  local script_name="${RELEASE_CATALOG_SCRIPT_NAME:-release-catalog}"
  local title="${RELEASE_CATALOG_ERROR_TITLE:-Release catalog error}"

  if [[ "${GITHUB_ACTIONS:-}" == "true" ]]; then
    message="${message//'%'/'%25'}"
    message="${message//$'\r'/'%0D'}"
    message="${message//$'\n'/'%0A'}"
    printf '::error title=%s::%s\n' "${title}" "${message}" >&2
  else
    printf '[%s] Error: %s\n' "${script_name}" "${message}" >&2
  fi
}
