#!/usr/bin/env bash
# Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.

# Candidate dependencies remain train-scoped and private.  We first retrieve
# the small release train and catalog envelopes, then download only files for
# the current unit's declared *build* prerequisites.  In particular, this must
# never turn all completed candidate outputs into a shared package channel.
set -euo pipefail

require_value() {
  local name="$1"
  local value="$2"
  if [[ -z "${value}" || "${value}" == *$'\n'* || "${value}" == *$'\r'* ]]; then
    echo "${name} must be a non-empty single-line string" >&2
    exit 1
  fi
}

for value in GITHUB_ENV GITHUB_PATH RUNNER_TEMP GITHUB_REPOSITORY RELEASE_CANDIDATE_BUCKET RELEASE_CANDIDATE_TRAIN_PREFIX RELEASE_CANDIDATE_TRAIN_SHA256 RELEASE_CANDIDATE_PREPARE_CONDA_CHANNEL RELEASE_CANDIDATE_ARTIFACT_FAMILY RELEASE_CANDIDATE_ARCH RELEASE_CANDIDATE_CUDA_VERSION RELEASE_CANDIDATE_PYTHON_VERSION; do
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
if [[ "${RELEASE_CANDIDATE_ARTIFACT_FAMILY}" != "conda" && "${RELEASE_CANDIDATE_ARTIFACT_FAMILY}" != "wheel" ]]; then
  echo "RELEASE_CANDIDATE_ARTIFACT_FAMILY must be conda or wheel" >&2
  exit 1
fi

root="s3://${RELEASE_CANDIDATE_BUCKET}/${RELEASE_CANDIDATE_TRAIN_PREFIX}/${RELEASE_CANDIDATE_TRAIN_SHA256}"
train_key_prefix="${RELEASE_CANDIDATE_TRAIN_PREFIX}/${RELEASE_CANDIDATE_TRAIN_SHA256}"
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

# Schema-2 trains keep generated input evidence in compact, checksum-addressed
# receipts. Download those before resolving this job's lock packages. Older
# trains retain the inline fields during the migration period.
lock_source="${workspace}/release-train.json"
variant_source="${workspace}/release-train.json"
if jq -e '.input_receipts' "${workspace}/release-train.json" >/dev/null; then
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
  done
  lock_source="${workspace}/locks-receipt.json"
  variant_source="${workspace}/variants-receipt.json"
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
aws s3 cp "${root}/" "${manifests}" --recursive --exclude '*' --include '*/bundle-reference.json'
shopt -s globstar nullglob
reference_paths=("${manifests}"/**/bundle-reference.json)
declare -A selected_attempts=()
declare -A selected_reference_paths=()
for reference_path in "${reference_paths[@]}"; do
  source_repository="$(jq -r '.repository // empty' "${reference_path}")"
  source_artifact="$(jq -r '.source_artifact // empty' "${reference_path}")"
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
  artifact_key="$(jq -r '.artifact_key // .content_key // empty' "${reference_path}")"
  if [[ -z "${artifact_key}" || "${artifact_key}" != artifacts/* || "${artifact_key}" == *".."* ]]; then
    echo "candidate train contains an unsafe artifact reference" >&2
    exit 1
  fi
  build_input_digest="$(jq -r '.build_input_digest // empty' "${reference_path}")"
  if [[ -z "${build_input_digest}" || ! "${build_input_digest}" =~ ^[0-9a-f]{64}$ ]]; then
    echo "candidate train contains a missing or unsafe producer build-input digest" >&2
    exit 1
  fi
  bundle_key="$(jq -r '.bundle_key // empty' "${reference_path}")"
  if [[ ( "${bundle_key}" != "${artifact_key}"/release-catalog-entries.json && "${bundle_key}" != "${train_key_prefix}"/*/release-catalog-entries.json ) || "${bundle_key}" == *".."* ]]; then
    echo "candidate train contains an unsafe catalog bundle reference" >&2
    exit 1
  fi
  manifest_path="${reference_path%/bundle-reference.json}/release-catalog-entries.json"
  aws s3 cp "s3://${RELEASE_CANDIDATE_BUCKET}/${bundle_key}" "${manifest_path}"
  jq \
    --arg artifact_key "${artifact_key}" \
    --arg build_input_digest "${build_input_digest}" \
    '. + {artifact_key: $artifact_key, build_input_digest: $build_input_digest}' \
    "${manifest_path}" >"${manifest_path}.tmp"
  mv "${manifest_path}.tmp" "${manifest_path}"
done

repository="${GITHUB_REPOSITORY##*/}"
target_unit="${RELEASE_CANDIDATE_ARTIFACT_FAMILY}:${repository}"
cuda_major="${RELEASE_CANDIDATE_CUDA_VERSION%%.*}"
selected="${workspace}/selected-artifacts.json"
resolved_inputs="${workspace}/resolved-upstream-inputs.jsonl"
upstream_inputs="${workspace}/upstream-inputs.json"
: >"${resolved_inputs}"
if ! jq -e --arg target "${target_unit}" '.release_units[] | select(.id == $target and .artifact_family != "source")' \
  "${workspace}/release-train.json" >/dev/null; then
  echo "release train has no candidate build unit for ${target_unit}" >&2
  exit 1
fi

# Shell globstar has a clear, portable meaning here and keeps the jq program
# focused on schema fields rather than filesystem traversal.
manifest_paths=("${manifests}"/**/release-catalog-entries.json)
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
  --arg conda_platform "$([[ "${RELEASE_CANDIDATE_ARCH}" == "aarch64" ]] && printf linux-aarch64 || printf linux-64)" \
  --slurpfile records "${workspace}/catalog-records.json" \
  '
    ($train[0].release_units | map({key: .id, value: .}) | from_entries) as $units
    | def closure($id):
        ($units[$id].dependencies // [])
        | map(select($units[.] != null and $units[.].artifact_family != "source") | ., closure(.))
        | flatten | unique;
    closure($target) as $dependencies
    | [ $dependencies[] | select(startswith($family + ":")) ] as $family_dependencies
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
  if [[ -z "${artifact_key}" || "${artifact_key}" != artifacts/* || "${artifact_key}" == *".."* || "${artifact_path}" == /* || "${artifact_path}" == *".."* ]]; then
    echo "candidate catalog contains an unsafe dependency location" >&2
    exit 1
  fi
  # Canonical candidate storage preserves the producer's bundle-relative
  # layout without tying dependency retrieval to a GitHub run attempt.
  source="s3://${RELEASE_CANDIDATE_BUCKET}/${artifact_key}/${artifact_path}"
  case "${artifact_path}" in
    *.conda|*.tar.bz2)
      [[ "${RELEASE_CANDIDATE_PREPARE_CONDA_CHANNEL}" == "true" ]] || continue
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

if [[ "${RELEASE_CANDIDATE_PREPARE_CONDA_CHANNEL}" == "true" ]]; then
  if ! command -v conda >/dev/null; then
    echo "conda is required to index the private candidate channel" >&2
    exit 1
  fi
  conda index "${channel}"
fi

original_download="$(command -v rapids-download-from-github || true)"
original_rattler="$(command -v rapids-rattler-channel-string || true)"
original_rattler_build="$(command -v rattler-build || true)"
if [[ -z "${original_download}" ]]; then
  echo "RAPIDS gha-tools must be installed before candidate dependency setup" >&2
  exit 1
fi
if [[ "${RELEASE_CANDIDATE_PREPARE_CONDA_CHANNEL}" == "true" && -z "${original_rattler}" ]]; then
  echo "rapids-rattler-channel-string is required for a candidate Conda build" >&2
  exit 1
fi
if [[ "${RELEASE_CANDIDATE_PREPARE_CONDA_CHANNEL}" == "true" && -z "${original_rattler_build}" ]]; then
  echo "rattler-build is required for a candidate Conda build" >&2
  exit 1
fi

# A lock metapackage has no payload. Its run constraints are nevertheless part
# of the build/host solve once this package is injected into a recipe. Keeping
# it in the job-local candidate channel avoids publishing CI-only lock packages.
if [[ "${RELEASE_CANDIDATE_PREPARE_CONDA_CHANNEL}" == "true" ]]; then
  conda_platform="$([[ "${RELEASE_CANDIDATE_ARCH}" == "aarch64" ]] && printf linux-aarch64 || printf linux-64)"
  lock_workspace="${workspace}/train-locks"
  mkdir -p "${lock_workspace}"

  prepare_lock_metapackage() {
    local section="$1"
    local record relative_path expected_sha256 lock_path actual_sha256 constraints package_name recipe
    record="$(jq -cer \
      --arg platform "${conda_platform}" \
      --arg cuda "${cuda_major}" \
      --arg environment "${section}" \
      '(.conda // .input_locks.conda)[]
       | select(.scope.platform == $platform and .scope.cuda == $cuda and .scope.environment == $environment)' \
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
    jq -n \
      --arg name "${package_name}" \
      --argjson constraints "${constraints}" \
      '{schema_version: 1, package: {name: $name, version: "0"}, build: {number: 0, noarch: "generic"}, requirements: {run_constraints: $constraints}}' \
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
  conda index "${channel}"
fi

# Repository build scripts already name their same-run intermediate artifact.
# Preserve that interface, but resolve only that exact bundle from candidate S3.
# Capture the family decision now: the generated helper runs later and should
# not need a Conda-only setup variable in a wheel job.
{
  printf '%s\n' '#!/usr/bin/env bash' 'set -euo pipefail'
  printf 'prepare_conda_channel=%q\n' "${RELEASE_CANDIDATE_PREPARE_CONDA_CHANNEL}"
  cat <<'EOF'
artifact_name="${1:?artifact name is required}"
destination="${RAPIDS_UNZIP_DIR:-$(mktemp -d)}"
reference_root="${RELEASE_CANDIDATE_DEPENDENCY_MANIFESTS:?candidate dependency manifests are required}/${GITHUB_REPOSITORY}/${artifact_name}"
mapfile -t references < <(find "${reference_root}" -type f -name bundle-reference.json -print | sort)
if [[ "${#references[@]}" -ne 1 ]]; then
  echo "candidate artifact must have exactly one selected train reference: ${artifact_name}" >&2
  exit 1
fi
reference="${references[0]}"
artifact_key="$(jq -r '.artifact_key // .content_key // empty' "${reference}")"
if [[ -z "${artifact_key}" || "${artifact_key}" != artifacts/* || "${artifact_key}" == *".."* ]]; then
  echo "candidate artifact has an unsafe artifact reference: ${artifact_name}" >&2
  exit 1
fi
source_prefix="s3://${RELEASE_CANDIDATE_BUCKET}/${artifact_key}/"
# Callers capture this helper's stdout as the downloaded directory. Keep AWS
# transfer diagnostics on stderr so they can never become part of a Conda URL.
if ! aws s3 cp "${source_prefix}" "${destination}" --recursive >&2; then
  echo "candidate artifact is not available in this train: ${artifact_name}" >&2
  exit 1
fi
if [[ "${prepare_conda_channel}" == "true" ]] \
  && find "${destination}" -type f \( -name '*.conda' -o -name '*.tar.bz2' \) -print -quit | grep -q .; then
  # C++ outputs are also an input to the repository's Python build. That
  # temporary same-job directory is a Conda channel too, so it needs empty
  # noarch and platform indexes in addition to the downloaded package files.
  mkdir -p "${destination}"/{linux-64,linux-aarch64,noarch}
  conda index "${destination}" >&2
fi
printf '%s' "${destination}"
EOF
} >"${tools}/rapids-download-from-github"
chmod +x "${tools}/rapids-download-from-github"

printf 'RELEASE_CANDIDATE_DEPENDENCY_MANIFESTS=%s\n' "${manifests}" >>"${GITHUB_ENV}"

if [[ "${RELEASE_CANDIDATE_PREPARE_CONDA_CHANNEL}" == "true" ]]; then
cat >"${tools}/rapids-rattler-channel-string" <<'EOF'
#!/usr/bin/env bash
RAPIDS_PREPENDED_CONDA_CHANNELS=("${RAPIDS_CANDIDATE_CONDA_CHANNEL}" "${RAPIDS_PREPENDED_CONDA_CHANNELS[@]:-}")
source "${RAPIDS_CANDIDATE_ORIGINAL_RATTLER_CHANNEL_STRING}"
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
        --host-lock "${RAPIDS_CANDIDATE_HOST_LOCK_PACKAGE}")"
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
  printf 'RELEASE_CANDIDATE_UPSTREAM_INPUTS=%s\n' "${upstream_inputs}"
  if [[ "${RELEASE_CANDIDATE_PREPARE_CONDA_CHANNEL}" == "true" ]]; then
    printf 'RAPIDS_CANDIDATE_CONDA_CHANNEL=%s\n' "${channel}"
    printf 'RAPIDS_CANDIDATE_ORIGINAL_RATTLER_CHANNEL_STRING=%s\n' "${original_rattler}"
    printf 'RAPIDS_CANDIDATE_ORIGINAL_RATTLER_BUILD=%s\n' "${original_rattler_build}"
    printf 'RAPIDS_CANDIDATE_RECIPE_PATCHER=%s\n' "${IMPLEMENTATION_PATH}/patch_recipe.py"
    printf 'RAPIDS_CANDIDATE_BUILD_LOCK_PACKAGE=%s\n' "${lock_build_package}"
    printf 'RAPIDS_CANDIDATE_HOST_LOCK_PACKAGE=%s\n' "${lock_host_package}"
    printf 'RAPIDS_CANDIDATE_RATTLER_VARIANT_CONFIG=%s\n' "${variant_config}"
  fi
  printf 'PIP_FIND_LINKS=%s\n' "${wheelhouse}${PIP_FIND_LINKS:+ ${PIP_FIND_LINKS}}"
} >>"${GITHUB_ENV}"
printf '%s\n' "${tools}" >>"${GITHUB_PATH}"
