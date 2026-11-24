#!/usr/bin/env bash
# Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.

# Print the catalog bundle key a train reference may be resolved through, or
# fail. Usage: catalog-bundle-key.sh REFERENCE_JSON RELATIVE_DIRECTORY, where
# RELATIVE_DIRECTORY is the reference's location below the current train's
# prefix (<repository>/<run>.<attempt>/<build input digest>).
#
# A reference built for this train must keep its bundle under this train. A
# reference this train reused from an earlier train keeps that train's bundle,
# beside its attempt-scoped provenance, and records the train in
# reuse.source_train_sha256. It is accepted only at exactly the same location
# in that train, and only when the current train owns the reference itself.
set -euo pipefail

if (($# != 2)); then
  echo "usage: $0 REFERENCE_JSON RELATIVE_DIRECTORY" >&2
  exit 2
fi
reference=$1
directory=$2
prefix=${RELEASE_CANDIDATE_TRAIN_PREFIX:?candidate train prefix is required}
train=${RELEASE_CANDIDATE_TRAIN_SHA256:?candidate train SHA-256 is required}

unsafe() {
  echo "candidate train contains an unsafe catalog bundle reference: $1" >&2
  exit 1
}

bundle_key="$(jq -r '.bundle_key // empty' "${reference}")"
bundle_name="${bundle_key##*/}"
if [[ ! "${bundle_name}" =~ ^release-catalog-entries\.[A-Za-z0-9_-]+\.json$ || "${bundle_key}" == *".."* ]]; then
  unsafe "malformed bundle key"
fi

if jq -e 'has("reuse")' "${reference}" >/dev/null; then
  source_train="$(jq -r '.reuse.source_train_sha256 // empty' "${reference}")"
  artifact="$(jq -r '.source_artifact // empty' "${reference}")"
  owner="$(jq -r '.train_sha256 // empty' "${reference}")"
  if [[ ! "${source_train}" =~ ^[0-9a-f]{64}$ ]]; then
    unsafe "reused reference names no earlier train"
  fi
  if [[ "${owner}" != "${train}" ]]; then
    unsafe "reused reference is not owned by this train"
  fi
  if [[ ! "${artifact}" =~ ^[A-Za-z0-9_-]+$ ]]; then
    unsafe "reused reference names no source artifact"
  fi
  if [[ ! "${directory}" =~ ^[A-Za-z0-9_.-]+/[0-9]+\.[0-9]+/[0-9a-f]{64}$ ]]; then
    unsafe "reused reference is not at a train reference location"
  fi
  expected="${prefix}/${source_train}/${directory}/release-catalog-entries.${artifact}.json"
  if [[ "${bundle_key}" != "${expected}" ]]; then
    unsafe "reused reference does not point at its earlier train's bundle"
  fi
elif [[ "${bundle_key}" != "${prefix}/${train}/"* ]]; then
  unsafe "bundle is outside this train"
fi
printf '%s\n' "${bundle_key}"
