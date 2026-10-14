#!/usr/bin/env bash
# Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.

set -euo pipefail

workspace="${1:?usage: prepare-ucxx-wheel-version.sh WORKSPACE UNIT_ID UNIT_VERSION}"
unit_id="${2:?usage: prepare-ucxx-wheel-version.sh WORKSPACE UNIT_ID UNIT_VERSION}"
unit_version="${3:?usage: prepare-ucxx-wheel-version.sh WORKSPACE UNIT_ID UNIT_VERSION}"

# UCXX's Conda version is the two-component project version, while its CMake
# input is a three-component version. Other source projects keep their existing
# version files untouched.
if [[ "${unit_id}" != "wheel:ucxx" ]]; then
  exit 0
fi
if [[ ! "${unit_version}" =~ ^([0-9]+)\.([0-9]+)$ ]]; then
  echo "wheel:ucxx requires a frozen two-component package version" >&2
  exit 1
fi

source_version="${BASH_REMATCH[1]}.${BASH_REMATCH[2]}.00"
root_version_file="${workspace}/VERSION"
if [[ ! -f "${root_version_file}" || "$(<"${root_version_file}")" != "${source_version}" ]]; then
  echo "UCXX source VERSION does not match frozen wheel version ${unit_version}" >&2
  exit 1
fi

metadata_versions=(
  "${workspace}/python/ucxx/ucxx/VERSION"
  "${workspace}/python/libucxx/libucxx/VERSION"
  "${workspace}/python/distributed-ucxx/distributed_ucxx/VERSION"
)
for version_file in "${metadata_versions[@]}"; do
  if [[ -L "${version_file}" ]]; then
    if [[ "$(readlink "${version_file}")" != "../../../VERSION" ]]; then
      echo "unexpected UCXX wheel metadata VERSION symlink: ${version_file}" >&2
      exit 1
    fi
    if [[ "$(<"${version_file}")" != "${source_version}" ]]; then
      echo "UCXX wheel metadata VERSION does not match its root source VERSION" >&2
      exit 1
    fi
  elif [[ -f "${version_file}" && "$(<"${version_file}")" == "${unit_version}" ]]; then
    # Allow an idempotent retry after this candidate-only preparation ran.
    continue
  else
    echo "missing or unexpected UCXX wheel metadata VERSION file: ${version_file}" >&2
    exit 1
  fi
done

for version_file in "${metadata_versions[@]}"; do
  if [[ -L "${version_file}" ]]; then
    replacement="${version_file}.candidate.$$"
    printf '%s\n' "${unit_version}" >"${replacement}"
    mv "${replacement}" "${version_file}"
  fi
done
