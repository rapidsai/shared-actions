#!/usr/bin/env bash
# Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.

# Install a private candidate replacement for rapids-upload-docs.
#
# Repository docs scripts retain their normal interface; the custom job calls
# this only after candidate dependency setup has selected the private train.
set -euo pipefail

for value in GITHUB_PATH GITHUB_WORKSPACE GITHUB_REPOSITORY RUNNER_TEMP; do
  if [[ -z "${!value:-}" ]]; then
    echo "${value} must be set for release-candidate documentation staging" >&2
    exit 1
  fi
done

tools="${RUNNER_TEMP}/release-candidate-tools"
mkdir -p "${tools}"
script_directory="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cp "${script_directory}/archive_docs.py" "${tools}/archive_docs.py"

{
  printf '%s\n' '#!/usr/bin/env bash' 'set -euo pipefail'
  printf 'workspace=%q\n' "${GITHUB_WORKSPACE}"
  printf 'archive_helper=%q\n' "${tools}/archive_docs.py"
  cat <<'EOF'
docs_directory="${RAPIDS_DOCS_DIR:?RAPIDS_DOCS_DIR must name the generated documentation}"
docs_version="${RAPIDS_RELEASE_CANDIDATE_VERSION:-${RAPIDS_VERSION_NUMBER:-}}"
if [[ ! "${docs_version}" =~ ^[0-9]{2}\.[0-9]{2}$ ]]; then
  echo "RAPIDS_RELEASE_CANDIDATE_VERSION must name the frozen YY.MM train version" >&2
  exit 1
fi
artifact_directory="${workspace}/.release-candidate-artifacts/docs"
mkdir -p "${artifact_directory}"
archive="${artifact_directory}/deployment-docs.tar.gz"
python3 "${archive_helper}" \
  --source "${docs_directory}" \
  --archive "${archive}" \
  --identity "${artifact_directory}/deployment-docs-package-identity.json" \
  --version "${docs_version}"
printf 'Prepared private release-candidate documentation archive: %s\n' "${archive}"
EOF
} >"${tools}/rapids-upload-docs"

chmod +x "${tools}/rapids-upload-docs"
printf '%s\n' "${tools}" >>"${GITHUB_PATH}"
