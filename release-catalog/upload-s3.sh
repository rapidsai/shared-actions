#!/usr/bin/env bash
# Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.

# Upload only the files declared by the generated catalog document. GitHub
# Actions retains logs and this receipt, but never a second release-artifact
# copy. Conditional S3 writes make reruns safe: an existing object is accepted
# only when its checksum exactly matches the byte about to be uploaded.
set -euo pipefail

require_value() {
  local name="$1"
  local value="$2"
  if [[ -z "${value}" || "${value}" == *$'\n'* || "${value}" == *$'\r'* ]]; then
    echo "${name} must be a non-empty single-line string" >&2
    exit 1
  fi
}

safe_prefix() {
  local prefix="$1"
  [[ "${prefix}" != /* && "${prefix}" != */ && "${prefix}" != *'//' && "${prefix}" != *'..'* ]]
}

for value in RELEASE_ARTIFACT_DIRECTORY RELEASE_CANDIDATE_BUCKET RELEASE_CANDIDATE_PREFIX RELEASE_CANDIDATE_TRAIN_SHA256 RELEASE_SOURCE_ARTIFACT_NAME GITHUB_REPOSITORY GITHUB_RUN_ID; do
  require_value "${value}" "${!value:-}"
done
if [[ ! "${RELEASE_CANDIDATE_TRAIN_SHA256}" =~ ^[[:xdigit:]]{64}$ ]]; then
  echo "RELEASE_CANDIDATE_TRAIN_SHA256 must be a SHA-256 hex digest" >&2
  exit 1
fi
if [[ "${RELEASE_SOURCE_ARTIFACT_NAME}" == */* || "${RELEASE_SOURCE_ARTIFACT_NAME}" == *".."* ]]; then
  echo "RELEASE_SOURCE_ARTIFACT_NAME must be a plain bundle name" >&2
  exit 1
fi
if ! safe_prefix "${RELEASE_CANDIDATE_PREFIX}"; then
  echo "RELEASE_CANDIDATE_PREFIX must be a safe, relative S3 prefix" >&2
  exit 1
fi

bundle_root="$(realpath "${RELEASE_ARTIFACT_DIRECTORY}")"
entries_path="${bundle_root}/release-catalog-entries.json"
if [[ ! -f "${entries_path}" ]]; then
  echo "release catalog entries are missing: ${entries_path}" >&2
  exit 1
fi

# Catalog paths are relative to the build bundle. Reject traversal before an
# S3 key is constructed, even though materialize.sh already validates them.
safe_relative_path() {
  local path="$1"
  [[ -n "${path}" && "${path}" != /* && "${path}" != ../* && "${path}" != */../* && "${path}" != *'/..' ]]
}

base_key="${RELEASE_CANDIDATE_PREFIX}/${RELEASE_CANDIDATE_TRAIN_SHA256}/${GITHUB_REPOSITORY}/${GITHUB_RUN_ID}/${RELEASE_SOURCE_ARTIFACT_NAME}"
# `signature` is optional, so select only declared string paths. Without the
# filter, jq renders a missing optional value as the literal text `null` and
# the uploader attempts to find a file with that name.
mapfile -t bundle_paths < <(jq -r '["release-catalog-entries.json"] + ([.entries[] | .path, .sbom, .provenance, .signature? | strings] | unique) | .[]' "${entries_path}")
head_metadata="$(mktemp)"
trap 'rm -f "${head_metadata}"' EXIT

upload_one() {
  local relative_path="$1"
  local local_path="${bundle_root}/${relative_path}"
  local object_key="${base_key}/${relative_path}"
  local checksum
  checksum="$(openssl dgst -sha256 -binary "${local_path}" | base64)"

  if aws s3api head-object --bucket "${RELEASE_CANDIDATE_BUCKET}" --key "${object_key}" --checksum-mode ENABLED >"${head_metadata}" 2>/dev/null; then
    if [[ "$(jq -r '.ChecksumSHA256 // empty' "${head_metadata}")" != "${checksum}" ]]; then
      echo "existing candidate object has different bytes: ${object_key}" >&2
      exit 1
    fi
    return
  fi

  if ! aws s3api put-object --bucket "${RELEASE_CANDIDATE_BUCKET}" --key "${object_key}" --body "${local_path}" --checksum-algorithm SHA256 --checksum-sha256 "${checksum}" --if-none-match '*' --tagging 'release-candidate-status=candidate' >/dev/null 2>&1; then
    aws s3api head-object --bucket "${RELEASE_CANDIDATE_BUCKET}" --key "${object_key}" --checksum-mode ENABLED >"${head_metadata}"
    if [[ "$(jq -r '.ChecksumSHA256 // empty' "${head_metadata}")" != "${checksum}" ]]; then
      echo "candidate object could not be written immutably: ${object_key}" >&2
      exit 1
    fi
  fi
}

for relative_path in "${bundle_paths[@]}"; do
  if ! safe_relative_path "${relative_path}" || [[ ! -f "${bundle_root}/${relative_path}" ]]; then
    echo "catalog declares a missing or unsafe candidate file: ${relative_path}" >&2
    exit 1
  fi
  upload_one "${relative_path}"
done

printf 'Release candidate bundle: s3://%s/%s\n' "${RELEASE_CANDIDATE_BUCKET}" "${base_key}" >>"${GITHUB_STEP_SUMMARY}"
