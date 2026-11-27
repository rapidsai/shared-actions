#!/usr/bin/env bash
# Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
#
# Fetch a train's pinned rapids-cmake once and store it beside the train, so no build row downloads it.
#
# Every build row used to fetch https://github.com/rapidsai/rapids-cmake/archive/<sha>.zip itself, once, with no
# retry; GitHub answered 504 for it three times in two 26.12 release runs (45922055714, 45926793154), each failing a
# row. Create/Update Train runs this after staging the train: it fetches the commit by SHA with git (retried),
# checks that git has exactly that commit, and writes a deterministic tarball (git archive | gzip -n, top directory
# rapids-cmake-<sha>/, as GitHub's archive) to <prefix>/<train>/build-tools/rapids-cmake/<sha>.tar.gz, written once.
# Dependency setup (prepare-rapids-cmake.sh) hands it to CMake as FETCHCONTENT_SOURCE_DIR_RAPIDS-CMAKE.
#
# Usage: stage-rapids-cmake.sh TRAIN_JSON BUCKET TRAIN_PREFIX

set -euo pipefail

train="${1:?usage: stage-rapids-cmake.sh TRAIN_JSON BUCKET TRAIN_PREFIX}"
bucket="${2:?usage: stage-rapids-cmake.sh TRAIN_JSON BUCKET TRAIN_PREFIX}"
prefix="${3:?usage: stage-rapids-cmake.sh TRAIN_JSON BUCKET TRAIN_PREFIX}"
repository="${RAPIDS_CMAKE_REPOSITORY_URL:-https://github.com/rapidsai/rapids-cmake.git}"
attempts="${RAPIDS_CMAKE_FETCH_ATTEMPTS:-5}"
delay="${RAPIDS_CMAKE_FETCH_DELAY_SECONDS:-15}"

train_sha256="$(jq -r '.train_sha256 // empty' "${train}")"
sha="$(jq -r '.build_tool_revisions["rapids-cmake"] // empty' "${train}")"
if [[ ! "${train_sha256}" =~ ^[0-9a-f]{64}$ ]]; then
  echo "the release train has no valid train_sha256" >&2
  exit 1
fi
if [[ ! "${sha}" =~ ^[0-9a-f]{40}$ ]]; then
  echo "the release train pins no full rapids-cmake commit (build_tool_revisions.rapids-cmake)" >&2
  exit 1
fi
if [[ ! "${prefix}" =~ ^[A-Za-z0-9_.-]+$ || ! "${attempts}" =~ ^[1-9][0-9]*$ || ! "${delay}" =~ ^[0-9]+$ ]]; then
  echo "invalid train prefix, attempts or delay" >&2
  exit 1
fi
key="${prefix}/${train_sha256}/build-tools/rapids-cmake/${sha}.tar.gz"

if aws s3api head-object --bucket "${bucket}" --key "${key}" > /dev/null 2>&1; then
  echo "rapids-cmake ${sha} is already staged at s3://${bucket}/${key}"
  exit 0
fi

work="$(mktemp -d)"
trap 'rm -rf "${work}"' EXIT
git -C "${work}" init --quiet
git -C "${work}" remote add origin "${repository}"
fetched=false
for attempt in $(seq 1 "${attempts}"); do
  if git -C "${work}" fetch --quiet --depth 1 --no-tags origin "${sha}"; then
    fetched=true
    break
  fi
  echo "fetching rapids-cmake ${sha} failed (attempt ${attempt} of ${attempts})" >&2
  if (( attempt < attempts )); then
    sleep $(( delay * attempt ))
  fi
done
if [[ "${fetched}" != true ]]; then
  echo "could not fetch rapids-cmake ${sha} from ${repository} in ${attempts} attempts" >&2
  exit 1
fi
if [[ "$(git -C "${work}" rev-parse FETCH_HEAD)" != "${sha}" ]]; then
  echo "git fetched another commit than rapids-cmake ${sha}" >&2
  exit 1
fi
archive="${work}/rapids-cmake.tar.gz"
git -C "${work}" archive --format=tar --prefix="rapids-cmake-${sha}/" FETCH_HEAD | gzip -n > "${archive}"

if ! aws s3api put-object --bucket "${bucket}" --key "${key}" --body "${archive}" \
    --content-type application/gzip --if-none-match '*' > /dev/null 2> "${work}/error.txt"; then
  # Another planning run staged it first; the key names one commit, so either copy serves.
  if ! grep -q 'PreconditionFailed' "${work}/error.txt"; then
    cat "${work}/error.txt" >&2
    exit 1
  fi
fi
echo "Staged rapids-cmake ${sha} at s3://${bucket}/${key}"
