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
    printf '%s\n' "${key}" >>"${FAKE_LOG}"
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
printf '%s\n' '{"schema_version":1,"train_inputs":{"lock_receipt_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","variant_receipt_sha256":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"},"build_policy":{"sccache":true}}' >"${temporary_directory}/candidate-build-inputs.json"

run_upload() {
  local implementation_revisions="$2"
  local gha_tools_revision
  local shared_actions_revision
  local build_datetime
  gha_tools_revision="$(jq -r '."gha-tools"' <<<"${implementation_revisions}")"
  shared_actions_revision="$(jq -r '."shared-actions"' <<<"${implementation_revisions}")"
  gha_tools_revision="${3:-${gha_tools_revision}}"
  shared_actions_revision="${4:-${shared_actions_revision}}"
  build_datetime="${5-260901120000}"
  GITHUB_RUN_ATTEMPT="$1" \
    PATH="${temporary_directory}/bin:${PATH}" \
    FAKE_S3="${temporary_directory}/s3" \
    FAKE_LOG="${temporary_directory}/put-objects.log" \
    RELEASE_ARTIFACT_DIRECTORY="${temporary_directory}/bundle" \
    RELEASE_CANDIDATE_BUCKET="candidate-store" \
    RELEASE_CANDIDATE_CONTENT_PREFIX="artifacts" \
    RELEASE_CANDIDATE_TRAIN_PREFIX="train-state" \
    RELEASE_CANDIDATE_TRAIN_SHA256="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb" \
    RELEASE_SOURCE_ARTIFACT_NAME="conda" \
    GITHUB_REPOSITORY="rapidsai/example" \
    GITHUB_RUN_ID="101" \
    GITHUB_STEP_SUMMARY="${temporary_directory}/summary" \
    RELEASE_CANDIDATE_BUILD_IMPLEMENTATION_REVISIONS="${implementation_revisions}" \
    RELEASE_CANDIDATE_GHA_TOOLS_REVISION="${gha_tools_revision}" \
    RELEASE_CANDIDATE_CATALOG_SHARED_ACTIONS_REPOSITORY="rapidsai/shared-actions" \
    RELEASE_CANDIDATE_CATALOG_SHARED_ACTIONS_REVISION="${shared_actions_revision}" \
    RELEASE_CANDIDATE_UPSTREAM_INPUTS="${temporary_directory}/upstream-inputs.json" \
    RELEASE_CANDIDATE_BUILD_INPUTS="${temporary_directory}/candidate-build-inputs.json" \
    RAPIDS_DATETIME_STRING="${build_datetime}" \
    "${repository_root}/release-catalog/upload-s3.sh"
}

base_implementation_revisions='{"gha-tools":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","shared-actions":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","shared-workflows":"cccccccccccccccccccccccccccccccccccccccc"}'
changed_implementation_revisions='{"gha-tools":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","shared-actions":"dddddddddddddddddddddddddddddddddddddddd","shared-workflows":"cccccccccccccccccccccccccccccccccccccccc"}'

run_upload 1 "${base_implementation_revisions}"
artifact_root="${temporary_directory}/s3/candidate-store/artifacts/example"
artifact_digest="$(find "${artifact_root}" -mindepth 1 -maxdepth 1 -type d -exec basename {} \;)"
canonical="${artifact_root}/${artifact_digest}/conda"
train="${temporary_directory}/s3/candidate-store/train-state/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb/example/conda"
test -f "${canonical}/artifact-index.json"
test -f "${canonical}/linux-64/example-26.10.00.conda"
test -f "${train}/101.1/release-catalog-entries.json"
test -f "${train}/101.1/release-evidence/example.intoto.jsonl"
test ! -d "${artifact_root}/${artifact_digest}/attempts"
jq -e '.upstream_dependencies == []' "${canonical}/build-record.json" >/dev/null
jq -e '.schema_version == 4 and .build_datetime == "260901120000"' "${canonical}/build-record.json" >/dev/null
jq -e '.candidate_build_inputs == {"schema_version":1,"train_inputs":{"lock_receipt_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","variant_receipt_sha256":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"},"build_policy":{"sccache":true}}' "${canonical}/build-record.json" >/dev/null
jq -e '.schema_version == 2' "${canonical}/artifact-index.json" >/dev/null
artifact_line="$(grep -nF "${artifact_digest}/conda/linux-64/example-26.10.00.conda" "${temporary_directory}/put-objects.log" | cut -d: -f1)"
index_line="$(grep -nF "${artifact_digest}/conda/artifact-index.json" "${temporary_directory}/put-objects.log" | cut -d: -f1)"
if [[ -z "${artifact_line}" || -z "${index_line}" || "${index_line}" -le "${artifact_line}" ]]; then
  echo "canonical artifact index was not committed after its artifact bytes" >&2
  exit 1
fi
jq -e '.build_implementation_revisions == {"gha-tools":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","shared-actions":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","shared-workflows":"cccccccccccccccccccccccccccccccccccccccc"}' "${canonical}/build-record.json" >/dev/null
if jq -e 'tostring | test("run_id|run_attempt")' "${canonical}/artifact-index.json" >/dev/null; then
  echo "canonical artifact index contains GitHub execution metadata" >&2
  exit 1
fi
jq -e '.source_run_id == "101" and .source_run_attempt == "1"' \
  "${train}/101.1/bundle-reference.json" >/dev/null
if run_upload 9 "${base_implementation_revisions}" "dddddddddddddddddddddddddddddddddddddddd" >/dev/null 2>&1; then
  echo "upload accepted a gha-tools revision that does not match the release train" >&2
  exit 1
fi
if run_upload 9 "${base_implementation_revisions}" "" "dddddddddddddddddddddddddddddddddddddddd" >/dev/null 2>&1; then
  echo "upload accepted a shared-actions revision that does not match the release train" >&2
  exit 1
fi

run_upload 2 "${changed_implementation_revisions}"
test -f "${train}/101.2/bundle-reference.json"
mapfile -t artifact_digests < <(find "${artifact_root}" -mindepth 1 -maxdepth 1 -type d -exec basename {} \; | sort)
if [[ "${#artifact_digests[@]}" -ne 2 || "${artifact_digests[0]}" == "${artifact_digests[1]}" ]]; then
  echo "shared build implementation change did not create a distinct build-input digest" >&2
  exit 1
fi
cat >"${temporary_directory}/upstream-inputs.json" <<'EOF'
{
  "schema_version": 1,
  "dependencies": [{
    "artifact_key": "artifacts/rapids-logger/input/conda",
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
run_upload 3 "${base_implementation_revisions}"
mapfile -t artifact_digests < <(find "${artifact_root}" -mindepth 1 -maxdepth 1 -type d -exec basename {} \; | sort)
if [[ "${#artifact_digests[@]}" -ne 3 ]]; then
  echo "upstream input change did not create a distinct build-input digest" >&2
  exit 1
fi
test -f "${train}/101.3/bundle-reference.json"
upstream_digest=""
for candidate_digest in "${artifact_digests[@]}"; do
  candidate_record="${artifact_root}/${candidate_digest}/conda/build-record.json"
  if jq -e '.upstream_dependencies | length == 1' "${candidate_record}" >/dev/null; then
    upstream_digest="${candidate_digest}"
    break
  fi
done
if [[ -z "${upstream_digest}" ]]; then
  echo "unable to find the canonical artifact record with the upstream dependency" >&2
  exit 1
fi
jq -e '
  .upstream_dependencies == [{
    artifact_key: "artifacts/rapids-logger/input/conda",
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
run_upload 4 "${base_implementation_revisions}" "" "" "260901120001"
mapfile -t artifact_digests < <(find "${artifact_root}" -mindepth 1 -maxdepth 1 -type d -exec basename {} \; | sort)
if [[ "${#artifact_digests[@]}" -ne 4 ]]; then
  echo "build timestamp change did not create a distinct build-input digest" >&2
  exit 1
fi
run_upload 5 "${base_implementation_revisions}" "" "" ""
mapfile -t artifact_digests < <(find "${artifact_root}" -mindepth 1 -maxdepth 1 -type d -exec basename {} \; | sort)
if [[ "${#artifact_digests[@]}" -ne 5 ]]; then
  echo "empty build timestamp was not accepted as a stable build input" >&2
  exit 1
fi
printf '%s\n' '{"schema_version":1,"train_inputs":{"lock_receipt_sha256":"dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd","variant_receipt_sha256":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"},"build_policy":{"sccache":true}}' >"${temporary_directory}/candidate-build-inputs.json"
run_upload 7 "${base_implementation_revisions}"
mapfile -t artifact_digests < <(find "${artifact_root}" -mindepth 1 -maxdepth 1 -type d -exec basename {} \; | sort)
if [[ "${#artifact_digests[@]}" -ne 6 ]]; then
  echo "train lock receipt change did not create a distinct build-input digest" >&2
  exit 1
fi
printf '%s\n' '{"schema_version":1,"train_inputs":{"lock_receipt_sha256":"dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd","variant_receipt_sha256":"eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"},"build_policy":{"sccache":true}}' >"${temporary_directory}/candidate-build-inputs.json"
run_upload 8 "${base_implementation_revisions}"
mapfile -t artifact_digests < <(find "${artifact_root}" -mindepth 1 -maxdepth 1 -type d -exec basename {} \; | sort)
if [[ "${#artifact_digests[@]}" -ne 7 ]]; then
  echo "train variant receipt change did not create a distinct build-input digest" >&2
  exit 1
fi
printf '%s\n' '{"schema_version":1,"train_inputs":{"lock_receipt_sha256":"dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd","variant_receipt_sha256":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"},"build_policy":{"sccache":false}}' >"${temporary_directory}/candidate-build-inputs.json"
run_upload 9 "${base_implementation_revisions}"
mapfile -t artifact_digests < <(find "${artifact_root}" -mindepth 1 -maxdepth 1 -type d -exec basename {} \; | sort)
if [[ "${#artifact_digests[@]}" -ne 8 ]]; then
  echo "sccache policy change did not create a distinct build-input digest" >&2
  exit 1
fi
printf '%s\n' '{"schema_version":1,"train_inputs":{"lock_receipt_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","variant_receipt_sha256":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"},"build_policy":{"sccache":true}}' >"${temporary_directory}/candidate-build-inputs.json"
printf 'different bytes\n' >"${temporary_directory}/bundle/linux-64/example-26.10.00.conda"
if run_upload 6 "${base_implementation_revisions}" >/dev/null 2>&1; then
  echo "upload accepted different canonical package bytes" >&2
  exit 1
fi
