#!/usr/bin/env bash
# Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.

set -euo pipefail

require_nonempty() {
  local name="$1"
  local value="$2"
  if [[ -z "${value}" ]]; then
    echo "${name} must be a non-empty string" >&2
    exit 1
  fi
}

require_nonempty "RELEASE_CATALOG_KEY" "${RELEASE_CATALOG_KEY:-}"
require_nonempty "RELEASE_OUTPUT_DIRECTORY" "${RELEASE_OUTPUT_DIRECTORY:-}"
require_nonempty "RELEASE_ENTRIES_NAME" "${RELEASE_ENTRIES_NAME:-}"
require_nonempty "RELEASE_ARTIFACTS" "${RELEASE_ARTIFACTS:-}"
require_nonempty "RELEASE_SOURCE_ARTIFACT_NAME" "${RELEASE_SOURCE_ARTIFACT_NAME:-}"

source_sha="${RELEASE_SOURCE_SHA:-${GITHUB_SHA:-}}"
require_nonempty "RELEASE_SOURCE_SHA or GITHUB_SHA" "${source_sha}"

require_plain_filename() {
  local label="$1"
  local filename="$2"
  if [[ "${filename}" == */* || "${filename}" == .* || "${filename}" == *".."* ]]; then
    echo "${label} must be a plain filename" >&2
    exit 1
  fi
}

require_plain_filename "entries-name" "${RELEASE_ENTRIES_NAME}"

if [[ ! -d "${RELEASE_OUTPUT_DIRECTORY}" ]]; then
  echo "output-directory does not exist or is not a directory: ${RELEASE_OUTPUT_DIRECTORY}" >&2
  exit 1
fi

ensure_relative_pattern() {
  local field="$1"
  local pattern="$2"
  if [[ "${pattern}" == /* || "${pattern}" == */../* || "${pattern}" == ../* || "${pattern}" == *"/.." ]]; then
    echo "${field} must be a relative path inside output-directory: ${pattern}" >&2
    exit 1
  fi
}

output_directory="$(realpath "${RELEASE_OUTPUT_DIRECTORY}")"

if ! jq -e 'type == "array" and length > 0' <<<"${RELEASE_ARTIFACTS}" >/dev/null; then
  echo "release-artifacts must be a non-empty JSON array" >&2
  exit 1
fi

entries_path="${output_directory}/${RELEASE_ENTRIES_NAME}"
temporary_manifest="$(mktemp "${output_directory}/.release-catalog.XXXXXX")"
trap 'rm -f "${temporary_manifest}"' EXIT

printf '%s\n' '{"entries":[]}' >"${temporary_manifest}"

resolve_one_file() {
  local field="$1"
  local pattern="$2"
  local -a matches=()

  ensure_relative_pattern "${field}" "${pattern}"
  while IFS= read -r match; do
    matches+=("${match}")
  done < <(compgen -G "${output_directory}/${pattern}" || true)
  if [[ "${#matches[@]}" -ne 1 || ! -f "${matches[0]:-}" ]]; then
    echo "${field} pattern must resolve to exactly one file: ${pattern}" >&2
    exit 1
  fi

  local resolved
  resolved="$(realpath "${matches[0]}")"
  if [[ "${resolved}" != "${output_directory}"/* ]]; then
    echo "${field} must resolve inside output-directory: ${pattern}" >&2
    exit 1
  fi
  printf '%s\n' "${resolved#"${output_directory}/"}"
}

validate_package_identity() {
  jq -e '
    type == "object"
    and (keys - ["ecosystem", "name", "version", "build", "platform"] | length == 0)
    and (.ecosystem | type == "string" and length > 0)
    and (.name | type == "string" and length > 0)
    and (.version | type == "string" and length > 0)
    and ((has("build") | not) or (.build | type == "string" and length > 0))
    and ((has("platform") | not) or (.platform | type == "string" and length > 0))
  ' >/dev/null
}

generated_evidence_path() {
  local primary_path="$1"
  local kind="$2"
  local artifact_digest
  artifact_digest="$(sha256sum "${output_directory}/${primary_path}" | awk '{print $1}')"
  local artifact_label="${primary_path//\//_}"
  printf 'release-evidence/%s.%s.%s.json\n' "${artifact_label}" "${artifact_digest}" "${kind}"
}

copy_supplied_evidence() {
  local primary_path="$1"
  local supplied_path="$2"
  local kind="$3"
  local artifact_digest
  artifact_digest="$(sha256sum "${output_directory}/${primary_path}" | awk '{print $1}')"
  local artifact_label="${primary_path//\//_}"
  local destination
  destination="release-evidence/${artifact_label}.${artifact_digest}/${kind}-$(basename "${supplied_path}")"
  mkdir -p "$(dirname "${output_directory}/${destination}")"
  cp "${output_directory}/${supplied_path}" "${output_directory}/${destination}"
  printf '%s\n' "${destination}"
}

write_generated_sbom() {
  local primary_path="$1"
  local package="$2"
  local destination="$3"
  local artifact_digest
  artifact_digest="$(sha256sum "${output_directory}/${primary_path}" | awk '{print $1}')"
  mkdir -p "$(dirname "${output_directory}/${destination}")"
  jq -n -S \
    --arg artifact_digest "${artifact_digest}" \
    --arg artifact_path "${primary_path}" \
    --argjson package "${package}" \
    '{
      spdxVersion: "SPDX-2.3",
      dataLicense: "CC0-1.0",
      SPDXID: "SPDXRef-DOCUMENT",
      name: ("RAPIDS release artifact " + $artifact_path),
      documentNamespace: ("https://rapids.ai/release-platform/spdx/" + $artifact_digest),
      creationInfo: {
        creators: ["Tool: rapidsai/shared-workflows release catalog"],
        created: (now | strftime("%Y-%m-%dT%H:%M:%SZ"))
      },
      documentDescribes: ["SPDXRef-Artifact"],
      packages: [{
        SPDXID: "SPDXRef-Artifact",
        name: $package.name,
        versionInfo: $package.version,
        downloadLocation: "NOASSERTION",
        filesAnalyzed: false,
        checksums: [{algorithm: "SHA256", checksumValue: $artifact_digest}]
      }],
      relationships: [{
        spdxElementId: "SPDXRef-DOCUMENT",
        relationshipType: "DESCRIBES",
        relatedSpdxElement: "SPDXRef-Artifact"
      }],
      comment: "Artifact-identity SBOM envelope. A producer-supplied dependency SBOM may replace this record."
    }' >"${output_directory}/${destination}"
}

write_generated_provenance() {
  local primary_path="$1"
  local package="$2"
  local destination="$3"
  local artifact_digest
  artifact_digest="$(sha256sum "${output_directory}/${primary_path}" | awk '{print $1}')"
  mkdir -p "$(dirname "${output_directory}/${destination}")"
  jq -n -S \
    --arg artifact_digest "${artifact_digest}" \
    --arg artifact_path "${primary_path}" \
    --arg repository "${GITHUB_REPOSITORY:-}" \
    --arg run_attempt "${GITHUB_RUN_ATTEMPT:-}" \
    --arg run_id "${GITHUB_RUN_ID:-}" \
    --arg source_sha "${source_sha}" \
    --arg workflow_ref "${GITHUB_WORKFLOW_REF:-}" \
    --argjson package "${package}" \
    '{
      _type: "https://in-toto.io/Statement/v1",
      subject: [{name: $artifact_path, digest: {sha256: $artifact_digest}}],
      predicateType: "https://slsa.dev/provenance/v1",
      predicate: {
        buildDefinition: {
          buildType: "https://rapids.ai/release-platform/catalog-record/v1",
          externalParameters: {release_catalog_key: env.RELEASE_CATALOG_KEY, package: $package},
          resolvedDependencies: [{
            uri: ("git+https://github.com/" + $repository + "@" + $source_sha),
            digest: {gitCommit: $source_sha}
          }]
        },
        runDetails: {
          builder: {id: ("https://github.com/" + $workflow_ref)},
          metadata: {invocationId: ("https://github.com/" + $repository + "/actions/runs/" + $run_id + "/attempts/" + $run_attempt)}
        }
      }
    }' >"${output_directory}/${destination}"
}

shopt -s globstar nullglob
while IFS= read -r descriptor; do
  if ! jq -e '
    type == "object"
    and (keys - ["path", "package_identity_file", "sbom", "provenance", "signature", "package"] | length == 0)
    and (.path | type == "string" and length > 0)
    and ((.package_identity_file // "") | type == "string")
    and ((.sbom // "") | type == "string")
    and ((.provenance // "") | type == "string")
    and ((.signature // "") | type == "string")
    and ([has("package"), has("package_identity_file")] | map(select(.)) | length == 1)
  ' <<<"${descriptor}" >/dev/null; then
    echo "release artifact descriptor must contain path, exactly one package identity source, and optional evidence paths: ${descriptor}" >&2
    exit 1
  fi

  primary_path="$(resolve_one_file path "$(jq -r '.path' <<<"${descriptor}")")"
  sbom_pattern="$(jq -r '.sbom // empty' <<<"${descriptor}")"
  provenance_pattern="$(jq -r '.provenance // empty' <<<"${descriptor}")"
  signature_pattern="$(jq -r '.signature // empty' <<<"${descriptor}")"
  package_identity_file="$(jq -r '.package_identity_file // empty' <<<"${descriptor}")"
  if [[ -n "${package_identity_file}" ]]; then
    package_identity_path="$(resolve_one_file package_identity_file "${package_identity_file}")"
    if ! package="$(jq -ce . "${output_directory}/${package_identity_path}" 2>/dev/null)"; then
      echo "package_identity_file must contain valid JSON: ${package_identity_file}" >&2
      exit 1
    fi
  else
    package="$(jq -c '.package' <<<"${descriptor}")"
  fi
  if ! validate_package_identity <<<"${package}"; then
    echo "package identity for ${primary_path} must contain non-empty ecosystem, name, and version strings and only optional build or platform strings" >&2
    exit 1
  fi

  if [[ -n "${sbom_pattern}" ]]; then
    supplied_sbom_path="$(resolve_one_file sbom "${sbom_pattern}")"
    sbom_path="$(copy_supplied_evidence "${primary_path}" "${supplied_sbom_path}" "sbom")"
    sbom_kind="producer-dependency"
  else
    sbom_path="$(generated_evidence_path "${primary_path}" "spdx")"
    write_generated_sbom "${primary_path}" "${package}" "${sbom_path}"
    sbom_kind="generated-identity"
  fi
  if [[ -n "${provenance_pattern}" ]]; then
    supplied_provenance_path="$(resolve_one_file provenance "${provenance_pattern}")"
    provenance_path="$(copy_supplied_evidence "${primary_path}" "${supplied_provenance_path}" "provenance")"
  else
    provenance_path="$(generated_evidence_path "${primary_path}" "provenance")"
    write_generated_provenance "${primary_path}" "${package}" "${provenance_path}"
  fi
  entry="$(jq -cn \
    --arg release_catalog_key "${RELEASE_CATALOG_KEY}" \
    --arg path "${primary_path}" \
    --arg sbom "${sbom_path}" \
    --arg sbom_kind "${sbom_kind}" \
    --arg provenance "${provenance_path}" \
    --argjson package "${package}" \
    '{release_catalog_key: $release_catalog_key, path: $path, sbom: $sbom, sbom_kind: $sbom_kind, provenance: $provenance, package: $package}')"
  if [[ -n "${signature_pattern}" ]]; then
    supplied_signature_path="$(resolve_one_file signature "${signature_pattern}")"
    signature_path="$(copy_supplied_evidence "${primary_path}" "${supplied_signature_path}" "signature")"
    entry="$(jq -c --arg signature "${signature_path}" '. + {signature: $signature}' <<<"${entry}")"
  fi

  jq --argjson entry "${entry}" '.entries += [$entry]' "${temporary_manifest}" >"${temporary_manifest}.next"
  mv "${temporary_manifest}.next" "${temporary_manifest}"
done < <(jq -c '.[]' <<<"${RELEASE_ARTIFACTS}")

if ! jq -e '.entries as $items | ($items | map([.release_catalog_key, .path] | join("\u0000")) | unique | length) == ($items | length)' "${temporary_manifest}" >/dev/null; then
  echo "release-artifacts contains duplicate release_catalog_key/path entries" >&2
  exit 1
fi

jq -n -S \
  --arg artifact_name "${RELEASE_SOURCE_ARTIFACT_NAME}" \
  --arg repository "${GITHUB_REPOSITORY:-}" \
  --arg run_attempt "${GITHUB_RUN_ATTEMPT:-}" \
  --arg run_id "${GITHUB_RUN_ID:-}" \
  --arg sha "${source_sha}" \
  --arg workflow_ref "${GITHUB_WORKFLOW_REF:-}" \
  --argjson entries "$(jq -c '.entries' "${temporary_manifest}")" \
  '{
    schema_version: 1,
    producer: "shared-workflows",
    source: {
      artifact: $artifact_name,
      repository: $repository,
      sha: $sha,
      workflow_ref: $workflow_ref,
      run_id: $run_id,
      run_attempt: $run_attempt
    },
    entries: $entries
  }' >"${entries_path}"
echo "entries-path=${entries_path}" >>"${GITHUB_OUTPUT}"
