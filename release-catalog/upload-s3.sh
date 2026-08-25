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

for value in RELEASE_ARTIFACT_DIRECTORY RELEASE_CANDIDATE_BUCKET RELEASE_CANDIDATE_CONTENT_PREFIX RELEASE_CANDIDATE_TRAIN_PREFIX RELEASE_CANDIDATE_TRAIN_SHA256 RELEASE_SOURCE_ARTIFACT_NAME GITHUB_REPOSITORY; do
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
if ! safe_prefix "${RELEASE_CANDIDATE_CONTENT_PREFIX}" || ! safe_prefix "${RELEASE_CANDIDATE_TRAIN_PREFIX}"; then
  echo "candidate content and train prefixes must be safe, relative S3 prefixes" >&2
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

# A repository build is keyed by its generated catalog record, excluding the
# GitHub run identity. This includes the frozen source/workflow plus final
# package identity and matrix in the catalog entries. Preserve the exact
# canonical record at the resulting key so a human can explain or compare two
# digests without access to the original GitHub run.
build_record_path="$(mktemp)"
jq -cS 'del(.source.run_id, .source.run_attempt)' "${entries_path}" >"${build_record_path}"
build_input_digest="$(sha256sum "${build_record_path}" | awk '{print $1}')"
content_key="${RELEASE_CANDIDATE_CONTENT_PREFIX}/${GITHUB_REPOSITORY}/${build_input_digest}/${RELEASE_SOURCE_ARTIFACT_NAME}"
build_record_key="${content_key}/build-record.json"
bundle_key="${content_key}/release-catalog-entries.json"
train_key="${RELEASE_CANDIDATE_TRAIN_PREFIX}/${RELEASE_CANDIDATE_TRAIN_SHA256}/${GITHUB_REPOSITORY}/${RELEASE_SOURCE_ARTIFACT_NAME}/bundle-reference.json"
# `signature` is optional, so select only declared string paths. Upload the
# manifest only after every declared payload and evidence object succeeds: it
# is the S3 completion marker a collector may safely discover.
mapfile -t bundle_paths < <(jq -r '([.entries[] | .path, .sbom, .provenance, .signature? | strings] | unique) | .[]' "${entries_path}")
head_metadata="$(mktemp)"
trap 'rm -f "${head_metadata}" "${build_record_path}"' EXIT

upload_immutable() {
  local local_path="$1"
  local object_key="$2"
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

upload_one() {
  local relative_path="$1"
  local local_path="${bundle_root}/${relative_path}"
  local object_key
  if [[ "${relative_path}" == "release-catalog-entries.json" ]]; then
    object_key="${bundle_key}"
  else
    # Keep the exact bundle-relative path expected by existing RAPIDS build
    # tools. The source-artifact directory is the collision boundary, so no
    # architecture-specific storage translation is needed.
    object_key="${content_key}/${relative_path}"
  fi
  upload_immutable "${local_path}" "${object_key}"
}

for relative_path in "${bundle_paths[@]}"; do
  if ! safe_relative_path "${relative_path}" || [[ ! -f "${bundle_root}/${relative_path}" ]]; then
    echo "catalog declares a missing or unsafe candidate file: ${relative_path}" >&2
    exit 1
  fi
  upload_one "${relative_path}"
done
upload_immutable "${build_record_path}" "${build_record_key}"
upload_one "release-catalog-entries.json"

reference_path="$(mktemp)"
trap 'rm -f "${head_metadata}" "${build_record_path}" "${reference_path}"' EXIT
jq -n \
  --arg train_sha256 "${RELEASE_CANDIDATE_TRAIN_SHA256}" \
  --arg repository "${GITHUB_REPOSITORY}" \
  --arg source_artifact "${RELEASE_SOURCE_ARTIFACT_NAME}" \
  --arg build_input_digest "${build_input_digest}" \
  --arg content_key "${content_key}" \
  --arg build_record_key "${build_record_key}" \
  --arg bundle_key "${bundle_key}" \
  '{schema_version: 1, producer: "shared-workflows", train_sha256: $train_sha256, repository: $repository, source_artifact: $source_artifact, build_input_digest: $build_input_digest, content_key: $content_key, build_record_key: $build_record_key, bundle_key: $bundle_key}' \
  >"${reference_path}"

upload_reference() {
  local checksum
  checksum="$(openssl dgst -sha256 -binary "${reference_path}" | base64)"
  if aws s3api head-object --bucket "${RELEASE_CANDIDATE_BUCKET}" --key "${train_key}" --checksum-mode ENABLED >"${head_metadata}" 2>/dev/null; then
    if [[ "$(jq -r '.ChecksumSHA256 // empty' "${head_metadata}")" != "${checksum}" ]]; then
      echo "existing train reference has different bytes: ${train_key}" >&2
      exit 1
    fi
    return
  fi
  if ! aws s3api put-object --bucket "${RELEASE_CANDIDATE_BUCKET}" --key "${train_key}" --body "${reference_path}" --checksum-algorithm SHA256 --checksum-sha256 "${checksum}" --if-none-match '*' --tagging 'release-candidate-status=candidate' >/dev/null 2>&1; then
    aws s3api head-object --bucket "${RELEASE_CANDIDATE_BUCKET}" --key "${train_key}" --checksum-mode ENABLED >"${head_metadata}"
    if [[ "$(jq -r '.ChecksumSHA256 // empty' "${head_metadata}")" != "${checksum}" ]]; then
      echo "candidate train reference could not be written immutably: ${train_key}" >&2
      exit 1
    fi
  fi
}

upload_reference
printf 'Reusable candidate bundle: s3://%s/%s\n' "${RELEASE_CANDIDATE_BUCKET}" "${content_key}" >>"${GITHUB_STEP_SUMMARY}"
printf 'Candidate train reference: s3://%s/%s\n' "${RELEASE_CANDIDATE_BUCKET}" "${train_key}" >>"${GITHUB_STEP_SUMMARY}"
