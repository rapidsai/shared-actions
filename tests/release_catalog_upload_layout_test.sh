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

if [[ "$1" == "s3" && "$2" == "cp" ]]; then
  source="$3"
  destination="$4"
  bucket_key="${source#s3://}"
  bucket="${bucket_key%%/*}"
  key="${bucket_key#*/}"
  cp "${FAKE_S3}/${bucket}/${key}" "${destination}"
  exit 0
fi
[[ "$1" == "s3api" ]]
operation="$2"
shift 2
bucket=""
key=""
body=""
conditional=""
while [[ "$#" -gt 0 ]]; do
  case "$1" in
    --bucket) bucket="$2"; shift 2 ;;
    --key) key="$2"; shift 2 ;;
    --body) body="$2"; shift 2 ;;
    --if-none-match) conditional="$2"; shift 2 ;;
    --tagging)
      echo "AccessDenied: producers cannot set retention tags, including during creation" >&2
      exit 1
      ;;
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
    [[ "${conditional}" == '*' ]]
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

cat >"${temporary_directory}/bundle/release-catalog-entries.conda.json" <<'EOF'
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
implementation_repositories='{"gha-tools":"rapidsai/gha-tools","shared-actions":"rapidsai/shared-actions","shared-workflows":"rapidsai/shared-workflows"}'
cat >"${temporary_directory}/release-train.json" <<'EOF'
{
  "release_units": [
    {"id":"conda:example","artifact_family":"conda","dependencies":["source:foo"]},
    {"id":"source:foo","artifact_family":"source","repository":"foo","source_revision":"ffffffffffffffffffffffffffffffffffffffff"}
  ],
  "source_revisions": {"foo":"ffffffffffffffffffffffffffffffffffffffff"},
  "github_repositories": {"foo":"rapidsai/foo"},
  "build_tool_revisions": {
    "dependency-file-generator":"1111111111111111111111111111111111111111",
    "rapids-build-backend":"2222222222222222222222222222222222222222",
    "rapids-cmake":"3333333333333333333333333333333333333333"
  }
}
EOF
train_sha256="$(jq -cSj 'del(.train_sha256)' "${temporary_directory}/release-train.json" | sha256sum | awk '{print $1}')"
mkdir -p "${temporary_directory}/s3/candidate-store/train-state/${train_sha256}"
cp "${temporary_directory}/release-train.json" \
  "${temporary_directory}/s3/candidate-store/train-state/${train_sha256}/release-train.json"
bash "${repository_root}/release-candidate-dependencies/write-source-inputs.sh" \
  --train "${temporary_directory}/release-train.json" \
  --output "${temporary_directory}/source-inputs.json" \
  --target-unit conda:example
jq -cnS \
  --argjson source_inputs "$(cat "${temporary_directory}/source-inputs.json")" \
  '{schema_version:1,train_inputs:{lock_receipt_sha256:"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",variant_receipt_sha256:"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"},build_policy:{sccache:true},build_implementation_repositories:{"gha-tools":"rapidsai/gha-tools","shared-actions":"rapidsai/shared-actions","shared-workflows":"rapidsai/shared-workflows"},source_inputs:$source_inputs}' \
  >"${temporary_directory}/candidate-build-inputs.json"

run_upload() {
  local implementation_revisions="$2"
  local repositories="${7:-${implementation_repositories}}"
  jq -cS --argjson repositories "${repositories}" \
    '. + {build_implementation_repositories: $repositories}' \
    "${temporary_directory}/candidate-build-inputs.json" \
    >"${temporary_directory}/candidate-build-inputs.normalized.json"
  mv "${temporary_directory}/candidate-build-inputs.normalized.json" \
    "${temporary_directory}/candidate-build-inputs.json"
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
    RELEASE_CANDIDATE_TRAIN_SHA256="${train_sha256}" \
    RELEASE_SOURCE_ARTIFACT_NAME="conda" \
    GITHUB_REPOSITORY="rapidsai/example" \
    GITHUB_RUN_ID="101" \
    GITHUB_STEP_SUMMARY="${temporary_directory}/summary" \
    RELEASE_CANDIDATE_BUILD_IMPLEMENTATION_REVISIONS="${implementation_revisions}" \
    RELEASE_CANDIDATE_BUILD_IMPLEMENTATION_REPOSITORIES="${repositories}" \
    RELEASE_CANDIDATE_GHA_TOOLS_REVISION="${gha_tools_revision}" \
    RELEASE_CANDIDATE_CATALOG_SHARED_ACTIONS_REPOSITORY="${6:-rapidsai/shared-actions}" \
    RELEASE_CANDIDATE_CATALOG_SHARED_ACTIONS_REVISION="${shared_actions_revision}" \
    RELEASE_CANDIDATE_UPSTREAM_INPUTS="${temporary_directory}/upstream-inputs.json" \
    RELEASE_CANDIDATE_BUILD_INPUTS="${temporary_directory}/candidate-build-inputs.json" \
    RELEASE_CANDIDATE_SOURCE_INPUTS="${temporary_directory}/source-inputs.json" \
    RAPIDS_DATETIME_STRING="${build_datetime}" \
    "${repository_root}/release-catalog/upload-candidate-s3.sh"
}

base_implementation_revisions='{"gha-tools":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","shared-actions":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","shared-workflows":"cccccccccccccccccccccccccccccccccccccccc"}'
changed_implementation_revisions='{"gha-tools":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","shared-actions":"dddddddddddddddddddddddddddddddddddddddd","shared-workflows":"cccccccccccccccccccccccccccccccccccccccc"}'

run_upload 1 "${base_implementation_revisions}"
initial_created="$(wc -l <"${temporary_directory}/put-objects.log")"
run_upload 1 "${base_implementation_revisions}"
[[ "$(wc -l <"${temporary_directory}/put-objects.log")" == "${initial_created}" ]]
artifact_root="${temporary_directory}/s3/candidate-store/artifacts/example"
artifact_digest="$(find "${artifact_root}" -mindepth 1 -maxdepth 1 -type d -exec basename {} \;)"
canonical="${artifact_root}/${artifact_digest}"
train="${temporary_directory}/s3/candidate-store/train-state/${train_sha256}/example"
test -f "${canonical}/artifact-index.json"
test -f "${canonical}/linux-64/example-26.10.00.conda"
test -f "${train}/101.1/${artifact_digest}/release-catalog-entries.conda.json"
test -f "${train}/101.1/${artifact_digest}/release-evidence/example.intoto.jsonl"
test ! -d "${artifact_root}/${artifact_digest}/attempts"
jq -e '.upstream_dependencies == []' "${canonical}/build-record.json" >/dev/null
jq -e '.schema_version == 5 and .storage_layout == 2 and .build_datetime == "260901120000"' "${canonical}/build-record.json" >/dev/null
jq -e '.candidate_build_inputs.build_implementation_repositories == {"gha-tools":"rapidsai/gha-tools","shared-actions":"rapidsai/shared-actions","shared-workflows":"rapidsai/shared-workflows"}' "${canonical}/build-record.json" >/dev/null
jq -e '.candidate_build_inputs.source_inputs.sources.foo == {"repository":"rapidsai/foo","revision":"ffffffffffffffffffffffffffffffffffffffff"} and (.candidate_build_inputs.source_inputs.build_tools | length) == 3' "${canonical}/build-record.json" >/dev/null
jq -e '.schema_version == 3 and .source_artifact == "conda"' "${canonical}/artifact-index.json" >/dev/null
artifact_line="$(grep -nF "${artifact_digest}/linux-64/example-26.10.00.conda" "${temporary_directory}/put-objects.log" | cut -d: -f1)"
index_line="$(grep -nF "${artifact_digest}/artifact-index.json" "${temporary_directory}/put-objects.log" | cut -d: -f1)"
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
  "${train}/101.1/${artifact_digest}/bundle-reference.conda.json" >/dev/null
cp "${temporary_directory}/source-inputs.json" "${temporary_directory}/source-inputs.valid.json"
jq '.sources.foo.revision = "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"' \
  "${temporary_directory}/source-inputs.valid.json" >"${temporary_directory}/source-inputs.json"
if run_upload 6 "${base_implementation_revisions}" >/dev/null 2>&1; then
  echo "upload accepted source inputs that differ from the frozen train" >&2
  exit 1
fi
mv "${temporary_directory}/source-inputs.valid.json" "${temporary_directory}/source-inputs.json"
if run_upload 9 "${base_implementation_revisions}" "dddddddddddddddddddddddddddddddddddddddd" >/dev/null 2>&1; then
  echo "upload accepted a gha-tools revision that does not match the release train" >&2
  exit 1
fi
if run_upload 9 "${base_implementation_revisions}" "" "dddddddddddddddddddddddddddddddddddddddd" >/dev/null 2>&1; then
  echo "upload accepted a shared-actions revision that does not match the release train" >&2
  exit 1
fi

run_upload 2 "${changed_implementation_revisions}"
test -n "$(find "${train}/101.2" -name 'bundle-reference.conda.json' -print -quit)"
mapfile -t artifact_digests < <(find "${artifact_root}" -mindepth 1 -maxdepth 1 -type d -exec basename {} \; | sort)
if [[ "${#artifact_digests[@]}" -ne 2 || "${artifact_digests[0]}" == "${artifact_digests[1]}" ]]; then
  echo "shared build implementation change did not create a distinct build-input digest" >&2
  exit 1
fi
cat >"${temporary_directory}/upstream-inputs.json" <<'EOF'
{
  "schema_version": 1,
  "dependencies": [{
    "artifact_key": "artifacts/rapids-logger/input",
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
test -n "$(find "${train}/101.3" -name 'bundle-reference.conda.json' -print -quit)"
upstream_digest=""
for candidate_digest in "${artifact_digests[@]}"; do
  candidate_record="${artifact_root}/${candidate_digest}/build-record.json"
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
    artifact_key: "artifacts/rapids-logger/input",
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
' "${artifact_root}/${upstream_digest}/build-record.json" >/dev/null
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
jq -cnS --argjson source_inputs "$(cat "${temporary_directory}/source-inputs.json")" '{schema_version:1,train_inputs:{lock_receipt_sha256:"dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",variant_receipt_sha256:"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"},build_policy:{sccache:true},source_inputs:$source_inputs}' >"${temporary_directory}/candidate-build-inputs.json"
run_upload 7 "${base_implementation_revisions}"
mapfile -t artifact_digests < <(find "${artifact_root}" -mindepth 1 -maxdepth 1 -type d -exec basename {} \; | sort)
if [[ "${#artifact_digests[@]}" -ne 6 ]]; then
  echo "train lock receipt change did not create a distinct build-input digest" >&2
  exit 1
fi
jq -cnS --argjson source_inputs "$(cat "${temporary_directory}/source-inputs.json")" '{schema_version:1,train_inputs:{lock_receipt_sha256:"dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",variant_receipt_sha256:"eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"},build_policy:{sccache:true},source_inputs:$source_inputs}' >"${temporary_directory}/candidate-build-inputs.json"
run_upload 8 "${base_implementation_revisions}"
mapfile -t artifact_digests < <(find "${artifact_root}" -mindepth 1 -maxdepth 1 -type d -exec basename {} \; | sort)
if [[ "${#artifact_digests[@]}" -ne 7 ]]; then
  echo "train variant receipt change did not create a distinct build-input digest" >&2
  exit 1
fi
jq -cnS --argjson source_inputs "$(cat "${temporary_directory}/source-inputs.json")" '{schema_version:1,train_inputs:{lock_receipt_sha256:"dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",variant_receipt_sha256:"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"},build_policy:{sccache:false},source_inputs:$source_inputs}' >"${temporary_directory}/candidate-build-inputs.json"
run_upload 9 "${base_implementation_revisions}"
mapfile -t artifact_digests < <(find "${artifact_root}" -mindepth 1 -maxdepth 1 -type d -exec basename {} \; | sort)
if [[ "${#artifact_digests[@]}" -ne 8 ]]; then
  echo "sccache policy change did not create a distinct build-input digest" >&2
  exit 1
fi
fork_implementation_repositories='{"gha-tools":"rapidsai/gha-tools","shared-actions":"msarahan/shared-actions","shared-workflows":"rapidsai/shared-workflows"}'
run_upload 10 "${base_implementation_revisions}" "" "" "260901120000" "msarahan/shared-actions" "${fork_implementation_repositories}"
mapfile -t artifact_digests < <(find "${artifact_root}" -mindepth 1 -maxdepth 1 -type d -exec basename {} \; | sort)
if [[ "${#artifact_digests[@]}" -ne 9 ]]; then
  echo "implementation repository mapping change did not create a distinct build-input digest" >&2
  exit 1
fi
fork_record="$(find "${artifact_root}" -name build-record.json -exec grep -l 'msarahan/shared-actions' {} \; | head -n 1)"
jq -e '.build_implementation_repositories."shared-actions" == "msarahan/shared-actions"' "${fork_record}" >/dev/null
jq '.source_revisions.foo = "9999999999999999999999999999999999999999" | .release_units[1].source_revision = "9999999999999999999999999999999999999999"' \
  "${temporary_directory}/release-train.json" >"${temporary_directory}/release-train.changed.json"
mv "${temporary_directory}/release-train.changed.json" "${temporary_directory}/release-train.json"
train_sha256="$(jq -cSj 'del(.train_sha256)' "${temporary_directory}/release-train.json" | sha256sum | awk '{print $1}')"
mkdir -p "${temporary_directory}/s3/candidate-store/train-state/${train_sha256}"
cp "${temporary_directory}/release-train.json" \
  "${temporary_directory}/s3/candidate-store/train-state/${train_sha256}/release-train.json"
bash "${repository_root}/release-candidate-dependencies/write-source-inputs.sh" \
  --train "${temporary_directory}/release-train.json" \
  --output "${temporary_directory}/source-inputs.json" \
  --target-unit conda:example
jq -cS --argjson source_inputs "$(cat "${temporary_directory}/source-inputs.json")" \
  '.source_inputs = $source_inputs' "${temporary_directory}/candidate-build-inputs.json" \
  >"${temporary_directory}/candidate-build-inputs.changed.json"
mv "${temporary_directory}/candidate-build-inputs.changed.json" \
  "${temporary_directory}/candidate-build-inputs.json"
run_upload 11 "${base_implementation_revisions}"
mapfile -t artifact_digests < <(find "${artifact_root}" -mindepth 1 -maxdepth 1 -type d -exec basename {} \; | sort)
if [[ "${#artifact_digests[@]}" -ne 10 ]]; then
  echo "source-only dependency revision change did not create a distinct build-input digest" >&2
  exit 1
fi

# The legacy uploader must also work with a producer identity that cannot tag
# newly created objects. Its identical retry must not rewrite any bytes.
cp "${temporary_directory}/bundle/release-catalog-entries.conda.json" \
  "${temporary_directory}/bundle/release-catalog-entries.json"
run_legacy_upload() {
  PATH="${temporary_directory}/bin:${PATH}" \
    FAKE_S3="${temporary_directory}/s3" \
    FAKE_LOG="${temporary_directory}/legacy-put-objects.log" \
    RELEASE_ARTIFACT_DIRECTORY="${temporary_directory}/bundle" \
    RELEASE_CANDIDATE_BUCKET="candidate-store" \
    RELEASE_CANDIDATE_PREFIX="legacy" \
    RELEASE_CANDIDATE_TRAIN_SHA256="${train_sha256}" \
    RELEASE_SOURCE_ARTIFACT_NAME="conda" \
    GITHUB_REPOSITORY="rapidsai/example" \
    GITHUB_RUN_ID=101 \
    GITHUB_STEP_SUMMARY="${temporary_directory}/summary" \
    "${repository_root}/release-catalog/upload-s3.sh"
}
run_legacy_upload
legacy_created="$(wc -l <"${temporary_directory}/legacy-put-objects.log")"
run_legacy_upload
[[ "$(wc -l <"${temporary_directory}/legacy-put-objects.log")" == "${legacy_created}" ]]
printf 'different bytes\n' >"${temporary_directory}/bundle/linux-64/example-26.10.00.conda"
if run_legacy_upload >/dev/null 2>&1; then
  echo "legacy upload accepted changed existing bytes" >&2
  exit 1
fi
if run_upload 12 "${base_implementation_revisions}" >/dev/null 2>&1; then
  echo "upload accepted different canonical package bytes" >&2
  exit 1
fi
