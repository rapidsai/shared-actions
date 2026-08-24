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

for value in GITHUB_ENV GITHUB_PATH RUNNER_TEMP GITHUB_REPOSITORY GITHUB_RUN_ID RELEASE_CANDIDATE_BUCKET RELEASE_CANDIDATE_PREFIX RELEASE_CANDIDATE_TRAIN_SHA256 RELEASE_CANDIDATE_PREPARE_CONDA_CHANNEL RELEASE_CANDIDATE_ARTIFACT_FAMILY RELEASE_CANDIDATE_ARCH RELEASE_CANDIDATE_CUDA_VERSION RELEASE_CANDIDATE_PYTHON_VERSION; do
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

root="s3://${RELEASE_CANDIDATE_BUCKET}/${RELEASE_CANDIDATE_PREFIX}/${RELEASE_CANDIDATE_TRAIN_SHA256}"
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

# Catalog envelopes are metadata, not package payloads.  Downloading them is
# deliberately cheap and lets the runner resolve exact S3 object keys below.
# The catalog lives below repository/run/artifact directories. The leading
# wildcard is required by AWS CLI's include matching; without it, only a
# hypothetical train-root manifest would be copied.
aws s3 cp "${root}/" "${manifests}" --recursive --exclude '*' --include '*release-catalog-entries.json'

repository="${GITHUB_REPOSITORY##*/}"
target_unit="${RELEASE_CANDIDATE_ARTIFACT_FAMILY}:${repository}"
cuda_major="${RELEASE_CANDIDATE_CUDA_VERSION%%.*}"
selected="${workspace}/selected-artifacts.json"
if ! jq -e --arg target "${target_unit}" '.release_units[] | select(.id == $target and .artifact_family != "source")' \
  "${workspace}/release-train.json" >/dev/null; then
  echo "release train has no candidate build unit for ${target_unit}" >&2
  exit 1
fi

# Shell globstar has a clear, portable meaning here and keeps the jq program
# focused on schema fields rather than filesystem traversal.
shopt -s globstar nullglob
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
            source: $record.source,
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

while IFS= read -r selection; do
  source_artifact="$(jq -r '.source.artifact' <<<"${selection}")"
  source_repository="$(jq -r '.source.repository' <<<"${selection}")"
  source_run_id="$(jq -r '.source.run_id' <<<"${selection}")"
  artifact_path="$(jq -r '.path' <<<"${selection}")"
  if [[ -z "${source_artifact}" || -z "${source_repository}" || -z "${source_run_id}" || "${artifact_path}" == /* || "${artifact_path}" == *".."* ]]; then
    echo "candidate catalog contains an unsafe dependency location" >&2
    exit 1
  fi
  source="${root}/${source_repository}/${source_run_id}/${source_artifact}/${artifact_path}"
  case "${artifact_path}" in
    *.conda|*.tar.bz2)
      [[ "${RELEASE_CANDIDATE_PREPARE_CONDA_CHANNEL}" == "true" ]] || continue
      platform="$(jq -r '.package.platform // ""' <<<"${selection}")"
      case "${platform}" in
        linux-64|linux-aarch64|noarch) destination="${channel}/${platform}" ;;
        *) echo "candidate Conda package has unsupported platform: ${platform}" >&2; exit 1 ;;
      esac
      aws s3 cp "${source}" "${destination}/$(basename "${artifact_path}")"
      ;;
    *.whl)
      if ! python3 "$(dirname "$0")/select_wheel.py" \
        --python-version "${RELEASE_CANDIDATE_PYTHON_VERSION}" "$(basename "${artifact_path}")"; then
        continue
      fi
      aws s3 cp "${source}" "${wheelhouse}/$(basename "${artifact_path}")"
      ;;
    *) echo "candidate catalog has unsupported package type: ${artifact_path}" >&2; exit 1 ;;
  esac
done < <(jq -c '.[]' "${selected}")

if [[ "${RELEASE_CANDIDATE_PREPARE_CONDA_CHANNEL}" == "true" ]]; then
  if ! command -v conda >/dev/null; then
    echo "conda is required to index the private candidate channel" >&2
    exit 1
  fi
  conda index "${channel}"
fi

original_download="$(command -v rapids-download-from-github || true)"
original_rattler="$(command -v rapids-rattler-channel-string || true)"
if [[ -z "${original_download}" ]]; then
  echo "RAPIDS gha-tools must be installed before candidate dependency setup" >&2
  exit 1
fi
if [[ "${RELEASE_CANDIDATE_PREPARE_CONDA_CHANNEL}" == "true" && -z "${original_rattler}" ]]; then
  echo "rapids-rattler-channel-string is required for a candidate Conda build" >&2
  exit 1
fi

# Repository build scripts already name their same-run intermediate artifact.
# Preserve that interface, but resolve only that exact bundle from candidate S3.
cat >"${tools}/rapids-download-from-github" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
artifact_name="${1:?artifact name is required}"
destination="${RAPIDS_UNZIP_DIR:-$(mktemp -d)}"
source_prefix="s3://${RELEASE_CANDIDATE_BUCKET}/${RELEASE_CANDIDATE_PREFIX}/${RELEASE_CANDIDATE_TRAIN_SHA256}/${GITHUB_REPOSITORY}/${GITHUB_RUN_ID}/${artifact_name}/"
if ! aws s3 cp "${source_prefix}" "${destination}" --recursive; then
  echo "candidate artifact is not available in this train: ${artifact_name}" >&2
  exit 1
fi
printf '%s' "${destination}"
EOF
chmod +x "${tools}/rapids-download-from-github"

if [[ "${RELEASE_CANDIDATE_PREPARE_CONDA_CHANNEL}" == "true" ]]; then
cat >"${tools}/rapids-rattler-channel-string" <<'EOF'
#!/usr/bin/env bash
RAPIDS_PREPENDED_CONDA_CHANNELS=("${RAPIDS_CANDIDATE_CONDA_CHANNEL}" "${RAPIDS_PREPENDED_CONDA_CHANNELS[@]:-}")
source "${RAPIDS_CANDIDATE_ORIGINAL_RATTLER_CHANNEL_STRING}"
EOF
chmod +x "${tools}/rapids-rattler-channel-string"
fi

{
  printf 'RELEASE_CANDIDATE_BUCKET=%s\n' "${RELEASE_CANDIDATE_BUCKET}"
  printf 'RELEASE_CANDIDATE_PREFIX=%s\n' "${RELEASE_CANDIDATE_PREFIX}"
  printf 'RELEASE_CANDIDATE_TRAIN_SHA256=%s\n' "${RELEASE_CANDIDATE_TRAIN_SHA256}"
  if [[ "${RELEASE_CANDIDATE_PREPARE_CONDA_CHANNEL}" == "true" ]]; then
    printf 'RAPIDS_CANDIDATE_CONDA_CHANNEL=%s\n' "${channel}"
    printf 'RAPIDS_CANDIDATE_ORIGINAL_RATTLER_CHANNEL_STRING=%s\n' "${original_rattler}"
  fi
  printf 'PIP_FIND_LINKS=%s\n' "${wheelhouse}${PIP_FIND_LINKS:+ ${PIP_FIND_LINKS}}"
} >>"${GITHUB_ENV}"
printf '%s\n' "${tools}" >>"${GITHUB_PATH}"
