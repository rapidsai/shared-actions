#!/usr/bin/env bash
# Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.

# Candidate dependencies remain train-scoped and private.  We first retrieve
# the small release train and catalog envelopes, then download only files for
# the current unit's declared *build* prerequisites.  In particular, this must
# never turn all completed candidate outputs into a shared package channel.
set -euo pipefail

script_directory="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# The helper is adjacent to this script but its absolute action path is dynamic.
# shellcheck disable=SC1091
source "${script_directory}/platform.sh"

require_value() {
  local name="$1"
  local value="$2"
  if [[ -z "${value}" || "${value}" == *$'\n'* || "${value}" == *$'\r'* ]]; then
    echo "${name} must be a non-empty single-line string" >&2
    exit 1
  fi
}

for value in GITHUB_ENV GITHUB_PATH RUNNER_TEMP GITHUB_REPOSITORY RELEASE_CANDIDATE_BUCKET RELEASE_CANDIDATE_TRAIN_PREFIX RELEASE_CANDIDATE_TRAIN_SHA256 RELEASE_CANDIDATE_PREPARE_CONDA_CHANNEL RELEASE_CANDIDATE_ARTIFACT_FAMILY RELEASE_CANDIDATE_ARCH RELEASE_CANDIDATE_CUDA_VERSION; do
  require_value "${value}" "${!value:-}"
done
if [[ ! "${RELEASE_CANDIDATE_TRAIN_SHA256}" =~ ^[[:xdigit:]]{64}$ ]]; then
  echo "RELEASE_CANDIDATE_TRAIN_SHA256 must be a SHA-256 digest" >&2
  exit 1
fi
if [[ "${RELEASE_CANDIDATE_PREPARE_CONDA_CHANNEL}" != "true" && "${RELEASE_CANDIDATE_PREPARE_CONDA_CHANNEL}" != "false" ]]; then
  echo "RELEASE_CANDIDATE_PREPARE_CONDA_CHANNEL must be true or false" >&2
  exit 1
fi
if [[ "${RELEASE_CANDIDATE_ARTIFACT_FAMILY}" != "conda" \
  && "${RELEASE_CANDIDATE_ARTIFACT_FAMILY}" != "wheel" \
  && "${RELEASE_CANDIDATE_ARTIFACT_FAMILY}" != "maven" \
  && "${RELEASE_CANDIDATE_ARTIFACT_FAMILY}" != "docs" \
  && "${RELEASE_CANDIDATE_ARTIFACT_FAMILY}" != "container" ]]; then
  echo "RELEASE_CANDIDATE_ARTIFACT_FAMILY must be conda, wheel, maven, docs, or container" >&2
  exit 1
fi
if [[ "${RELEASE_CANDIDATE_ARTIFACT_FAMILY}" == "wheel" && -z "${RELEASE_CANDIDATE_PYTHON_VERSION:-}" ]]; then
  echo "RELEASE_CANDIDATE_PYTHON_VERSION is required for wheel candidate dependencies" >&2
  exit 1
fi
if [[ "${RELEASE_CANDIDATE_ARTIFACT_FAMILY}" == "container" \
  && "${RELEASE_CANDIDATE_PREPARE_CONDA_CHANNEL}" != "true" ]]; then
  echo "container candidate dependencies require a private Conda channel" >&2
  exit 1
fi
candidate_unit_id="${RELEASE_CANDIDATE_UNIT_ID:-${RELEASE_CANDIDATE_ARTIFACT_FAMILY}:${GITHUB_REPOSITORY##*/}}"
implementation_mode="${RELEASE_CANDIDATE_IMPLEMENTATION_MODE:-reusable}"
if [[ "${implementation_mode}" != "reusable" && "${implementation_mode}" != "direct" ]]; then
  echo "release-candidate implementation mode must be reusable or direct" >&2
  exit 1
fi
if [[ ! "${candidate_unit_id}" =~ ^${RELEASE_CANDIDATE_ARTIFACT_FAMILY}:[A-Za-z0-9_.-]+$ ]]; then
  echo "RELEASE_CANDIDATE_UNIT_ID must use the selected family and a safe train unit name" >&2
  exit 1
fi

root="s3://${RELEASE_CANDIDATE_BUCKET}/${RELEASE_CANDIDATE_TRAIN_PREFIX}/${RELEASE_CANDIDATE_TRAIN_SHA256}"
train_key_prefix="${RELEASE_CANDIDATE_TRAIN_PREFIX}/${RELEASE_CANDIDATE_TRAIN_SHA256}"
repository="${GITHUB_REPOSITORY#*/}"
cuda_major="${RELEASE_CANDIDATE_CUDA_VERSION%%.*}"
conda_platform="$(conda_platform_for_arch "${RELEASE_CANDIDATE_ARCH}")"
wheel_platform="$(wheel_platform_for_arch "${RELEASE_CANDIDATE_ARCH}")"
workspace="${RUNNER_TEMP}/release-candidate-dependencies"
channel="${workspace}/conda-channel"
wheelhouse="${workspace}/wheelhouse"
tools="${workspace}/tools"
manifests="${workspace}/catalog-manifests"
mkdir -p "${wheelhouse}" "${tools}" "${manifests}"
if [[ "${RELEASE_CANDIDATE_PREPARE_CONDA_CHANNEL}" == "true" ]]; then
  mkdir -p "${channel}"/{linux-64,linux-aarch64,noarch}
fi

# The train is the authorization boundary.  A missing record means the
# coordinator did not stage this candidate correctly; guessing from S3 paths
# would make unrelated outputs available to the job.
aws s3 cp "${root}/release-train.json" "${workspace}/release-train.json"
actual_train_sha256="$(jq -cSj 'del(.train_sha256)' "${workspace}/release-train.json" | sha256sum | awk '{print $1}')"
if [[ "${actual_train_sha256}" != "${RELEASE_CANDIDATE_TRAIN_SHA256}" ]]; then
  echo "stored release train does not match RELEASE_CANDIDATE_TRAIN_SHA256" >&2
  exit 1
fi
rapids_cmake_sha="$(jq -r '.build_tool_revisions["rapids-cmake"] // empty' "${workspace}/release-train.json")"
if [[ ! "${rapids_cmake_sha}" =~ ^[[:xdigit:]]{40}$ ]]; then
  echo "release train must lock rapids-cmake with build_tool_revisions.rapids-cmake; regenerate the train" >&2
  exit 1
fi
build_implementation_revisions="$(jq -ceS '
  .build_implementation_revisions as $revisions
  | if ($revisions | type) != "object" then error("build implementation revisions must be an object")
    elif (["gha-tools", "shared-actions", "shared-workflows"] | all(.[]; . as $name | $revisions | has($name))) | not
      then error("missing required build implementation revision")
    elif ($revisions | all(to_entries[]; (.key | type) == "string" and (.value | type) == "string" and (.value | test("^[0-9a-f]{40}$"))))
      then $revisions
    else error("invalid build implementation revision")
    end
' "${workspace}/release-train.json")" || {
  echo "release train must lock shared-workflows, shared-actions, and gha-tools; regenerate the train" >&2
  exit 1
}
build_implementation_repositories="$(jq -ceS '
  (.build_implementation_repositories // {
    "gha-tools": "rapidsai/gha-tools",
    "shared-actions": "rapidsai/shared-actions",
    "shared-workflows": "rapidsai/shared-workflows"
  }) as $repositories
  | if ($repositories | type) != "object" then error("build implementation repositories must be an object")
    elif (["gha-tools", "shared-actions", "shared-workflows"] | sort) != ($repositories | keys | sort)
      then error("build implementation repositories must name exactly the supported tools")
    elif ($repositories | all(to_entries[];
      (.key | type) == "string"
      and (.value | type) == "string"
      and (.value | test("^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$"))))
      and ($repositories["gha-tools"] | endswith("/gha-tools"))
      and ($repositories["shared-actions"] | endswith("/shared-actions"))
      and ($repositories["shared-workflows"] | endswith("/shared-workflows"))
      then $repositories
    else error("invalid build implementation repository")
    end
' "${workspace}/release-train.json")" || {
  echo "release train has invalid build_implementation_repositories" >&2
  exit 1
}
gha_tools_revision="$(jq -r '."gha-tools"' <<<"${build_implementation_revisions}")"
shared_actions_revision="$(jq -r '."shared-actions"' <<<"${build_implementation_revisions}")"
shared_workflows_revision="$(jq -r '."shared-workflows"' <<<"${build_implementation_revisions}")"
candidate_sccache="$(jq -r '
  if ((.candidate_build_policy | type) == "object")
     and ((.candidate_build_policy.sccache | type) == "boolean")
  then .candidate_build_policy.sccache
  else empty
  end
' "${workspace}/release-train.json")"
if [[ "${candidate_sccache}" != "true" && "${candidate_sccache}" != "false" ]]; then
  echo "release train must declare candidate_build_policy.sccache as true or false; regenerate the train" >&2
  exit 1
fi
candidate_release_version="$(jq -er '.release_version | select(type == "string" and test("^[0-9]{2}\\.[0-9]{2}$"))' "${workspace}/release-train.json")" || {
  echo "release train has no valid release_version" >&2
  exit 1
}
source_manifest="${workspace}/source-manifest.json"
python3 "${script_directory}/write_source_manifest.py" \
  --train "${workspace}/release-train.json" --output "${source_manifest}"

require_expected_revision() {
  local label="$1"
  local expected_repository="$2"
  local expected_revision="$3"
  local actual_repository="$4"
  local actual_revision="$5"
  local usage_repository="$6"
  if [[ "${actual_repository}" != "${expected_repository}" || "${actual_revision}" != "${expected_revision}" ]]; then
    echo "${label} usage in ${usage_repository} does not match the frozen release train: train expects ${expected_repository}@${expected_revision:0:8}, got ${actual_repository}@${actual_revision:0:8}" >&2
    exit 1
  fi
}

require_expected_revision \
  "shared-actions" \
  "$(jq -r '."shared-actions"' <<<"${build_implementation_repositories}")" \
  "${shared_actions_revision}" \
  "${RELEASE_CANDIDATE_SHARED_ACTIONS_REPOSITORY:-}" \
  "${RELEASE_CANDIDATE_SHARED_ACTIONS_REVISION:-}" \
  "${RELEASE_CANDIDATE_SHARED_WORKFLOWS_REPOSITORY:-${GITHUB_REPOSITORY}}"
if [[ "${implementation_mode}" == "reusable" ]]; then
  require_expected_revision \
    "shared-workflows" \
    "$(jq -r '."shared-workflows"' <<<"${build_implementation_repositories}")" \
    "${shared_workflows_revision}" \
    "${RELEASE_CANDIDATE_SHARED_WORKFLOWS_REPOSITORY:-}" \
    "${RELEASE_CANDIDATE_SHARED_WORKFLOWS_REVISION:-}" \
    "${GITHUB_REPOSITORY}"
fi

if ! command -v git >/dev/null; then
  echo "git is required to install the train-locked gha-tools revision" >&2
  exit 1
fi
gha_tools_directory="${workspace}/gha-tools"
git init --quiet "${gha_tools_directory}"
gha_tools_repository="$(jq -r '."gha-tools"' <<<"${build_implementation_repositories}")"
git -C "${gha_tools_directory}" remote add origin "https://github.com/${gha_tools_repository}.git"
git -C "${gha_tools_directory}" fetch --quiet --depth 1 origin "${gha_tools_revision}"
git -C "${gha_tools_directory}" checkout --quiet --detach FETCH_HEAD
actual_gha_tools_revision="$(git -C "${gha_tools_directory}" rev-parse HEAD)"
if [[ "${actual_gha_tools_revision}" != "${gha_tools_revision}" ]]; then
  echo "gha-tools checkout does not match the frozen release train: expected ${gha_tools_revision}, got ${actual_gha_tools_revision}" >&2
  exit 1
fi
PATH="${gha_tools_directory}/tools:${PATH}"
# Candidate jobs freeze the package version in the source checkout's VERSION
# file. Prefer that value over the Git-distance version computed by
# gha-tools, while leaving non-candidate/custom invocations unchanged.
bash "${script_directory}/install-version-override.sh" "${tools}"
# scikit-build-core may use the CMake wheel rather than resolving `cmake` from
# PATH, so its configure call can bypass the candidate CMake wrapper below.
# Its supported environment configuration is additive and reaches both the
# bundled and system CMake implementations.  Put the immutable train value
# last so it wins over an inherited generic definition of the same cache key.
skbuild_cmake_define="rapids-cmake-sha=${rapids_cmake_sha}"
if [[ -n "${SKBUILD_CMAKE_DEFINE:-}" ]]; then
  require_value "SKBUILD_CMAKE_DEFINE" "${SKBUILD_CMAKE_DEFINE}"
  skbuild_cmake_define="${SKBUILD_CMAKE_DEFINE};${skbuild_cmake_define}"
fi

# Schema-2 trains keep generated input evidence in compact, checksum-addressed
# receipts. Download those before resolving this job's lock packages. Older
# trains retain the inline fields during the migration period.
if ! jq -e '.input_receipts | type == "object"' "${workspace}/release-train.json" >/dev/null; then
  echo "release train must contain checksum-addressed input receipts; regenerate the train" >&2
  exit 1
fi
lock_receipt_sha256=""
variant_receipt_sha256=""
for receipt_name in locks variants; do
  receipt_path="$(jq -r --arg name "${receipt_name}" '.input_receipts[$name].path // empty' "${workspace}/release-train.json")"
  receipt_sha256="$(jq -r --arg name "${receipt_name}" '.input_receipts[$name].sha256 // empty' "${workspace}/release-train.json")"
  if [[ -z "${receipt_path}" || "${receipt_path}" == /* || "${receipt_path}" == *".."* || ! "${receipt_sha256}" =~ ^[[:xdigit:]]{64}$ ]]; then
    echo "release train contains an unsafe ${receipt_name} receipt reference" >&2
    exit 1
  fi
  receipt_file="${workspace}/${receipt_name}-receipt.json"
  aws s3 cp "${root}/inputs/${receipt_path}" "${receipt_file}"
  if [[ "$(sha256sum "${receipt_file}" | awk '{print $1}')" != "${receipt_sha256}" ]]; then
    echo "stored ${receipt_name} receipt does not match the release train" >&2
    exit 1
  fi
  if [[ "${receipt_name}" == "locks" ]]; then
    lock_receipt_sha256="${receipt_sha256}"
  else
    variant_receipt_sha256="${receipt_sha256}"
  fi
done
lock_source="${workspace}/locks-receipt.json"
variant_source="${workspace}/variants-receipt.json"
candidate_build_inputs="${workspace}/candidate-build-inputs.json"

if [[ "${RELEASE_CANDIDATE_PREPARE_CONDA_CHANNEL}" == "true" ]]; then
  mapfile -t bootstrap_records < <(jq -cer '.bootstrap_conda[]?' "${lock_source}")
  bootstrap_packages="$(printf '%s\n' "${bootstrap_records[@]}" | jq -Rsc 'split("\n") | map(select(length > 0) | fromjson | .package) | sort')"
  if [[ "${bootstrap_packages}" != '["rapids-build-backend","rapids-dependency-file-generator"]' ]]; then
    echo "release train must contain the complete Conda bootstrap tool bundle" >&2
    exit 1
  fi
  for record in "${bootstrap_records[@]}"; do
    bootstrap_path="$(jq -r '.path // empty' <<<"${record}")"
    bootstrap_sha256="$(jq -r '.sha256 // empty' <<<"${record}")"
    bootstrap_filename="${bootstrap_path##*/}"
    if [[ "${bootstrap_path}" != bootstrap-channel/noarch/* \
      || "${bootstrap_filename}" == *".."* \
      || ! "${bootstrap_filename}" =~ \.(conda|tar\.bz2)$ \
      || ! "${bootstrap_sha256}" =~ ^[[:xdigit:]]{64}$ ]]; then
      echo "release train contains an unsafe Conda bootstrap package reference" >&2
      exit 1
    fi
    bootstrap_destination="${channel}/noarch/${bootstrap_filename}"
    aws s3 cp "${root}/inputs/${bootstrap_path}" "${bootstrap_destination}"
    if [[ "$(sha256sum "${bootstrap_destination}" | awk '{print $1}')" != "${bootstrap_sha256}" ]]; then
      echo "stored Conda bootstrap package does not match the release train: ${bootstrap_filename}" >&2
      exit 1
    fi
  done
fi

wheel_constraint=""
if [[ "${RELEASE_CANDIDATE_ARTIFACT_FAMILY}" == "wheel" ]]; then
  wheel_lock_record="$(python3 "${script_directory}/select_lock.py" \
    --locks "${lock_source}" \
    --platform "${wheel_platform}" \
    --cuda-major "${cuda_major}" \
    --python-version "${RELEASE_CANDIDATE_PYTHON_VERSION}")"
  wheel_lock_path="$(jq -r '.path' <<<"${wheel_lock_record}")"
  wheel_lock_sha256="$(jq -r '.sha256' <<<"${wheel_lock_record}")"
  wheel_constraint="${workspace}/wheel-constraints.txt"
  aws s3 cp "${root}/inputs/${wheel_lock_path}" "${wheel_constraint}"
  if [[ "$(sha256sum "${wheel_constraint}" | awk '{print $1}')" != "${wheel_lock_sha256}" ]]; then
    echo "stored wheel lock does not match the release train" >&2
    exit 1
  fi
fi
# The scheduler binds each repository to one exact GitHub run.  Candidate
# references are append-only across retries, so consuming that binding avoids
# accidentally mixing a prior failed attempt into this build's dependencies.
scheduler_state="${workspace}/scheduler-state.json"
if ! aws s3 cp "${root}/scheduler-state.json" "${scheduler_state}" 2>/dev/null; then
  printf '{"workflow_runs":{}}\n' >"${scheduler_state}"
fi

# Train records are metadata, not package payloads. Each one names canonical
# reusable release bytes plus an attempt-specific catalog/provenance envelope.
# Resolve its bundle descriptors before downloading only the catalog envelopes
# needed to choose build dependencies.
aws s3 cp "${root}/" "${manifests}" --recursive --exclude '*' --include '*/bundle-reference.*.json'
shopt -s globstar nullglob
reference_paths=("${manifests}"/**/bundle-reference.*.json)
declare -A selected_attempts=()
declare -A selected_reference_paths=()
for reference_path in "${reference_paths[@]}"; do
  if ! jq -e '.schema_version == 4 and .storage_layout == 2' "${reference_path}" >/dev/null; then
    echo "candidate train contains a reference that does not use storage layout 2" >&2
    exit 1
  fi
  source_repository="$(jq -r '.repository // empty' "${reference_path}")"
  source_artifact="$(jq -r '.source_artifact // empty' "${reference_path}")"
  if [[ "${reference_path##*/}" != "bundle-reference.${source_artifact}.json" ]]; then
    echo "candidate train contains a reference whose filename does not match its source artifact" >&2
    exit 1
  fi
  source_run_id="$(jq -r '.source_run_id // empty' "${reference_path}")"
  source_run_attempt="$(jq -r '.source_run_attempt // empty' "${reference_path}")"
  repository_name="${source_repository##*/}"
  selected_run_id="$(jq -r --arg repository "${repository_name}" '.workflow_runs[$repository] // empty' "${scheduler_state}")"
  if [[ -n "${selected_run_id}" && "${source_run_id}" != "${selected_run_id}" ]]; then
    rm -f "${reference_path}"
    continue
  fi
  reference_identity="${source_repository}/${source_artifact}"
  if [[ -n "${selected_run_id}" ]]; then
    if [[ ! "${source_run_attempt}" =~ ^[0-9]+$ ]]; then
      echo "candidate train has an invalid selected run attempt" >&2
      exit 1
    fi
    if [[ -n "${selected_attempts[${reference_identity}]:-}" ]] \
      && (( source_run_attempt < selected_attempts[${reference_identity}] )); then
      rm -f "${reference_path}"
      continue
    fi
    if [[ -n "${selected_reference_paths[${reference_identity}]:-}" ]]; then
      rm -f "${selected_reference_paths[${reference_identity}]}"
    fi
    selected_attempts[${reference_identity}]="${source_run_attempt}"
    selected_reference_paths[${reference_identity}]="${reference_path}"
  fi
  artifact_key="$(jq -r '.artifact_key // empty' "${reference_path}")"
  if [[ -z "${artifact_key}" || ! "${artifact_key}" =~ ^artifacts/[^/]+/[0-9a-f]{64}$ ]]; then
    echo "candidate train contains an unsafe artifact reference" >&2
    exit 1
  fi
  build_input_digest="$(jq -r '.build_input_digest // empty' "${reference_path}")"
  if [[ -z "${build_input_digest}" || ! "${build_input_digest}" =~ ^[0-9a-f]{64}$ ]]; then
    echo "candidate train contains a missing or unsafe producer build-input digest" >&2
    exit 1
  fi
  bundle_key="$(jq -r '.bundle_key // empty' "${reference_path}")"
  bundle_name="${bundle_key##*/}"
  if [[ ! "${bundle_name}" =~ ^release-catalog-entries\.[A-Za-z0-9_-]+\.json$ ]] \
    || [[ "${bundle_key}" != "${train_key_prefix}"/* ]] \
    || [[ "${bundle_key}" == *".."* ]]; then
    echo "candidate train contains an unsafe catalog bundle reference" >&2
    exit 1
  fi
  manifest_path="${reference_path%/*}/${bundle_name}"
  aws s3 cp "s3://${RELEASE_CANDIDATE_BUCKET}/${bundle_key}" "${manifest_path}"
  jq \
    --arg artifact_key "${artifact_key}" \
    --arg build_input_digest "${build_input_digest}" \
    '. + {artifact_key: $artifact_key, build_input_digest: $build_input_digest}' \
    "${manifest_path}" >"${manifest_path}.tmp"
  mv "${manifest_path}.tmp" "${manifest_path}"
done

repository="${GITHUB_REPOSITORY##*/}"
target_unit="${candidate_unit_id}"
selected="${workspace}/selected-artifacts.json"
resolved_inputs="${workspace}/resolved-upstream-inputs.jsonl"
upstream_inputs="${workspace}/upstream-inputs.json"
: >"${resolved_inputs}"
if ! jq -e --arg target "${target_unit}" '.release_units[] | select(.id == $target and .artifact_family != "source")' \
  "${workspace}/release-train.json" >/dev/null; then
  echo "release train has no candidate build unit for ${target_unit}" >&2
  exit 1
fi
candidate_source_inputs="${workspace}/candidate-source-inputs.json"
bash "${script_directory}/write-source-inputs.sh" \
  --train "${workspace}/release-train.json" \
  --output "${candidate_source_inputs}" \
  --target-unit "${target_unit}"
jq -cnS \
  --arg lock_receipt_sha256 "${lock_receipt_sha256}" \
  --arg variant_receipt_sha256 "${variant_receipt_sha256}" \
  --argjson build_implementation_repositories "${build_implementation_repositories}" \
  --argjson source_inputs "$(cat "${candidate_source_inputs}")" \
  --argjson sccache "${candidate_sccache}" \
  '{schema_version: 1,
    train_inputs: {
      lock_receipt_sha256: $lock_receipt_sha256,
      variant_receipt_sha256: $variant_receipt_sha256
    },
    build_policy: {sccache: $sccache},
    build_implementation_repositories: $build_implementation_repositories,
    source_inputs: $source_inputs}' >"${candidate_build_inputs}"

# Shell globstar has a clear, portable meaning here and keeps the jq program
# focused on schema fields rather than filesystem traversal.
manifest_paths=("${manifests}"/**/release-catalog-entries.*.json)
dependency_count="$(jq -r --arg target "${target_unit}" '
  (.release_units | map({key: .id, value: .}) | from_entries) as $units
  | def closure($id):
      ($units[$id].dependencies // [])
      | map(select($units[.] != null and $units[.].artifact_family != "source") | ., closure(.))
      | flatten | unique;
  closure($target) | length
' "${workspace}/release-train.json")"
if [[ "${#manifest_paths[@]}" -eq 0 ]]; then
  if [[ "${dependency_count}" -ne 0 ]]; then
    echo "no completed candidate catalog envelopes found for ${target_unit}'s declared dependencies" >&2
    exit 1
  fi
  printf '[]\n' >"${workspace}/catalog-records.json"
else
  jq -s '.' "${manifest_paths[@]}" >"${workspace}/catalog-records.json"
fi

# Package metadata determines compatibility. A producing job's matrix only
# describes how the package was made: rapids-logger, for example, can be built
# in a CUDA 13 job but has no CUDA dependency and is valid for CUDA 12. Python
# compatibility is intentionally left to the published wheel tag and pip.
jq -n \
  --slurpfile train "${workspace}/release-train.json" \
  --arg target "${target_unit}" \
  --arg family "${RELEASE_CANDIDATE_ARTIFACT_FAMILY}" \
  --arg arch "${RELEASE_CANDIDATE_ARCH}" \
  --arg cuda_major "${cuda_major}" \
  --arg python_version "${RELEASE_CANDIDATE_PYTHON_VERSION}" \
  --arg conda_platform "${conda_platform}" \
  --slurpfile records "${workspace}/catalog-records.json" \
  '
    ($train[0].release_units | map({key: .id, value: .}) | from_entries) as $units
    | def closure($id):
        ($units[$id].dependencies // [])
        | map(select($units[.] != null and $units[.].artifact_family != "source") | ., closure(.))
        | flatten | unique;
    closure($target) as $dependencies
    | (if $family == "docs" or $family == "container" or $family == "maven" then ["conda:", "maven:"] else [$family + ":"] end) as $family_prefixes
    | [ $dependencies[] | select(. as $dependency | $family_prefixes | any(startswith($dependency))) ] as $family_dependencies
    | [ $records[0][]
        | . as $record
        | .entries[]
        | select(.release_catalog_key as $key | $family_dependencies | index($key))
        # Older envelopes have no cuda_major. Treating that as compatible is a
        # deliberate transition rule; new Conda envelopes set it only when the
        # package declares a cuda-version dependency in info/index.json.
        | select((.package.cuda_major // $cuda_major) == $cuda_major)
        | select(
            $family != "conda"
            or (.package.platform == "noarch" or .package.platform == $conda_platform)
          )
        | select(
            ($family != "maven" and $family != "docs" and $family != "container")
            or (.package.ecosystem != "maven")
            or (.package.platform == ($arch + "-cuda" + $cuda_major))
          )
        | {
          release_catalog_key: .release_catalog_key,
            source: $record.source,
            artifact_key: $record.artifact_key,
            producer_build_input_digest: $record.build_input_digest,
            path: .path,
            package: .package
          }
      ]
    | sort_by([.package.name, .package.version, if .source.matrix.python_version == $python_version then 0 else 1 end])
    | group_by([.package.name, .package.version, .package.build, .package.platform, .path])
    | map(.[0])
  ' >"${selected}"

if [[ "$(jq 'length' "${selected}")" -eq 0 ]]; then
  # A root node has no upstream candidate packages. Its own C++/Python
  # intermediate artifacts are still served by the exact-name wrapper below.
  echo "no upstream candidate package dependencies declared for ${target_unit}"
fi

record_resolved_input() {
  local selection="$1"
  local downloaded_path="$2"
  local artifact_key artifact_path release_catalog_key package sha256
  local producer_repository producer_sha producer_build_input_digest
  artifact_key="$(jq -r '.artifact_key' <<<"${selection}")"
  artifact_path="$(jq -r '.path' <<<"${selection}")"
  release_catalog_key="$(jq -r '.release_catalog_key // empty' <<<"${selection}")"
  producer_repository="$(jq -r '.source.repository // empty' <<<"${selection}")"
  producer_sha="$(jq -r '.source.sha // empty' <<<"${selection}")"
  producer_build_input_digest="$(jq -r '.producer_build_input_digest // empty' <<<"${selection}")"
  package="$(jq -c '.package' <<<"${selection}")"
  sha256="$(sha256sum "${downloaded_path}" | awk '{print $1}')"
  jq -cn \
    --arg artifact_key "${artifact_key}" \
    --arg path "${artifact_path}" \
    --arg release_catalog_key "${release_catalog_key}" \
    --arg producer_repository "${producer_repository}" \
    --arg producer_sha "${producer_sha}" \
    --arg producer_build_input_digest "${producer_build_input_digest}" \
    --arg sha256 "${sha256}" \
    --argjson package "${package}" \
    '{artifact_key: $artifact_key, path: $path, release_catalog_key: $release_catalog_key,
      producer: {repository: $producer_repository, sha: $producer_sha,
        build_input_digest: $producer_build_input_digest},
      sha256: $sha256, package: $package}' >>"${resolved_inputs}"
}

while IFS= read -r selection; do
  artifact_key="$(jq -r '.artifact_key // empty' <<<"${selection}")"
  artifact_path="$(jq -r '.path' <<<"${selection}")"
  if [[ -z "${artifact_key}" || ! "${artifact_key}" =~ ^artifacts/[^/]+/[0-9a-f]{64}$ || "${artifact_path}" == /* || "${artifact_path}" == *".."* ]]; then
    echo "candidate catalog contains an unsafe dependency location" >&2
    exit 1
  fi
  # Canonical candidate storage preserves the producer's bundle-relative
  # layout without tying dependency retrieval to a GitHub run attempt.
  source="s3://${RELEASE_CANDIDATE_BUCKET}/${artifact_key}/${artifact_path}"
  case "${artifact_path}" in
    *.conda|*.tar.bz2)
      if [[ "${RELEASE_CANDIDATE_PREPARE_CONDA_CHANNEL}" != "true" ]]; then
        echo "train declares a Conda dependency but prepare-conda-channel is false" >&2
        exit 1
      fi
      platform="$(jq -r '.package.platform // ""' <<<"${selection}")"
      case "${platform}" in
        linux-64|linux-aarch64|noarch) destination="${channel}/${platform}" ;;
        *) echo "candidate Conda package has unsupported platform: ${platform}" >&2; exit 1 ;;
      esac
      aws s3 cp "${source}" "${destination}/$(basename "${artifact_path}")"
      record_resolved_input "${selection}" "${destination}/$(basename "${artifact_path}")"
      ;;
    *.whl)
      if ! python3 "$(dirname "$0")/select_wheel.py" \
        --python-version "${RELEASE_CANDIDATE_PYTHON_VERSION}" "$(basename "${artifact_path}")"; then
        continue
      fi
      aws s3 cp "${source}" "${wheelhouse}/$(basename "${artifact_path}")"
      record_resolved_input "${selection}" "${wheelhouse}/$(basename "${artifact_path}")"
      ;;
    *.tar.gz)
      if [[ "${RELEASE_CANDIDATE_ARTIFACT_FAMILY}" != "maven" \
        && "${RELEASE_CANDIDATE_ARTIFACT_FAMILY}" != "docs" \
        && "${RELEASE_CANDIDATE_ARTIFACT_FAMILY}" != "container" ]]; then
        echo "candidate archive dependency is unsupported for ${RELEASE_CANDIDATE_ARTIFACT_FAMILY}: ${artifact_path}" >&2
        exit 1
      fi
      archive_discriminator="$(printf '%s' "${artifact_key}/${artifact_path}" | sha256sum | cut -c1-16)"
      archive_path="${workspace}/${archive_discriminator}-$(basename "${artifact_path}")"
      aws s3 cp "${source}" "${archive_path}"
      record_resolved_input "${selection}" "${archive_path}"
      if [[ "$(jq -r '.package.ecosystem' <<<"${selection}")" == "maven" ]]; then
        maven_repository="${workspace}/maven-repository"
        python3 "${script_directory}/extract_maven_repository.py" \
          --archive "${archive_path}" --destination "${maven_repository}"
      fi
      ;;
    *) echo "candidate catalog has unsupported package type: ${artifact_path}" >&2; exit 1 ;;
  esac
done < <(jq -c '.[]' "${selected}")

# The uploader uses this lock as part of the repository build-input digest. It
# records only the exact upstream package bytes materialized for this matrix,
# never their GitHub execution IDs. Sorting makes the lock independent of S3
# listing order and avoids rebuilding when an equivalent dependency view is
# prepared again.
jq -sS \
  '{schema_version: 1,
    dependencies: (unique_by([.release_catalog_key, .artifact_key, .path, .sha256])
      | sort_by(.release_catalog_key, .artifact_key, .path, .sha256))}' \
  "${resolved_inputs}" >"${upstream_inputs}"

index_candidate_conda_channel() {
  bash "${script_directory}/index-conda-channel.sh" "${channel}"
}

if [[ "${RELEASE_CANDIDATE_PREPARE_CONDA_CHANNEL}" == "true" ]]; then
  index_candidate_conda_channel
fi

original_download="$(command -v rapids-download-from-github || true)"
original_rattler="$(command -v rapids-rattler-channel-string || true)"
original_rattler_build="$(command -v rattler-build || true)"
original_cmake="$(command -v cmake || true)"
if [[ -z "${original_download}" ]]; then
  echo "RAPIDS gha-tools must be installed before candidate dependency setup" >&2
  exit 1
fi
if [[ "${RELEASE_CANDIDATE_PREPARE_CONDA_CHANNEL}" == "true" \
  && "${RELEASE_CANDIDATE_ARTIFACT_FAMILY}" != "container" \
  && "${RELEASE_CANDIDATE_ARTIFACT_FAMILY}" != "docs" \
  && "${RELEASE_CANDIDATE_ARTIFACT_FAMILY}" != "maven" \
  && -z "${original_rattler}" ]]; then
  echo "rapids-rattler-channel-string is required for a candidate Conda build" >&2
  exit 1
fi
if [[ "${RELEASE_CANDIDATE_PREPARE_CONDA_CHANNEL}" == "true" \
  && "${RELEASE_CANDIDATE_ARTIFACT_FAMILY}" != "container" \
  && "${RELEASE_CANDIDATE_ARTIFACT_FAMILY}" != "docs" \
  && "${RELEASE_CANDIDATE_ARTIFACT_FAMILY}" != "maven" \
  && -z "${original_rattler_build}" ]]; then
  echo "rattler-build is required for a candidate Conda build" >&2
  exit 1
fi
# A lock metapackage has no payload. Its run constraints are nevertheless part
# of the build/host solve once this package is injected into a recipe. Keeping
# it in the job-local candidate channel avoids publishing CI-only lock packages.
if [[ "${RELEASE_CANDIDATE_PREPARE_CONDA_CHANNEL}" == "true" \
  && "${RELEASE_CANDIDATE_ARTIFACT_FAMILY}" != "container" \
  && "${RELEASE_CANDIDATE_ARTIFACT_FAMILY}" != "docs" \
  && "${RELEASE_CANDIDATE_ARTIFACT_FAMILY}" != "maven" ]]; then
  lock_workspace="${workspace}/train-locks"
  mkdir -p "${lock_workspace}"

  prepare_lock_metapackage() {
    local section="$1"
    local record relative_path expected_sha256 lock_path actual_sha256 constraints package_name recipe activation_script package_script
    record="$(jq -cer \
      --arg repository "${repository}" \
      --arg platform "${conda_platform}" \
      --arg cuda "${cuda_major}" \
      --arg environment "${section}" \
      '[ (.conda // .input_locks.conda)[]
         | select(.scope.repository == $repository
                  and .scope.platform == $platform
                  and .scope.cuda == $cuda
                  and .scope.environment == $environment) ]
       | if length == 1 then .[0]
         else error("release train must contain exactly one repository-scoped Conda lock")
         end' \
      "${lock_source}")"
    relative_path="$(jq -r '.path' <<<"${record}")"
    expected_sha256="$(jq -r '.sha256' <<<"${record}")"
    if [[ "${relative_path}" == /* || "${relative_path}" == *".."* || ! "${expected_sha256}" =~ ^[[:xdigit:]]{64}$ ]]; then
      echo "release train contains an unsafe ${section} lock reference" >&2
      exit 1
    fi
    lock_path="${lock_workspace}/${section}-constraints.txt"
    # The enclosing function is called through command substitution. AWS also
    # writes transfer progress to stdout, so keep it out of the captured
    # package name for the later one-line GITHUB_ENV assignment.
    aws s3 cp "${root}/inputs/${relative_path}" "${lock_path}" >&2
    actual_sha256="$(sha256sum "${lock_path}" | awk '{print $1}')"
    if [[ "${actual_sha256}" != "${expected_sha256}" ]]; then
      echo "stored ${section} lock does not match the release train" >&2
      exit 1
    fi
    constraints="$(jq -Rsc 'split("\n") | map(select(length > 0))' "${lock_path}")"
    package_name="rapids-release-lock-${RELEASE_CANDIDATE_TRAIN_SHA256:0:12}-${conda_platform}-cuda${cuda_major}-${section}"
    recipe="${lock_workspace}/${section}-recipe.yaml"
    # Rattler-Build activates build and host prefixes in an isolated shell.
    # Compiler activation creates CMAKE_ARGS there, after the outer candidate
    # action has finished. Put the frozen RAPIDS CMake definition in each lock
    # package's late-named activation script so ordinary Conda CMake builds
    # receive it as well as scikit-build-core and PATH-based invocations.
    activation_script="if [[ \" \${CMAKE_ARGS:-} \" != *\" -Drapids-cmake-sha=${rapids_cmake_sha} \"* ]]; then export CMAKE_ARGS=\"\${CMAKE_ARGS:+\${CMAKE_ARGS} }-Drapids-cmake-sha=${rapids_cmake_sha}\"; fi"
    package_script=$'mkdir -p "${PREFIX}/etc/conda/activate.d"\n'
    package_script+="printf '%s\\n' '${activation_script}' > \"\${PREFIX}/etc/conda/activate.d/zz-rapids-release-cmake.sh\""
    jq -n \
      --arg name "${package_name}" \
      --argjson constraints "${constraints}" \
      --arg script "${package_script}" \
      '{schema_version: 1, package: {name: $name, version: "0"}, build: {number: 0, noarch: "generic", script: $script}, requirements: {run_constraints: $constraints}}' \
      >"${recipe}"
    # This function is called through command substitution. Keep Rattler's
    # human build log on stderr so the captured value is exactly the package
    # name, which can safely be written as one GITHUB_ENV assignment.
    "${original_rattler_build}" build --recipe "${recipe}" --output-dir "${channel}" --color never >&2
    printf '%s' "${package_name}"
  }

    lock_build_package="$(prepare_lock_metapackage build)"
    lock_host_package="$(prepare_lock_metapackage host)"
    variant_record="$(jq -cer 'if has("configuration_path") then {path: .configuration_path, sha256: .configuration_sha256} else .input_variants | {path: .configuration_path, sha256: .configuration_sha256} end' "${variant_source}")"
    variant_path="$(jq -r '.path' <<<"${variant_record}")"
    variant_sha256="$(jq -r '.sha256' <<<"${variant_record}")"
    if [[ "${variant_path}" == /* || "${variant_path}" == *".."* || ! "${variant_sha256}" =~ ^[[:xdigit:]]{64}$ ]]; then
      echo "release train contains an unsafe variant configuration reference" >&2
      exit 1
    fi
    variant_config="${lock_workspace}/train-variants.yaml"
    aws s3 cp "${root}/inputs/${variant_path}" "${variant_config}"
    if [[ "$(sha256sum "${variant_config}" | awk '{print $1}')" != "${variant_sha256}" ]]; then
      echo "stored variant configuration does not match the release train" >&2
      exit 1
    fi
  index_candidate_conda_channel
fi

# Apply the checksum-bound system pin policy to this disposable checkout before
# product scripts build or resolve dependencies. Legacy receipts are a no-op.
candidate_checkout="$(git rev-parse --show-toplevel)"
python3 "${script_directory}/apply_dependency_pins.py" \
  --receipt "${variant_source}" \
  --checkout "${candidate_checkout}"

# Repository build scripts already name their same-run intermediate artifact.
# Preserve that interface, but resolve only that exact bundle from candidate S3.
# Capture the family decision now: the generated helper runs later and should
# not need a Conda-only setup variable in a wheel job.
{
  printf '%s\n' '#!/usr/bin/env bash'
  printf 'prepare_conda_channel=%q\n' "${RELEASE_CANDIDATE_PREPARE_CONDA_CHANNEL}"
  tail -n +2 "${script_directory}/download-from-candidate.sh"
} >"${tools}/rapids-download-from-github"
chmod +x "${tools}/rapids-download-from-github"

# Not every release unit uses CMake.  When it does, shadow `cmake` so the
# repository's existing RAPIDS.cmake bootstrap receives the train-locked
# revision.  Pure-Python jobs remain independent of a CMake installation.
if [[ -n "${original_cmake}" ]]; then
  cp "${script_directory}/cmake.sh" "${tools}/cmake"
  chmod +x "${tools}/cmake"
fi

printf 'RELEASE_CANDIDATE_DEPENDENCY_MANIFESTS=%s\n' "${manifests}" >>"${GITHUB_ENV}"

if [[ "${RELEASE_CANDIDATE_PREPARE_CONDA_CHANNEL}" == "true" \
  && "${RELEASE_CANDIDATE_ARTIFACT_FAMILY}" != "container" \
  && "${RELEASE_CANDIDATE_ARTIFACT_FAMILY}" != "docs" \
  && "${RELEASE_CANDIDATE_ARTIFACT_FAMILY}" != "maven" ]]; then
cat >"${tools}/rapids-rattler-channel-string" <<'EOF'
#!/usr/bin/env bash
RAPIDS_PREPENDED_CONDA_CHANNELS=("${RAPIDS_CANDIDATE_CONDA_CHANNEL}" "${RAPIDS_PREPENDED_CONDA_CHANNELS[@]:-}")
source "${RAPIDS_CANDIDATE_ORIGINAL_RATTLER_CHANNEL_STRING}"

# Candidate builds must resolve RAPIDS packages only from exact local outputs
# and the train-scoped candidate channel. The standard helper adds the public
# RAPIDS channel, so remove those known defaults and reject any other external
# source that is not conda-forge.
candidate_channels=()
for ((index = 0; index < ${#RATTLER_CHANNELS[@]}; index += 2)); do
  if [[ "${RATTLER_CHANNELS[${index}]}" != "--channel" || -z "${RATTLER_CHANNELS[$((index + 1))]:-}" ]]; then
    echo "candidate build received an invalid Conda channel argument" >&2
    return 1
  fi
  channel="${RATTLER_CHANNELS[$((index + 1))]%/}"
  case "${channel}" in
    rapidsai|rapidsai-nightly|nvidia|https://conda.anaconda.org/rapidsai|https://conda.anaconda.org/rapidsai-nightly|https://conda.anaconda.org/nvidia)
      ;;
    conda-forge|https://conda.anaconda.org/conda-forge|file://*|/*)
      candidate_channels+=(--channel "${channel}")
      ;;
    *)
      echo "candidate build rejected external Conda channel: ${channel}" >&2
      return 1
      ;;
  esac
done
RATTLER_CHANNELS=("${candidate_channels[@]}")
export RATTLER_CHANNELS
EOF
chmod +x "${tools}/rapids-rattler-channel-string"
cat >"${tools}/rattler-build" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
arguments=("$@")
if [[ "${arguments[0]:-}" == "build" ]]; then
  for index in "${!arguments[@]}"; do
    if [[ "${arguments[${index}]}" == "--recipe" && -n "${arguments[$((index + 1))]:-}" ]]; then
      arguments[$((index + 1))]="$(python3 "${RAPIDS_CANDIDATE_RECIPE_PATCHER}" \
        --recipe "${arguments[$((index + 1))]}" \
        --build-lock "${RAPIDS_CANDIDATE_BUILD_LOCK_PACKAGE}" \
        --host-lock "${RAPIDS_CANDIDATE_HOST_LOCK_PACKAGE}" \
        --rapids-cmake-sha "${RAPIDS_CANDIDATE_RAPIDS_CMAKE_SHA}")"
      arguments+=(--ignore-recipe-variants --variant-config "${RAPIDS_CANDIDATE_RATTLER_VARIANT_CONFIG}")
      break
    fi
  done
fi
exec "${RAPIDS_CANDIDATE_ORIGINAL_RATTLER_BUILD}" "${arguments[@]}"
EOF
chmod +x "${tools}/rattler-build"
fi

{
  printf 'RELEASE_CANDIDATE_BUCKET=%s\n' "${RELEASE_CANDIDATE_BUCKET}"
  printf 'RELEASE_CANDIDATE_TRAIN_PREFIX=%s\n' "${RELEASE_CANDIDATE_TRAIN_PREFIX}"
  printf 'RELEASE_CANDIDATE_TRAIN_SHA256=%s\n' "${RELEASE_CANDIDATE_TRAIN_SHA256}"
  printf 'RELEASE_CANDIDATE_BUILD_IMPLEMENTATION_REVISIONS=%s\n' "${build_implementation_revisions}"
  printf 'RELEASE_CANDIDATE_BUILD_IMPLEMENTATION_REPOSITORIES=%s\n' "${build_implementation_repositories}"
  printf 'RELEASE_CANDIDATE_GHA_TOOLS_REVISION=%s\n' "${actual_gha_tools_revision}"
  printf 'RELEASE_CANDIDATE_UPSTREAM_INPUTS=%s\n' "${upstream_inputs}"
  printf 'RELEASE_CANDIDATE_BUILD_INPUTS=%s\n' "${candidate_build_inputs}"
  printf 'RELEASE_CANDIDATE_SOURCE_INPUTS=%s\n' "${candidate_source_inputs}"
  printf 'RELEASE_CANDIDATE_SOURCE_MANIFEST=%s\n' "${source_manifest}"
  printf 'RAPIDS_RELEASE_CANDIDATE_TRAIN_FILE=%s\n' "${workspace}/release-train.json"
  unit_versions="$(jq -ceS '
    (.version_contract // {}) as $contract
    | if ($contract | type) != "object"
      then error("version_contract must be an object")
      elif (($contract.package_versions // {}) | type) != "object"
        or (($contract.unit_package_versions // {}) | type) != "object"
        then error("package version maps must be objects")
      elif ((($contract.package_versions // {}) + ($contract.unit_package_versions // {}))
        | all(to_entries[]; (.key | type) == "string" and (.value | type) == "string" and (.value | length) > 0))
        then {package_versions: ($contract.package_versions // {}), unit_package_versions: ($contract.unit_package_versions // {})}
      else error("package version maps contain invalid entries")
      end
  ' "${workspace}/release-train.json")" || {
    echo "release train contains an invalid frozen package version contract" >&2
    exit 1
  }
  printf 'RAPIDS_RELEASE_CANDIDATE_UNIT_VERSIONS=%s\n' "${unit_versions}"
  printf 'RAPIDS_RELEASE_CANDIDATE_VERSION=%s\n' "${candidate_release_version}"
  if [[ -n "${maven_repository:-}" ]]; then
    printf 'MAVEN_LOCAL_REPO=%s\n' "${maven_repository}"
    maven_opts="${MAVEN_OPTS:-}"
    if [[ -n "${maven_opts}" ]]; then
      maven_opts+=" "
    fi
    printf 'MAVEN_OPTS=%s-Dmaven.repo.local=%s\n' "${maven_opts}" "${maven_repository}"
  fi
  printf 'RAPIDS_RELEASE_CANDIDATE_SCCACHE=%s\n' "${candidate_sccache}"
  # Rattler installs CMake in its isolated prefixes, so this train value must
  # be available to the recipe patcher even when CMake is absent from the
  # outer workflow container at setup time.
  printf 'RAPIDS_CANDIDATE_RAPIDS_CMAKE_SHA=%s\n' "${rapids_cmake_sha}"
  printf 'SKBUILD_CMAKE_DEFINE=%s\n' "${skbuild_cmake_define}"
  if [[ -n "${original_cmake}" ]]; then
    printf 'RAPIDS_CANDIDATE_ORIGINAL_CMAKE=%s\n' "${original_cmake}"
  fi
  if [[ "${RELEASE_CANDIDATE_PREPARE_CONDA_CHANNEL}" == "true" ]]; then
    printf 'RAPIDS_CANDIDATE_CONDA_CHANNEL=%s\n' "${channel}"
    if [[ "${RELEASE_CANDIDATE_ARTIFACT_FAMILY}" != "container" \
    && "${RELEASE_CANDIDATE_ARTIFACT_FAMILY}" != "docs" \
    && "${RELEASE_CANDIDATE_ARTIFACT_FAMILY}" != "maven" ]]; then
      printf 'RAPIDS_CANDIDATE_ORIGINAL_RATTLER_CHANNEL_STRING=%s\n' "${original_rattler}"
      printf 'RAPIDS_CANDIDATE_ORIGINAL_RATTLER_BUILD=%s\n' "${original_rattler_build}"
      printf 'RAPIDS_CANDIDATE_RECIPE_PATCHER=%s\n' "${IMPLEMENTATION_PATH}/patch_recipe.py"
      printf 'RAPIDS_CANDIDATE_BUILD_LOCK_PACKAGE=%s\n' "${lock_build_package}"
      printf 'RAPIDS_CANDIDATE_HOST_LOCK_PACKAGE=%s\n' "${lock_host_package}"
      printf 'RAPIDS_CANDIDATE_RATTLER_VARIANT_CONFIG=%s\n' "${variant_config}"
    fi
  fi
  if [[ -n "${wheel_constraint}" ]]; then
    printf 'PIP_CONSTRAINT=%s\n' "${wheel_constraint}"
  fi
  printf 'PIP_FIND_LINKS=%s\n' "${wheelhouse}${PIP_FIND_LINKS:+ ${PIP_FIND_LINKS}}"
} >>"${GITHUB_ENV}"
printf '%s\n' "${gha_tools_directory}/tools" >>"${GITHUB_PATH}"
printf '%s\n' "${tools}" >>"${GITHUB_PATH}"
