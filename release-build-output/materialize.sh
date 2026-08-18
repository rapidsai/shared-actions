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
require_nonempty "RELEASE_MANIFEST_NAME" "${RELEASE_MANIFEST_NAME:-}"
require_nonempty "RELEASE_METADATA_NAME" "${RELEASE_METADATA_NAME:-}"
require_nonempty "RELEASE_ARTIFACTS" "${RELEASE_ARTIFACTS:-}"
require_nonempty "RELEASE_SOURCE_ARTIFACT_NAME" "${RELEASE_SOURCE_ARTIFACT_NAME:-}"

if [[ -n "${RELEASE_PACKAGE:-}" && -n "${RELEASE_PACKAGE_FILE:-}" ]]; then
  echo "release-package and release-package-file are mutually exclusive" >&2
  exit 1
fi
if [[ -z "${RELEASE_PACKAGE:-}" && -z "${RELEASE_PACKAGE_FILE:-}" ]]; then
  echo "one of release-package or release-package-file is required" >&2
  exit 1
fi

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

require_plain_filename "manifest-name" "${RELEASE_MANIFEST_NAME}"
require_plain_filename "metadata-name" "${RELEASE_METADATA_NAME}"
if [[ "${RELEASE_MANIFEST_NAME}" == "${RELEASE_METADATA_NAME}" ]]; then
  echo "manifest-name and metadata-name must differ" >&2
  exit 1
fi

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
if [[ -n "${RELEASE_PACKAGE_FILE:-}" ]]; then
  ensure_relative_pattern "release-package-file" "${RELEASE_PACKAGE_FILE}"
  package_file_path="$(realpath "${output_directory}/${RELEASE_PACKAGE_FILE}")"
  if [[ "${package_file_path}" != "${output_directory}"/* || ! -f "${package_file_path}" ]]; then
    echo "release-package-file must resolve to one file inside output-directory: ${RELEASE_PACKAGE_FILE}" >&2
    exit 1
  fi
  RELEASE_PACKAGE="$(jq -c . "${package_file_path}")"
fi

if ! jq -e '
  type == "object"
  and (keys - ["ecosystem", "name", "version", "build", "platform"] | length == 0)
  and (.ecosystem | type == "string" and length > 0)
  and (.name | type == "string" and length > 0)
  and ((.version // "") | type == "string")
  and ((.build // "") | type == "string")
  and ((.platform // "") | type == "string")
' <<<"${RELEASE_PACKAGE}" >/dev/null; then
  echo "release-package must be a package object with ecosystem and name; version may be supplied or derived per artifact" >&2
  exit 1
fi

if ! jq -e 'type == "array" and length > 0' <<<"${RELEASE_ARTIFACTS}" >/dev/null; then
  echo "release-artifacts must be a non-empty JSON array" >&2
  exit 1
fi

manifest_path="${output_directory}/${RELEASE_MANIFEST_NAME}"
metadata_path="${output_directory}/${RELEASE_METADATA_NAME}"
temporary_manifest="$(mktemp "${output_directory}/.release-build-output.XXXXXX")"
trap 'rm -f "${temporary_manifest}"' EXIT

printf '%s\n' '{"schema_version":1,"producer":"release-platform","artifacts":[]}' >"${temporary_manifest}"

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

derive_package_version() {
  local ecosystem="$1"
  local package_name="$2"
  local artifact_path="$3"
  local filename
  filename="$(basename "${artifact_path}")"

  case "${ecosystem}" in
    conda)
      local conda_prefix="${package_name}-"
      if [[ "${filename}" != "${conda_prefix}"* ]]; then
        echo "Conda artifact filename does not start with package name '${package_name}': ${filename}" >&2
        exit 1
      fi
      local conda_remainder="${filename#"${conda_prefix}"}"
      local conda_version="${conda_remainder%%-*}"
      if [[ -z "${conda_version}" || "${conda_version}" == "${conda_remainder}" ]]; then
        echo "cannot derive Conda package version from artifact filename: ${filename}" >&2
        exit 1
      fi
      printf '%s\n' "${conda_version}"
      ;;
    wheel)
      local wheel_prefix="${package_name//-/_}-"
      if [[ "${filename}" != "${wheel_prefix}"* ]]; then
        echo "wheel artifact filename does not start with normalized package name '${package_name}': ${filename}" >&2
        exit 1
      fi
      local wheel_remainder="${filename#"${wheel_prefix}"}"
      local wheel_version="${wheel_remainder%%-*}"
      if [[ -z "${wheel_version}" || "${wheel_version}" == "${wheel_remainder}" ]]; then
        echo "cannot derive wheel package version from artifact filename: ${filename}" >&2
        exit 1
      fi
      printf '%s\n' "${wheel_version}"
      ;;
    *)
      echo "release-package version is required for ${ecosystem} artifacts" >&2
      exit 1
      ;;
  esac
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
        creators: ["Tool: rapidsai/shared-workflows release-build-output"],
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
          buildType: "https://rapids.ai/release-platform/build-output/v1",
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
artifact_metadata='[]'
while IFS= read -r descriptor; do
  if ! jq -e '
    type == "object"
    and (keys - ["path", "sbom", "provenance", "signature", "package"] | length == 0)
    and (.path | type == "string" and length > 0)
    and ((.sbom // "") | type == "string")
    and ((.provenance // "") | type == "string")
    and ((.signature // "") | type == "string")
    and ((.package // {}) | type == "object")
    and ((.package // {} | keys - ["ecosystem", "name", "version", "build", "platform"]) | length == 0)
    and ((.package // {} | to_entries | map(.value | type == "string" and length > 0) | all))
  ' <<<"${descriptor}" >/dev/null; then
    echo "every release-artifacts entry must contain path and optional SBOM/provenance/signature/package overrides" >&2
    exit 1
  fi

  primary_path="$(resolve_one_file path "$(jq -r '.path' <<<"${descriptor}")")"
  sbom_pattern="$(jq -r '.sbom // empty' <<<"${descriptor}")"
  provenance_pattern="$(jq -r '.provenance // empty' <<<"${descriptor}")"
  signature_pattern="$(jq -r '.signature // empty' <<<"${descriptor}")"
  package_override="$(jq -c '.package // {}' <<<"${descriptor}")"

  package="$(jq -cn --argjson base "${RELEASE_PACKAGE}" --argjson override "${package_override}" '$base + $override')"
  package_version="$(jq -r '.version // empty' <<<"${package}")"
  if [[ -z "${package_version}" ]]; then
    package_version="$(derive_package_version "$(jq -r '.ecosystem' <<<"${package}")" "$(jq -r '.name' <<<"${package}")" "${primary_path}")"
    package="$(jq -c --arg version "${package_version}" '. + {version: $version}' <<<"${package}")"
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
  artifact="$(jq -cn \
    --arg release_catalog_key "${RELEASE_CATALOG_KEY}" \
    --arg path "${primary_path}" \
    --arg sbom "${sbom_path}" \
    --arg provenance "${provenance_path}" \
    --argjson package "${package}" \
    '{release_catalog_key: $release_catalog_key, path: $path, sbom: $sbom, provenance: $provenance, package: $package}')"
  if [[ -n "${signature_pattern}" ]]; then
    supplied_signature_path="$(resolve_one_file signature "${signature_pattern}")"
    signature_path="$(copy_supplied_evidence "${primary_path}" "${supplied_signature_path}" "signature")"
    artifact="$(jq -c --arg signature "${signature_path}" '. + {signature: $signature}' <<<"${artifact}")"
  fi

  artifact_metadata="$(jq -cn \
    --arg path "${primary_path}" \
    --arg sbom_kind "${sbom_kind}" \
    --argjson artifacts "${artifact_metadata}" \
    '$artifacts + [{path: $path, sbom_kind: $sbom_kind}]')"

  jq --argjson artifact "${artifact}" '.artifacts += [$artifact]' "${temporary_manifest}" >"${temporary_manifest}.next"
  mv "${temporary_manifest}.next" "${temporary_manifest}"
done < <(jq -c '.[]' <<<"${RELEASE_ARTIFACTS}")

if ! jq -e '.artifacts as $items | ($items | map([.release_catalog_key, .path] | join("\u0000")) | unique | length) == ($items | length)' "${temporary_manifest}" >/dev/null; then
  echo "release-artifacts contains duplicate release_catalog_key/path entries" >&2
  exit 1
fi

jq -S . "${temporary_manifest}" >"${manifest_path}"
jq -n -S \
  --arg artifact_name "${RELEASE_SOURCE_ARTIFACT_NAME}" \
  --arg manifest_name "${RELEASE_MANIFEST_NAME}" \
  --arg repository "${GITHUB_REPOSITORY:-}" \
  --arg run_attempt "${GITHUB_RUN_ATTEMPT:-}" \
  --arg run_id "${GITHUB_RUN_ID:-}" \
  --arg sha "${source_sha}" \
  --arg release_catalog_key "${RELEASE_CATALOG_KEY}" \
  --arg workflow_ref "${GITHUB_WORKFLOW_REF:-}" \
  --argjson artifact_metadata "${artifact_metadata}" \
  '{
    schema_version: 1,
    producer: "shared-workflows",
    release_catalog_key: $release_catalog_key,
    source_artifact: $artifact_name,
    build_output_manifest: $manifest_name,
    build_environment: {
      repository: $repository,
      sha: $sha,
      workflow_ref: $workflow_ref,
      run_id: $run_id,
      run_attempt: $run_attempt
    },
    metadata: {artifacts: $artifact_metadata}
  }' >"${metadata_path}"
echo "manifest-path=${manifest_path}" >>"${GITHUB_OUTPUT}"
echo "metadata-path=${metadata_path}" >>"${GITHUB_OUTPUT}"
