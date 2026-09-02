#!/usr/bin/env bash
# Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.

# Upload the files declared by the generated catalog document. Uploading
# existing filenames with different content will raise an error.
set -euo pipefail

export RELEASE_CATALOG_SCRIPT_NAME="upload-s3.sh"
export RELEASE_CATALOG_ERROR_TITLE="Release catalog upload failed"
# shellcheck source=release-catalog/error.sh
source "$(dirname "${BASH_SOURCE[0]}")/error.sh"

require_value() {
  local name="$1"
  local value="$2"
  if [[ -z "${value}" || "${value}" == *$'\n'* || "${value}" == *$'\r'* ]]; then
    release_catalog_error "${name} must be a non-empty single-line string"
    exit 1
  fi
}

safe_prefix() {
  local prefix="$1"
  [[ "${prefix}" != /* && "${prefix}" != */ && "${prefix}" != *'//' && "${prefix}" != *'..'* ]]
}

for value in RELEASE_ARTIFACT_DIRECTORY RELEASE_CANDIDATE_BUCKET RELEASE_CANDIDATE_TRAIN_SHA256 RELEASE_SOURCE_ARTIFACT_NAME GITHUB_REPOSITORY GITHUB_RUN_ID; do
  require_value "${value}" "${!value:-}"
done
RELEASE_CANDIDATE_PREFIX="${RELEASE_CANDIDATE_PREFIX:-}"
if [[ ! "${RELEASE_CANDIDATE_TRAIN_SHA256}" =~ ^[[:xdigit:]]{64}$ ]]; then
  release_catalog_error "RELEASE_CANDIDATE_TRAIN_SHA256 must be a SHA-256 hex digest"
  exit 1
fi
if [[ "${RELEASE_SOURCE_ARTIFACT_NAME}" == */* || "${RELEASE_SOURCE_ARTIFACT_NAME}" == *".."* ]]; then
  release_catalog_error "RELEASE_SOURCE_ARTIFACT_NAME must be a plain name without path separators"
  exit 1
fi
if ! safe_prefix "${RELEASE_CANDIDATE_PREFIX}"; then
  release_catalog_error "RELEASE_CANDIDATE_PREFIX must be a safe, relative S3 prefix"
  exit 1
fi

companion_root="$(realpath "${RELEASE_ARTIFACT_DIRECTORY}")"
entries_path="${companion_root}/release-catalog-entries.json"
if [[ ! -f "${entries_path}" ]]; then
  release_catalog_error "release catalog entries are missing: ${entries_path}"
  exit 1
fi

# Catalog paths are relative to the companion root. Reject traversal before an
# S3 key is constructed, even though materialize.sh already validates them.
safe_relative_path() {
  local path="$1"
  [[ -n "${path}" && "${path}" != /* && "${path}" != ../* && "${path}" != */../* && "${path}" != *'/..' ]]
}

base_key="${RELEASE_CANDIDATE_TRAIN_SHA256}/${GITHUB_REPOSITORY}/${GITHUB_RUN_ID}/${RELEASE_SOURCE_ARTIFACT_NAME}"
if [[ -n "${RELEASE_CANDIDATE_PREFIX}" ]]; then
  base_key="${RELEASE_CANDIDATE_PREFIX}/${base_key}"
fi
# Upload exactly the files the companion declares: the entries document, each
# artifact, and each artifact's evidence files.
mapfile -t companion_paths < <(jq -r '["release-catalog-entries.json"] + ([.entries[] | .path, .sbom, .provenance] | unique) | .[]' "${entries_path}")
head_metadata="$(mktemp)"
trap 'rm -f "${head_metadata}"' EXIT

upload_one() {
  local relative_path="$1"
  local local_path="${companion_root}/${relative_path}"
  local object_key="${base_key}/${relative_path}"
  local checksum
  checksum="$(openssl dgst -sha256 -binary "${local_path}" | base64)"

  if aws s3api head-object --bucket "${RELEASE_CANDIDATE_BUCKET}" --key "${object_key}" --checksum-mode ENABLED >"${head_metadata}" 2>/dev/null; then
    if [[ "$(jq -r '.ChecksumSHA256 // empty' "${head_metadata}")" != "${checksum}" ]]; then
      release_catalog_error "Filename already exists remotely and has different content from current file: ${object_key}"
      exit 1
    fi
    return
  fi

  if ! aws s3api put-object --bucket "${RELEASE_CANDIDATE_BUCKET}" --key "${object_key}" --body "${local_path}" --checksum-algorithm SHA256 --checksum-sha256 "${checksum}" --if-none-match '*' --tagging 'release-candidate-status=candidate' >/dev/null 2>&1; then
    aws s3api head-object --bucket "${RELEASE_CANDIDATE_BUCKET}" --key "${object_key}" --checksum-mode ENABLED >"${head_metadata}"
    if [[ "$(jq -r '.ChecksumSHA256 // empty' "${head_metadata}")" != "${checksum}" ]]; then
      release_catalog_error "candidate object could not be written immutably: ${object_key}"
      exit 1
    fi
  fi
}

for relative_path in "${companion_paths[@]}"; do
  if ! safe_relative_path "${relative_path}" || [[ ! -f "${companion_root}/${relative_path}" ]]; then
    release_catalog_error "catalog declares a missing or unsafe candidate file: ${relative_path}"
    exit 1
  fi
  upload_one "${relative_path}"
done

printf 'Release catalog companion: s3://%s/%s\n' "${RELEASE_CANDIDATE_BUCKET}" "${base_key}" >>"${GITHUB_STEP_SUMMARY}"
