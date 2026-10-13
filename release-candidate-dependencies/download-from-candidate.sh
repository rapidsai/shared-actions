#!/usr/bin/env bash
# Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.

set -euo pipefail

artifact_name="${1:?artifact name is required}"
if [[ ! "${artifact_name}" =~ ^[A-Za-z0-9_.-]+$ ]]; then
  echo "candidate artifact name is unsafe" >&2
  exit 1
fi

destination="${RAPIDS_UNZIP_DIR:-$(mktemp -d)}"
repository="${GITHUB_REPOSITORY:?current GitHub repository is required}"
repository_name="${repository##*/}"
run_id="${GITHUB_RUN_ID:?current GitHub run ID is required}"
run_attempt="${GITHUB_RUN_ATTEMPT:?current GitHub run attempt is required}"
train_sha256="${RELEASE_CANDIDATE_TRAIN_SHA256:?candidate train SHA-256 is required}"
if [[ ! "${run_id}" =~ ^[0-9]+$ || ! "${run_attempt}" =~ ^[0-9]+$ \
  || ! "${train_sha256}" =~ ^[0-9a-f]{64}$ ]]; then
  echo "candidate run or train identity is invalid" >&2
  exit 1
fi

train_prefix="${RELEASE_CANDIDATE_TRAIN_PREFIX:?candidate train prefix is required}/${train_sha256}"
run_prefix="${train_prefix}/${repository_name}/${run_id}.${run_attempt}"
reference_directory="$(mktemp -d)"
catalog_path="${reference_directory}/catalog.json"
index_path="${destination}/artifact-index.json"
trap 'rm -rf "${reference_directory}"' EXIT

# Resolve only the current repository's exact run and attempt. A retry or a
# similarly named output from another producer run cannot satisfy this lookup.
aws s3 cp \
  "s3://${RELEASE_CANDIDATE_BUCKET:?candidate bucket is required}/${run_prefix}/" \
  "${reference_directory}" \
  --recursive --exclude '*' --include "*/bundle-reference.${artifact_name}.json" >&2
mapfile -t references < <(find "${reference_directory}" -type f -name "bundle-reference.${artifact_name}.json" -print | sort)
if [[ "${#references[@]}" -ne 1 ]]; then
  echo "current candidate run must contain exactly one reference for ${artifact_name}" >&2
  exit 1
fi

reference="${references[0]}"
if ! jq -e \
  --arg train_sha256 "${train_sha256}" \
  --arg repository "${repository}" \
  --arg artifact_name "${artifact_name}" \
  --arg run_id "${run_id}" \
  --arg run_attempt "${run_attempt}" \
  '.schema_version == 4 and .storage_layout == 2
   and .train_sha256 == $train_sha256 and .repository == $repository
   and .source_artifact == $artifact_name and .source_run_id == $run_id
   and .source_run_attempt == $run_attempt' "${reference}" >/dev/null; then
  echo "candidate reference does not match the current train, repository, run, attempt, and artifact" >&2
  exit 1
fi

artifact_key="$(jq -r '.artifact_key // empty' "${reference}")"
build_input_digest="$(jq -r '.build_input_digest // empty' "${reference}")"
bundle_key="$(jq -r '.bundle_key // empty' "${reference}")"
artifact_index_key="$(jq -r '.artifact_index_key // empty' "${reference}")"
bundle_name="release-catalog-entries.${artifact_name}.json"
expected_artifact_key="artifacts/${repository_name}/${build_input_digest}"
if [[ "${artifact_key}" != "${expected_artifact_key}" \
  || ! "${build_input_digest}" =~ ^[0-9a-f]{64}$ \
  || "${artifact_index_key}" != "${artifact_key}/artifact-index.json" \
  || "${bundle_key}" != "${run_prefix}/${build_input_digest}/${bundle_name}" ]]; then
  echo "current candidate reference has unsafe or inconsistent storage keys" >&2
  exit 1
fi

aws s3 cp "s3://${RELEASE_CANDIDATE_BUCKET}/${bundle_key}" "${catalog_path}" >&2
if ! jq -e \
  --arg repository "${repository}" \
  --arg artifact_name "${artifact_name}" \
  --arg run_id "${run_id}" \
  --arg run_attempt "${run_attempt}" \
  --arg sha "${GITHUB_SHA:?current GitHub SHA is required}" \
  '.schema_version == 1 and .source.repository == $repository
   and .source.artifact == $artifact_name
   and .source.run_id == $run_id and .source.run_attempt == $run_attempt
   and .source.sha == $sha and (.entries | type) == "array"' \
  "${catalog_path}" >/dev/null; then
  echo "candidate catalog does not match the current repository, run, attempt, and SHA" >&2
  exit 1
fi

source_prefix="s3://${RELEASE_CANDIDATE_BUCKET}/${artifact_key}/"
# Callers capture stdout as the downloaded directory. Keep AWS diagnostics on
# stderr so they cannot become part of a Conda URL.
if ! aws s3 cp "${source_prefix}" "${destination}" --recursive >&2; then
  echo "candidate artifact is not available in this train: ${artifact_name}" >&2
  exit 1
fi

# The index is the commit marker for canonical package bytes. Validate every
# indexed path after the copy so a partial or mismatched S3 handoff fails shut.
if ! jq -e \
  --arg digest "${build_input_digest}" \
  --arg artifact_name "${artifact_name}" \
  '.schema_version == 3 and .build_input_digest == $digest
   and .source_artifact == $artifact_name and (.entries | type) == "array" and (.entries | length) > 0' \
  "${index_path}" >/dev/null; then
  echo "candidate artifact index does not match its committed reference" >&2
  exit 1
fi
while IFS= read -r entry; do
  while IFS= read -r indexed_file; do
    artifact_path="$(jq -r '.path // empty' <<<"${indexed_file}")"
    expected_sha256="$(jq -r '.sha256 // empty' <<<"${indexed_file}")"
    if [[ -z "${artifact_path}" || "${artifact_path}" == /* || "${artifact_path}" == *".."* \
      || ! "${expected_sha256}" =~ ^[0-9a-f]{64}$ ]]; then
      echo "candidate artifact index contains an unsafe path or digest" >&2
      exit 1
    fi
    downloaded_path="${destination}/${artifact_path}"
    if [[ ! -f "${downloaded_path}" ]] \
      || [[ "$(sha256sum "${downloaded_path}" | awk '{print $1}')" != "${expected_sha256}" ]]; then
      echo "candidate artifact bytes do not match the committed index: ${artifact_path}" >&2
      exit 1
    fi
  done < <(
    jq -c '([{path: .path, sha256: .sha256}]
      + (if (.sbom | type) == "object" then [{path: .sbom.path, sha256: .sbom.sha256}] else [] end))[]' \
      <<<"${entry}"
  )
done < <(jq -c '.entries[]' "${index_path}")

if [[ "${prepare_conda_channel:-false}" == "true" ]] \
  && find "${destination}" -type f \( -name '*.conda' -o -name '*.tar.bz2' \) -print -quit | grep -q .; then
  mkdir -p "${destination}"/{linux-64,linux-aarch64,noarch}
  conda index "${destination}" >&2
fi
printf '%s' "${destination}"
