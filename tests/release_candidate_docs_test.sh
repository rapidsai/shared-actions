#!/usr/bin/env bash
# Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
set -euo pipefail

repository_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
temporary_directory="$(mktemp -d)"
trap 'rm -rf "${temporary_directory}"' EXIT
mkdir -p "${temporary_directory}/workspace" "${temporary_directory}/docs/reference" \
  "${temporary_directory}/runner-temp" "${temporary_directory}/bin"
printf '%s\n' '<html>candidate docs</html>' >"${temporary_directory}/docs/index.html"
printf '%s\n' 'reference' >"${temporary_directory}/docs/reference/index.html"
cat >"${temporary_directory}/bin/aws" <<'EOF'
#!/usr/bin/env bash
echo "candidate docs helper must not write directly to S3" >&2
exit 1
EOF
chmod +x "${temporary_directory}/bin/aws"

GITHUB_PATH="${temporary_directory}/github-path" \
GITHUB_WORKSPACE="${temporary_directory}/workspace" \
GITHUB_REPOSITORY="rapidsai/deployment" \
RUNNER_TEMP="${temporary_directory}/runner-temp" \
bash "${repository_root}/release-candidate-docs/stage.sh"

RAPIDS_DOCS_DIR="${temporary_directory}/docs" RAPIDS_RELEASE_CANDIDATE_VERSION="26.12" \
  "${temporary_directory}/runner-temp/release-candidate-tools/rapids-upload-docs"

artifact_directory="${temporary_directory}/workspace/.release-candidate-artifacts/docs"
test -f "${artifact_directory}/deployment-docs.tar.gz"
jq -e '. == {"ecosystem":"docs","name":"deployment","version":"26.12","platform":"noarch"}' \
  "${artifact_directory}/deployment-docs-package-identity.json" >/dev/null
tar -tzf "${artifact_directory}/deployment-docs.tar.gz" | grep -Fx 'index.html' >/dev/null
tar -tzf "${artifact_directory}/deployment-docs.tar.gz" | grep -Fx 'reference/index.html' >/dev/null
