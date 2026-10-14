#!/usr/bin/env bash
# Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.

set -euo pipefail

tools_directory="${1:?usage: install-version-override.sh TOOLS_DIRECTORY}"
candidate_version="${RAPIDS_RELEASE_CANDIDATE_CMAKE_VERSION:-${RAPIDS_RELEASE_CANDIDATE_UNIT_VERSION:-${RAPIDS_RELEASE_CANDIDATE_VERSION:-}}}"

# Custom candidate jobs may not declare a VERSION file. Keep their train-pinned
# gha-tools command untouched when the shared workflow did not select a version.
if [[ -z "${candidate_version}" ]]; then
  exit 0
fi

if [[ -n "${RAPIDS_RELEASE_CANDIDATE_CMAKE_VERSION:-}" ]]; then
  valid_candidate_version='^[0-9]+\.[0-9]+\.[0-9]+$'
else
  valid_candidate_version='^[0-9]+\.[0-9]+(\.[0-9]+)?$'
fi
if [[ ! "${candidate_version}" =~ ${valid_candidate_version} ]]; then
  echo "candidate version must be final numeric X.Y or X.Y.Z" >&2
  exit 1
fi

mkdir -p "${tools_directory}"
wrapper="${tools_directory}/rapids-generate-version"
{
  printf '#!/usr/bin/env bash\n'
  printf 'set -euo pipefail\n'
  printf 'printf "%%s\\n" "%s"\n' "${candidate_version}"
} >"${wrapper}"
chmod 0755 "${wrapper}"
