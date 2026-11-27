#!/usr/bin/env bash
# Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.

# Decide whether a build may consume one train reference. Usage:
#   reference-binding.sh REFERENCE_JSON
# Prints one word:
#   bound    the reference comes from its repository's bound run (scheduler state)
#   unbound  the repository has no bound run; keep the reference as before
#   reused   a row of this run reused it from an earlier complete run
#   drop     anything else: another run's output, or a reuse this run did not choose
# A reuse binding that cannot be trusted fails instead of being dropped.
#
# A reusing row writes <repository>/reuse-bindings/<this run>/<artifact>.json in
# the train. It is accepted only for the exact source run, attempt and build
# input digest it names, and only when that source run left a controller-written
# completion record (completions/<run>.<attempt>.json) from the same or a later
# attempt in the train it was reused from, or a verification record for exactly
# this artifact (verified/<repository>/<run>.<attempt>/<artifact>.json), which
# verify writes even when the run did not complete. Reusing an earlier run of this same
# train copies nothing, so the binding then names that run's original reference,
# whose completion record is in this train.
set -euo pipefail

if (($# != 1)); then
  echo "usage: $0 REFERENCE_JSON" >&2
  exit 2
fi
reference=$1
run_id=${GITHUB_RUN_ID:?current GitHub run ID is required}
bucket=${RELEASE_CANDIDATE_BUCKET:?candidate bucket is required}
prefix=${RELEASE_CANDIDATE_TRAIN_PREFIX:?candidate train prefix is required}
train=${RELEASE_CANDIDATE_TRAIN_SHA256:?candidate train SHA-256 is required}
state=${SCHEDULER_STATE:?scheduler state path is required}
bindings=${REUSE_BINDINGS:?reuse bindings directory is required}

field() { jq -r --arg f "$1" '.[$f] // empty' "${reference}"; }
repository="$(field repository)"
artifact="$(field source_artifact)"
source_run="$(field source_run_id)"
source_attempt="$(field source_run_attempt)"
digest="$(field build_input_digest)"
repository_name="${repository##*/}"
if [[ ! "${repository_name}" =~ ^[A-Za-z0-9_.-]+$ || ! "${artifact}" =~ ^[A-Za-z0-9_-]+$ ]]; then
  echo "candidate reference names an unsafe repository or artifact" >&2
  exit 1
fi

binding="${bindings}/${repository_name}/reuse-bindings/${run_id}/${artifact}.json"
bound_run="$(jq -r --arg r "${repository_name}" '.workflow_runs[$r] // empty' "${state}")"

# A reused reference is a copy with a reuse block (reused from another train), or, when the
# earlier run is in this same train, that run's own original reference: reuse copies nothing then.
same_train_reuse=false
if [[ -f "${binding}" && "${source_run}" != "${run_id}" ]] && ! jq -e 'has("reuse")' "${reference}" >/dev/null; then
  same_train_reuse=true
fi

if [[ -f "${binding}" ]] && { jq -e 'has("reuse")' "${reference}" >/dev/null || [[ "${same_train_reuse}" == true ]]; }; then
  if [[ ! "${source_run}" =~ ^[0-9]+$ || ! "${source_attempt}" =~ ^[0-9]+$ ]]; then
    echo "reused reference for ${artifact} has a malformed source run" >&2
    exit 1
  fi
  if ! jq -e --arg t "${train}" --arg repo "${repository}" --arg a "${artifact}" --arg run "${run_id}" \
      '.schema_version == 1 and .train_sha256 == $t and .repository == $repo and .artifact == $a and .run_id == $run
       and (.source_run_id | type) == "string" and (.source_run_attempt | type) == "string"
       and (.build_input_digest | type) == "string"' "${binding}" >/dev/null; then
    echo "malformed reuse binding for ${artifact} in run ${run_id}" >&2
    exit 1
  fi
  if ! jq -e --arg run "${source_run}" --arg attempt "${source_attempt}" --arg d "${digest}" \
      '.source_run_id == $run and .source_run_attempt == $attempt and .build_input_digest == $d' \
      "${binding}" >/dev/null; then
    echo drop
    exit 0
  fi
  if [[ "${same_train_reuse}" == true ]]; then
    if [[ "$(field train_sha256)" != "${train}" ]]; then
      echo "reused reference for ${artifact} has no reuse record but belongs to another train" >&2
      exit 1
    fi
    source_train="${train}"
  else
    source_train="$(jq -r '.reuse.source_train_sha256 // empty' "${reference}")"
  fi
  if [[ ! "${source_train}" =~ ^[0-9a-f]{64}$ ]]; then
    echo "reused reference for ${artifact} names no earlier train" >&2
    exit 1
  fi
  completions="${prefix}/${source_train}/completions/${source_run}."
  record="$(mktemp)"
  trap 'rm -f "${record}"' EXIT
  while IFS= read -r key; do
    attempt="${key#"${completions}"}"
    attempt="${attempt%.json}"
    if [[ ! "${attempt}" =~ ^[0-9]+$ ]] || ((attempt < source_attempt)); then
      continue
    fi
    aws s3 cp "s3://${bucket}/${key}" "${record}" >/dev/null
    if jq -e --arg t "${source_train}" --arg run "${source_run}" --arg attempt "${attempt}" \
        '. == {schema_version: 1, train_sha256: $t, run_id: $run, run_attempt: $attempt, result: "complete"}' \
        "${record}" >/dev/null; then
      echo reused
      exit 0
    fi
  done < <(aws s3api list-objects-v2 --bucket "${bucket}" --prefix "${completions}" --output json \
    | jq -r '.Contents // [] | .[].Key')
  verified="${prefix}/${source_train}/verified/${repository_name}/${source_run}.${source_attempt}/${artifact}.json"
  if aws s3 cp "s3://${bucket}/${verified}" "${record}" >/dev/null 2>&1 \
    && jq -e --arg t "${source_train}" --arg repo "${repository_name}" --arg a "${artifact}" --arg run "${source_run}" \
      --arg attempt "${source_attempt}" --arg d "${digest}" \
      '. == {schema_version: 1, train_sha256: $t, repository: $repo, artifact: $a, run_id: $run,
             run_attempt: $attempt, build_input_digest: $d, result: "verified"}' "${record}" >/dev/null; then
    echo reused
    exit 0
  fi
  echo "reuse binding for ${artifact} names run ${source_run}, which has no completion record covering attempt ${source_attempt} and no verification record for it" >&2
  exit 1
fi

if [[ -z "${bound_run}" ]]; then
  echo unbound
elif [[ "${source_run}" != "${bound_run}" ]]; then
  echo drop
elif [[ -f "${binding}" ]]; then
  echo "${artifact} is both reused and built in run ${run_id}" >&2
  exit 1
else
  echo bound
fi
