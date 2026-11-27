#!/usr/bin/env bash
# Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
#
# Put the train's pinned rapids-cmake in OUTPUT_DIR for this build row, so CMake never downloads it.
#
# A row of a release file (REQUIRE_STAGED=true) takes the tarball Create/Update Train staged beside the train
# (stage-rapids-cmake.sh) and fails if there is none: the fix is Update Train. Any other candidate row takes the
# staged tarball when it exists, else GitHub's archive of the commit, retried, instead of CMake's single attempt.
# Either way every archive entry must be under rapids-cmake-<sha>/. Setup then passes OUTPUT_DIR to every CMake
# configure as FETCHCONTENT_SOURCE_DIR_RAPIDS-CMAKE.
#
# Usage: prepare-rapids-cmake.sh SHA OUTPUT_DIR REQUIRE_STAGED
# Environment: RELEASE_CANDIDATE_BUCKET, RELEASE_CANDIDATE_TRAIN_PREFIX, RELEASE_CANDIDATE_TRAIN_SHA256

set -euo pipefail

sha="${1:?usage: prepare-rapids-cmake.sh SHA OUTPUT_DIR REQUIRE_STAGED}"
output="${2:?usage: prepare-rapids-cmake.sh SHA OUTPUT_DIR REQUIRE_STAGED}"
require_staged="${3:?usage: prepare-rapids-cmake.sh SHA OUTPUT_DIR REQUIRE_STAGED}"
bucket="${RELEASE_CANDIDATE_BUCKET:?candidate bucket is required}"
prefix="${RELEASE_CANDIDATE_TRAIN_PREFIX:?candidate train prefix is required}"
train_sha256="${RELEASE_CANDIDATE_TRAIN_SHA256:?candidate train digest is required}"
archive_url="${RAPIDS_CMAKE_ARCHIVE_URL:-https://github.com/rapidsai/rapids-cmake/archive}"

if [[ ! "${sha}" =~ ^[0-9a-f]{40}$ || ! "${train_sha256}" =~ ^[0-9a-f]{64}$ ]]; then
  echo "invalid rapids-cmake commit or train digest" >&2
  exit 1
fi
if [[ "${require_staged}" != true && "${require_staged}" != false ]]; then
  echo "REQUIRE_STAGED must be true or false" >&2
  exit 1
fi
if [[ -e "${output}" ]]; then
  echo "rapids-cmake output directory already exists: ${output}" >&2
  exit 1
fi
key="${prefix}/${train_sha256}/build-tools/rapids-cmake/${sha}.tar.gz"

work="$(mktemp -d)"
trap 'rm -rf "${work}"' EXIT
archive="${work}/rapids-cmake.tar.gz"
if aws s3api get-object --bucket "${bucket}" --key "${key}" "${archive}" > /dev/null 2> "${work}/error.txt"; then
  origin="s3://${bucket}/${key}"
elif [[ "${require_staged}" == true ]]; then
  cat "${work}/error.txt" >&2
  echo "::error title=rapids-cmake not staged::Create/Update Train stages rapids-cmake ${sha} at s3://${bucket}/${key} for every release build; it is missing. Run Update Train." >&2
  exit 1
else
  rm -f "${archive}"
  curl --fail --silent --show-error --location --retry 5 --retry-all-errors --retry-delay 15 --max-time 300 \
    --output "${archive}" "${archive_url}/${sha}.tar.gz"
  origin="${archive_url}/${sha}.tar.gz"
fi

# Every entry must sit under rapids-cmake-<sha>/ (archives need not list the directory itself first).
entries="$(tar -tzf "${archive}")"
outside="$(grep -v -m 1 "^rapids-cmake-${sha}/" <<< "${entries}" || true)"
if [[ -z "${outside}" ]] && grep -q -E '(^|/)\.\.(/|$)' <<< "${entries}"; then
  outside="$(grep -m 1 -E '(^|/)\.\.(/|$)' <<< "${entries}")"
fi
if [[ -z "${entries}" || -n "${outside}" ]]; then
  echo "the rapids-cmake archive from ${origin} is not rapids-cmake-${sha}/ (entry: ${outside:-none})" >&2
  exit 1
fi
mkdir -p "${output}"
tar -xzf "${archive}" -C "${output}" --strip-components=1
if [[ ! -f "${output}/CMakeLists.txt" || ! -d "${output}/rapids-cmake" ]]; then
  echo "the rapids-cmake archive from ${origin} is not a rapids-cmake source tree" >&2
  exit 1
fi
echo "Using rapids-cmake ${sha} from ${origin}"
