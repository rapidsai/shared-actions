#!/usr/bin/env bash
# Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.

# Install a private candidate replacement for rapids-upload-docs.
#
# Repository docs scripts retain their normal interface; the custom job calls
# this only after candidate dependency setup has selected the private train.
set -euo pipefail

for value in GITHUB_PATH GITHUB_REPOSITORY GITHUB_RUN_ID RELEASE_CANDIDATE_BUCKET RELEASE_CANDIDATE_TRAIN_PREFIX RELEASE_CANDIDATE_TRAIN_SHA256 RUNNER_TEMP; do
  if [[ -z "${!value:-}" ]]; then
    echo "${value} must be set for release-candidate documentation staging" >&2
    exit 1
  fi
done

tools="${RUNNER_TEMP}/release-candidate-tools"
mkdir -p "${tools}"

{
  printf '%s\n' '#!/usr/bin/env bash' 'set -euo pipefail'
  printf 'candidate_bucket=%q\n' "${RELEASE_CANDIDATE_BUCKET}"
  printf 'candidate_train_prefix=%q\n' "${RELEASE_CANDIDATE_TRAIN_PREFIX}"
  printf 'candidate_train_sha256=%q\n' "${RELEASE_CANDIDATE_TRAIN_SHA256}"
  cat <<'EOF'
docs_directory="${RAPIDS_DOCS_DIR:?RAPIDS_DOCS_DIR must name the generated documentation}"
docs_version="${RAPIDS_VERSION_NUMBER:?RAPIDS_VERSION_NUMBER must name the staged documentation version}"
destination="s3://${candidate_bucket}/${candidate_train_prefix}/${candidate_train_sha256}/${GITHUB_REPOSITORY}/${GITHUB_RUN_ID}/docs/${docs_version}/"
aws s3 sync "${docs_directory}/" "${destination}" --no-progress
printf 'Staged release-candidate documentation: %s\n' "${destination}"
EOF
} >"${tools}/rapids-upload-docs"

chmod +x "${tools}/rapids-upload-docs"
printf '%s\n' "${tools}" >>"${GITHUB_PATH}"
