#!/usr/bin/env bash
# Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.

set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
helper="${root}/release-candidate-dependencies/prepare-ucxx-wheel-version.sh"
temporary_directory="$(mktemp -d)"
trap 'rm -rf "${temporary_directory}"' EXIT

seed_workspace() {
  local workspace="$1"
  printf '0.53.00\n' >"${workspace}/VERSION"
  mkdir -p \
    "${workspace}/python/ucxx/ucxx" \
    "${workspace}/python/libucxx/libucxx" \
    "${workspace}/python/distributed-ucxx/distributed_ucxx"
  ln -s ../../../VERSION "${workspace}/python/ucxx/ucxx/VERSION"
  ln -s ../../../VERSION "${workspace}/python/libucxx/libucxx/VERSION"
  ln -s ../../../VERSION "${workspace}/python/distributed-ucxx/distributed_ucxx/VERSION"
}

workspace="${temporary_directory}/valid"
mkdir -p "${workspace}"
seed_workspace "${workspace}"
bash "${helper}" "${workspace}" wheel:ucxx 0.53
[[ "$(<"${workspace}/VERSION")" == "0.53.00" ]]
for version_file in \
  "${workspace}/python/ucxx/ucxx/VERSION" \
  "${workspace}/python/libucxx/libucxx/VERSION" \
  "${workspace}/python/distributed-ucxx/distributed_ucxx/VERSION"; do
  [[ ! -L "${version_file}" ]]
  [[ "$(<"${version_file}")" == "0.53" ]]
done

# Candidate preparation can be retried before the build without changing bytes.
bash "${helper}" "${workspace}" wheel:ucxx 0.53

untouched="${temporary_directory}/other-unit"
mkdir -p "${untouched}"
seed_workspace "${untouched}"
bash "${helper}" "${untouched}" wheel:kvikio invalid
[[ -L "${untouched}/python/ucxx/ucxx/VERSION" ]]

bad_target="${temporary_directory}/bad-target"
mkdir -p "${bad_target}"
seed_workspace "${bad_target}"
rm "${bad_target}/python/distributed-ucxx/distributed_ucxx/VERSION"
ln -s ../../VERSION "${bad_target}/python/distributed-ucxx/distributed_ucxx/VERSION"
if bash "${helper}" "${bad_target}" wheel:ucxx 0.53; then
  echo "helper accepted an unexpected package VERSION symlink" >&2
  exit 1
fi
[[ -L "${bad_target}/python/ucxx/ucxx/VERSION" ]]

bad_version="${temporary_directory}/bad-version"
mkdir -p "${bad_version}"
seed_workspace "${bad_version}"
if bash "${helper}" "${bad_version}" wheel:ucxx 0.54; then
  echo "helper accepted a unit version that differs from the frozen source" >&2
  exit 1
fi
[[ -L "${bad_version}/python/ucxx/ucxx/VERSION" ]]

echo "UCXX candidate wheel version preparation tests passed."
