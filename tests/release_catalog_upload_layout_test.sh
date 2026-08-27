#!/usr/bin/env bash
# Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.

# Exercise the immutable candidate-store split without AWS credentials. The
# fake CLI deliberately implements only the S3 calls made by upload-s3.sh.
set -euo pipefail

repository_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
temporary_directory="$(mktemp -d)"
trap 'rm -rf "${temporary_directory}"' EXIT
mkdir -p "${temporary_directory}/bin" "${temporary_directory}/bundle/release-evidence"

cat >"${temporary_directory}/bin/aws" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

[[ "$1" == "s3api" ]]
operation="$2"
shift 2
bucket=""
key=""
body=""
while [[ "$#" -gt 0 ]]; do
  case "$1" in
    --bucket) bucket="$2"; shift 2 ;;
    --key) key="$2"; shift 2 ;;
    --body) body="$2"; shift 2 ;;
    *) shift ;;
  esac
done
path="${FAKE_S3}/${bucket}/${key}"
case "${operation}" in
  head-object)
    [[ -f "${path}" ]]
    checksum="$(openssl dgst -sha256 -binary "${path}" | base64)"
    printf '{"ChecksumSHA256":"%s"}\n' "${checksum}"
    ;;
  put-object)
    [[ ! -e "${path}" ]]
    mkdir -p "$(dirname "${path}")"
    cp "${body}" "${path}"
    ;;
  *)
    echo "unsupported fake AWS operation: ${operation}" >&2
    exit 2
    ;;
esac
EOF
chmod +x "${temporary_directory}/bin/aws"

cat >"${temporary_directory}/bundle/release-catalog-entries.json" <<'EOF'
{
  "schema_version": 1,
  "producer": "shared-workflows",
  "source": {
    "artifact": "conda",
    "repository": "rapidsai/example",
    "sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "workflow_ref": "rapidsai/example/.github/workflows/build.yaml@refs/heads/main",
    "run_id": "101",
    "run_attempt": "1",
    "matrix": {"arch": "x86_64", "cuda_version": "12.0", "python_version": "3.12"}
  },
  "entries": [{
    "release_catalog_key": "conda:example",
    "path": "linux-64/example-26.10.00.conda",
    "sbom": "release-evidence/example.spdx.json",
    "provenance": "release-evidence/example.intoto.jsonl",
    "package": {"ecosystem": "conda", "name": "example", "version": "26.10.00", "platform": "linux-64"}
  }]
}
EOF
mkdir -p "${temporary_directory}/bundle/linux-64"
printf 'package bytes\n' >"${temporary_directory}/bundle/linux-64/example-26.10.00.conda"
printf '{"spdxVersion":"SPDX-2.3"}\n' >"${temporary_directory}/bundle/release-evidence/example.spdx.json"
printf '{"run":"101"}\n' >"${temporary_directory}/bundle/release-evidence/example.intoto.jsonl"
printf '{"schema_version":1,"dependencies":[]}\n' >"${temporary_directory}/upstream-inputs.json"

run_upload() {
  GITHUB_RUN_ATTEMPT="$1" \
    PATH="${temporary_directory}/bin:${PATH}" \
    FAKE_S3="${temporary_directory}/s3" \
    RELEASE_ARTIFACT_DIRECTORY="${temporary_directory}/bundle" \
    RELEASE_CANDIDATE_BUCKET="candidate-store" \
    RELEASE_CANDIDATE_CONTENT_PREFIX="artifacts" \
    RELEASE_CANDIDATE_TRAIN_PREFIX="train-state" \
    RELEASE_CANDIDATE_TRAIN_SHA256="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb" \
    RELEASE_SOURCE_ARTIFACT_NAME="conda" \
    GITHUB_REPOSITORY="rapidsai/example" \
    GITHUB_RUN_ID="101" \
    GITHUB_STEP_SUMMARY="${temporary_directory}/summary" \
    RELEASE_CANDIDATE_UPSTREAM_INPUTS="${temporary_directory}/upstream-inputs.json" \
    "${repository_root}/release-catalog/upload-s3.sh"
}

run_upload 1
artifact_root="${temporary_directory}/s3/candidate-store/artifacts/rapidsai/example"
artifact_digest="$(find "${artifact_root}" -mindepth 1 -maxdepth 1 -type d -exec basename {} \;)"
canonical="${artifact_root}/${artifact_digest}/conda"
train="${temporary_directory}/s3/candidate-store/train-state/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb/rapidsai/example/conda"
test -f "${canonical}/artifact-index.json"
test -f "${canonical}/linux-64/example-26.10.00.conda"
test -f "${train}/101.1/release-catalog-entries.json"
test -f "${train}/101.1/release-evidence/example.intoto.jsonl"
test ! -d "${artifact_root}/${artifact_digest}/attempts"
jq -e '.upstream_dependencies == []' "${canonical}/build-record.json" >/dev/null
if jq -e 'tostring | test("run_id|run_attempt")' "${canonical}/artifact-index.json" >/dev/null; then
  echo "canonical artifact index contains GitHub execution metadata" >&2
  exit 1
fi
jq -e '.source_run_id == "101" and .source_run_attempt == "1"' \
  "${train}/101.1/bundle-reference.json" >/dev/null

run_upload 2
test -f "${train}/101.2/bundle-reference.json"
cat >"${temporary_directory}/upstream-inputs.json" <<'EOF'
{
  "schema_version": 1,
  "dependencies": [{
    "artifact_key": "artifacts/rapidsai/rapids-logger/input/conda",
    "path": "noarch/rapids-logger-0.3.0.conda",
    "release_catalog_key": "conda:rapids-logger",
    "producer": {
      "repository": "rapidsai/rapids-logger",
      "sha": "cccccccccccccccccccccccccccccccccccccccc",
      "build_input_digest": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"
    },
    "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "package": {"ecosystem": "conda", "name": "rapids-logger", "version": "0.3.0", "platform": "noarch"}
  }]
}
EOF
run_upload 3
mapfile -t artifact_digests < <(find "${artifact_root}" -mindepth 1 -maxdepth 1 -type d -exec basename {} \; | sort)
if [[ "${#artifact_digests[@]}" -ne 2 || "${artifact_digests[0]}" == "${artifact_digests[1]}" ]]; then
  echo "upstream input change did not create a distinct build-input digest" >&2
  exit 1
fi
test -f "${train}/101.3/bundle-reference.json"
upstream_digest="${artifact_digests[0]}"
if [[ "${upstream_digest}" == "${artifact_digest}" ]]; then
  upstream_digest="${artifact_digests[1]}"
fi
jq -e '
  .upstream_dependencies == [{
    artifact_key: "artifacts/rapidsai/rapids-logger/input/conda",
    path: "noarch/rapids-logger-0.3.0.conda",
    release_catalog_key: "conda:rapids-logger",
    producer: {
      repository: "rapidsai/rapids-logger",
      sha: "cccccccccccccccccccccccccccccccccccccccc",
      build_input_digest: "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"
    },
    sha256: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    package: {
      ecosystem: "conda", name: "rapids-logger", version: "0.3.0", platform: "noarch"
    }
  }]
' "${artifact_root}/${upstream_digest}/conda/build-record.json" >/dev/null
printf 'different bytes\n' >"${temporary_directory}/bundle/linux-64/example-26.10.00.conda"
if run_upload 4 >/dev/null 2>&1; then
  echo "upload accepted different canonical package bytes" >&2
  exit 1
fi
