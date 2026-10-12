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

for value in RELEASE_ARTIFACT_DIRECTORY RELEASE_CANDIDATE_BUCKET RELEASE_CANDIDATE_CONTENT_PREFIX RELEASE_CANDIDATE_TRAIN_PREFIX RELEASE_CANDIDATE_TRAIN_SHA256 RELEASE_CANDIDATE_BUILD_IMPLEMENTATION_REVISIONS RELEASE_CANDIDATE_GHA_TOOLS_REVISION RELEASE_CANDIDATE_CATALOG_SHARED_ACTIONS_REPOSITORY RELEASE_CANDIDATE_CATALOG_SHARED_ACTIONS_REVISION RELEASE_CANDIDATE_BUILD_INPUTS RELEASE_SOURCE_ARTIFACT_NAME GITHUB_REPOSITORY; do
  require_value "${value}" "${!value:-}"
done
if [[ ! "${GITHUB_REPOSITORY}" =~ ^[^/]+/[^/]+$ ]]; then
  echo "GITHUB_REPOSITORY must use owner/repository format" >&2
  exit 1
fi
repository_name="${GITHUB_REPOSITORY##*/}"
if [[ -n "${RAPIDS_DATETIME_STRING:-}" && ! "${RAPIDS_DATETIME_STRING}" =~ ^[0-9]{12}$ ]]; then
  echo "RAPIDS_DATETIME_STRING must use YYMMDDhhmmss format" >&2
  exit 1
fi
if [[ ! "${RELEASE_CANDIDATE_TRAIN_SHA256}" =~ ^[[:xdigit:]]{64}$ ]]; then
  echo "RELEASE_CANDIDATE_TRAIN_SHA256 must be a SHA-256 hex digest" >&2
  exit 1
fi
if [[ "${RELEASE_CANDIDATE_CATALOG_SHARED_ACTIONS_REPOSITORY}" != "rapidsai/shared-actions" ]]; then
  echo "release catalog action must execute from rapidsai/shared-actions" >&2
  exit 1
fi
build_implementation_revisions="$(jq -ceS '
  . as $revisions
  | if ($revisions | type) != "object" then error("build implementation revisions must be an object")
  elif (["gha-tools", "shared-actions", "shared-workflows"] | all(.[]; . as $name | $revisions | has($name))) | not
    then error("missing required build implementation revision")
  elif ($revisions | all(to_entries[]; (.key | type) == "string" and (.value | type) == "string" and (.value | test("^[0-9a-f]{40}$"))))
    then $revisions
  else error("invalid build implementation revision")
  end
' <<<"${RELEASE_CANDIDATE_BUILD_IMPLEMENTATION_REVISIONS}")" || {
  echo "RELEASE_CANDIDATE_BUILD_IMPLEMENTATION_REVISIONS is invalid" >&2
  exit 1
}
if [[ "${RELEASE_CANDIDATE_CATALOG_SHARED_ACTIONS_REVISION}" != "$(jq -r '."shared-actions"' <<<"${build_implementation_revisions}")" ]]; then
  echo "release catalog shared-actions revision does not match the frozen release train" >&2
  exit 1
fi
if [[ "${RELEASE_CANDIDATE_GHA_TOOLS_REVISION}" != "$(jq -r '."gha-tools"' <<<"${build_implementation_revisions}")" ]]; then
  echo "gha-tools revision does not match the frozen release train" >&2
  exit 1
fi
if [[ ! "${RELEASE_SOURCE_ARTIFACT_NAME}" =~ ^[A-Za-z0-9_-]+$ ]]; then
  echo "RELEASE_SOURCE_ARTIFACT_NAME must contain only letters, numbers, underscores, and hyphens" >&2
  exit 1
fi
if ! safe_prefix "${RELEASE_CANDIDATE_CONTENT_PREFIX}" || ! safe_prefix "${RELEASE_CANDIDATE_TRAIN_PREFIX}"; then
  echo "candidate content and train prefixes must be safe, relative S3 prefixes" >&2
  exit 1
fi
if [[ ! -f "${RELEASE_CANDIDATE_BUILD_INPUTS}" ]]; then
  echo "RELEASE_CANDIDATE_BUILD_INPUTS does not name a readable candidate input record" >&2
  exit 1
fi
candidate_build_inputs="$(jq -ceS '
  if .schema_version != 1
     or ((.train_inputs | type) != "object")
     or ((.train_inputs.lock_receipt_sha256 | type) != "string")
     or ((.train_inputs.lock_receipt_sha256 | test("^[0-9a-f]{64}$")) | not)
     or ((.train_inputs.variant_receipt_sha256 | type) != "string")
     or ((.train_inputs.variant_receipt_sha256 | test("^[0-9a-f]{64}$")) | not)
     or ((.build_policy | type) != "object")
     or ((.build_policy.sccache | type) != "boolean")
  then error("candidate build inputs must contain lock, variant, and sccache identities")
  else {
    schema_version: 1,
    train_inputs: {
      lock_receipt_sha256: .train_inputs.lock_receipt_sha256,
      variant_receipt_sha256: .train_inputs.variant_receipt_sha256
    },
    build_policy: {sccache: .build_policy.sccache}
  }
  end
' "${RELEASE_CANDIDATE_BUILD_INPUTS}")" || {
  echo "RELEASE_CANDIDATE_BUILD_INPUTS is invalid" >&2
  exit 1
}

bundle_root="$(realpath "${RELEASE_ARTIFACT_DIRECTORY}")"
entries_name="release-catalog-entries.${RELEASE_SOURCE_ARTIFACT_NAME}.json"
entries_path="${bundle_root}/${entries_name}"
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

# A reusable repository build is keyed only by its declared inputs. Package
# bytes can embed the CI timestamp, so that timestamp is an explicit input.
# The full generated catalog remains an output record and must not determine
# this key.
# A digest names canonical, reusable release bytes. Execution-specific catalog
# context and provenance stay with the train attempt instead of duplicating
# payloads below the canonical artifact namespace.
# The dependency action writes this lock after resolving only the upstream
# package bytes used by the current matrix. Keep run/attempt metadata out of
# it: a downstream rebuild is required when an actual input changes, not when
# an equivalent upstream job is retried.
upstream_inputs_path="${RELEASE_CANDIDATE_UPSTREAM_INPUTS:-}"
if [[ -z "${upstream_inputs_path}" ]]; then
  upstream_dependencies='[]'
elif [[ ! -f "${upstream_inputs_path}" ]]; then
  echo "RELEASE_CANDIDATE_UPSTREAM_INPUTS does not name a readable input lock" >&2
  exit 1
else
  upstream_dependencies="$(jq -ceS '
    if .schema_version != 1 or (.dependencies | type) != "array" then
      error("upstream input lock must contain schema_version 1 and dependencies")
    else
      [.dependencies[]
       | if (((.artifact_key | type) == "string")
            and ((.path | type) == "string")
            and ((.release_catalog_key | type) == "string")
            and ((.sha256 | type) == "string")
            and ((.sha256 | test("^[0-9a-f]{64}$")))
            and ((.producer | type) == "object")
            and ((.producer.repository | type) == "string")
            and ((.producer.sha | type) == "string")
            and ((.producer.sha | test("^[0-9a-f]{40}$")))
            and ((.producer.build_input_digest | type) == "string")
            and ((.producer.build_input_digest | test("^[0-9a-f]{64}$")))
            and ((.package | type) == "object"))
         then {artifact_key, path, release_catalog_key, producer, sha256, package}
         else error("upstream dependency has invalid identity")
         end]
      | unique_by([.release_catalog_key, .artifact_key, .path, .sha256])
      | sort_by(.release_catalog_key, .artifact_key, .path, .sha256)
    end
  ' "${upstream_inputs_path}")" || {
    echo "RELEASE_CANDIDATE_UPSTREAM_INPUTS is invalid" >&2
    exit 1
  }
fi

build_record_path="$(mktemp)"
jq -cS --argjson upstream_dependencies "${upstream_dependencies}" \
  --argjson build_implementation_revisions "${build_implementation_revisions}" \
  --argjson candidate_build_inputs "${candidate_build_inputs}" \
  --arg build_datetime "${RAPIDS_DATETIME_STRING:-}" '
  {
    schema_version: 5,
    storage_layout: 2,
    producer,
    source: (.source | {artifact, repository, sha, workflow_ref, matrix}),
    build_datetime: $build_datetime,
    build_implementation_revisions: $build_implementation_revisions,
    candidate_build_inputs: $candidate_build_inputs,
    upstream_dependencies: $upstream_dependencies,
    packages: [
      .entries[]
      | {
          release_catalog_key,
          package: (.package | {ecosystem, name, version, platform})
        }
    ] | sort_by(.release_catalog_key, .package.ecosystem, .package.name, .package.version, .package.platform)
  }
' "${entries_path}" >"${build_record_path}"
build_input_digest="$(sha256sum "${build_record_path}" | awk '{print $1}')"
attempt_id="${GITHUB_RUN_ID:-}.${GITHUB_RUN_ATTEMPT:-}"
if [[ ! "${attempt_id}" =~ ^[0-9]+\.[0-9]+$ ]]; then
  echo "GITHUB_RUN_ID and GITHUB_RUN_ATTEMPT must be numeric for a candidate upload" >&2
  exit 1
fi
artifact_key="${RELEASE_CANDIDATE_CONTENT_PREFIX}/${repository_name}/${build_input_digest}"
build_record_key="${artifact_key}/build-record.json"
artifact_index_key="${artifact_key}/artifact-index.json"
train_bundle_key="${RELEASE_CANDIDATE_TRAIN_PREFIX}/${RELEASE_CANDIDATE_TRAIN_SHA256}/${repository_name}/${attempt_id}/${build_input_digest}"
bundle_key="${train_bundle_key}/${entries_name}"
train_key="${train_bundle_key}/bundle-reference.${RELEASE_SOURCE_ARTIFACT_NAME}.json"
# Primary package bytes and deterministic SBOMs are canonical release content.
# Generated provenance identifies a specific GitHub execution, so it remains
# under the train attempt alongside the execution catalog. A signature is kept
# there as well until its determinism contract is defined.
mapfile -t artifact_paths < <(jq -r '([.entries[] | .path, .sbom | strings] | unique) | .[]' "${entries_path}")
mapfile -t execution_paths < <(jq -r '([.entries[] | .provenance, .signature? | strings] | unique) | .[]' "${entries_path}")
head_metadata="$(mktemp)"
artifact_index_entries="$(mktemp)"
artifact_index_path="$(mktemp)"
trap 'rm -f "${head_metadata}" "${build_record_path}" "${artifact_index_entries}" "${artifact_index_path}"' EXIT

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

upload_artifact() {
  local relative_path="$1"
  local local_path="${bundle_root}/${relative_path}"
  upload_immutable "${local_path}" "${artifact_key}/${relative_path}"
}

upload_execution() {
  local relative_path="$1"
  local local_path="${bundle_root}/${relative_path}"
  upload_immutable "${local_path}" "${train_bundle_key}/${relative_path}"
}

for relative_path in "${artifact_paths[@]}"; do
  if ! safe_relative_path "${relative_path}" || [[ ! -f "${bundle_root}/${relative_path}" ]]; then
    echo "catalog declares a missing or unsafe candidate file: ${relative_path}" >&2
    exit 1
  fi
done
for relative_path in "${execution_paths[@]}"; do
  if ! safe_relative_path "${relative_path}" || [[ ! -f "${bundle_root}/${relative_path}" ]]; then
    echo "catalog declares a missing or unsafe candidate file: ${relative_path}" >&2
    exit 1
  fi
done

# This is the stable artifact receipt: it identifies the canonical package
# bytes without carrying GitHub run/attempt data. A retry with different bytes
# for these declared inputs must fail the immutable write rather than create a
# second implicit release candidate.
while IFS= read -r entry; do
  primary_path="$(jq -r '.path' <<<"${entry}")"
  package="$(jq -c '.package' <<<"${entry}")"
  primary_sha256="$(sha256sum "${bundle_root}/${primary_path}" | awk '{print $1}')"
  sbom_path="$(jq -r '.sbom // empty' <<<"${entry}")"
  if [[ -n "${sbom_path}" ]]; then
    sbom_sha256="$(sha256sum "${bundle_root}/${sbom_path}" | awk '{print $1}')"
  else
    sbom_sha256=""
  fi
  jq -cn \
    --arg release_catalog_key "$(jq -r '.release_catalog_key' <<<"${entry}")" \
    --arg path "${primary_path}" \
    --arg sha256 "${primary_sha256}" \
    --arg sbom_path "${sbom_path}" \
    --arg sbom_sha256 "${sbom_sha256}" \
    --argjson package "${package}" \
    '{release_catalog_key: $release_catalog_key, path: $path, sha256: $sha256, package: $package}
     + if $sbom_path == "" then {} else {sbom: {path: $sbom_path, sha256: $sbom_sha256}} end'
done < <(jq -c '.entries[]' "${entries_path}") >"${artifact_index_entries}"
jq -csS \
  --arg build_input_digest "${build_input_digest}" \
  --arg source_artifact "${RELEASE_SOURCE_ARTIFACT_NAME}" \
  '{schema_version: 3, producer: "shared-workflows", build_input_digest: $build_input_digest,
    source_artifact: $source_artifact,
    entries: sort_by(.release_catalog_key, .path)}' \
  "${artifact_index_entries}" >"${artifact_index_path}"

upload_immutable "${build_record_path}" "${build_record_key}"
for relative_path in "${artifact_paths[@]}"; do
  upload_artifact "${relative_path}"
done
# The index is the canonical artifact commit marker. Write it only after every
# byte and deterministic SBOM that it certifies is durably present. A failed
# or racing upload may leave harmless unreferenced content, but never a
# complete-looking index for a partial candidate.
upload_immutable "${artifact_index_path}" "${artifact_index_key}"
for relative_path in "${execution_paths[@]}"; do
  upload_execution "${relative_path}"
done
upload_immutable "${entries_path}" "${bundle_key}"

reference_path="$(mktemp)"
trap 'rm -f "${head_metadata}" "${build_record_path}" "${reference_path}"' EXIT
jq -n \
  --arg train_sha256 "${RELEASE_CANDIDATE_TRAIN_SHA256}" \
  --arg repository "${GITHUB_REPOSITORY}" \
  --arg source_artifact "${RELEASE_SOURCE_ARTIFACT_NAME}" \
  --arg source_run_id "${GITHUB_RUN_ID}" \
  --arg source_run_attempt "${GITHUB_RUN_ATTEMPT}" \
  --arg build_input_digest "${build_input_digest}" \
  --arg artifact_key "${artifact_key}" \
  --arg build_record_key "${build_record_key}" \
  --arg artifact_index_key "${artifact_index_key}" \
  --arg bundle_key "${bundle_key}" \
  '{schema_version: 4, producer: "shared-workflows", storage_layout: 2, train_sha256: $train_sha256, repository: $repository, source_artifact: $source_artifact, source_run_id: $source_run_id, source_run_attempt: $source_run_attempt, build_input_digest: $build_input_digest, artifact_key: $artifact_key, build_record_key: $build_record_key, artifact_index_key: $artifact_index_key, bundle_key: $bundle_key}' \
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
printf 'Reusable candidate artifacts: s3://%s/%s\n' "${RELEASE_CANDIDATE_BUCKET}" "${artifact_key}" >>"${GITHUB_STEP_SUMMARY}"
printf 'Candidate train reference: s3://%s/%s\n' "${RELEASE_CANDIDATE_BUCKET}" "${train_key}" >>"${GITHUB_STEP_SUMMARY}"
