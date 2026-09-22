#!/usr/bin/env bash
# Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.

set -euo pipefail

export RELEASE_CATALOG_SCRIPT_NAME="materialize.sh"
export RELEASE_CATALOG_ERROR_TITLE="Release catalog materialization failed"
# shellcheck source=release-catalog/error.sh
source "$(dirname "${BASH_SOURCE[0]}")/error.sh"

# Record artifact metadata from completed build directory into a release catalog companion.
#
# Inputs come from release-catalog/action.yml. RELEASE_ARTIFACTS is
# either empty (discover Conda packages, wheels, and Maven JARs) or a validated JSON array
# selecting custom artifacts. GitHub Actions supplies the GITHUB_* build
# context, while RELEASE_SOURCE_SHA identifies the commit actually checked out.
#
# The script validates all context before touching outputs, resolves every
# artifact to exactly one file inside RELEASE_ARTIFACT_DIRECTORY, reads or
# extracts its package identity, generates an identity-only CycloneDX SBOM plus
# provenance evidence, and writes release-catalog-entries.json. It never
# modifies primary files.

require_nonempty() {
  local name="$1"
  local value="$2"
  if [[ -z "${value}" ]]; then
    release_catalog_error "${name} must be a non-empty string"
    exit 1
  fi
}

require_single_line() {
  local name="$1"
  local value="$2"
  require_nonempty "${name}" "${value}"
  if [[ "${value}" == *$'\n'* || "${value}" == *$'\r'* ]]; then
    release_catalog_error "${name} must be a single-line string"
    exit 1
  fi
}

require_positive_integer() {
  local name="$1"
  local value="$2"
  if [[ ! "${value}" =~ ^[1-9][0-9]*$ ]]; then
    release_catalog_error "${name} must be a positive integer"
    exit 1
  fi
}

# Fail before materialization when provenance would otherwise contain empty or
# malformed source fields. These values are guaranteed in GitHub Actions; a
# local caller must set explicit test values.
require_single_line "RELEASE_CATALOG_KEY" "${RELEASE_CATALOG_KEY:-}"
require_single_line "RELEASE_ARTIFACT_DIRECTORY" "${RELEASE_ARTIFACT_DIRECTORY:-}"
require_single_line "RELEASE_SOURCE_ARTIFACT_NAME" "${RELEASE_SOURCE_ARTIFACT_NAME:-}"

source_sha="${RELEASE_SOURCE_SHA:-}"
require_single_line "RELEASE_SOURCE_SHA" "${source_sha}"
if [[ ! "${source_sha}" =~ ^[[:xdigit:]]{40}$ && ! "${source_sha}" =~ ^[[:xdigit:]]{64}$ ]]; then
  release_catalog_error "RELEASE_SOURCE_SHA must be a 40- or 64-character hexadecimal Git object ID"
  exit 1
fi

github_repository="${GITHUB_REPOSITORY:-}"
github_run_attempt="${GITHUB_RUN_ATTEMPT:-}"
github_run_id="${GITHUB_RUN_ID:-}"
github_workflow_ref="${GITHUB_WORKFLOW_REF:-}"
require_single_line "GITHUB_REPOSITORY" "${github_repository}"
require_single_line "GITHUB_RUN_ATTEMPT" "${github_run_attempt}"
require_single_line "GITHUB_RUN_ID" "${github_run_id}"
require_single_line "GITHUB_WORKFLOW_REF" "${github_workflow_ref}"
if [[ ! "${github_repository}" =~ ^[^/[:space:]]+/[^/[:space:]]+$ ]]; then
  release_catalog_error "GITHUB_REPOSITORY must have owner/repository form"
  exit 1
fi
require_positive_integer "GITHUB_RUN_ATTEMPT" "${github_run_attempt}"
require_positive_integer "GITHUB_RUN_ID" "${github_run_id}"

if [[ ! -d "${RELEASE_ARTIFACT_DIRECTORY}" ]]; then
  release_catalog_error "artifact-directory does not exist or is not a directory: ${RELEASE_ARTIFACT_DIRECTORY}"
  exit 1
fi

ensure_relative_pattern() {
  local field="$1"
  local pattern="$2"
  if [[ "${pattern}" == /* || "${pattern}" == */../* || "${pattern}" == ../* || "${pattern}" == *"/.." ]]; then
    release_catalog_error "${field} must be a relative path inside artifact-directory: ${pattern}"
    exit 1
  fi
}

artifact_directory="$(realpath "${RELEASE_ARTIFACT_DIRECTORY}")"

# Artifact paths may contain globs for matrix-dependent filenames, but each
# descriptor must resolve to one regular file below the configured directory.
resolve_one_file() {
  local field="$1"
  local pattern="$2"
  local -a matches=()

  ensure_relative_pattern "${field}" "${pattern}"
  while IFS= read -r match; do
    matches+=("${match}")
  done < <(compgen -G "${artifact_directory}/${pattern}" || true)
  if [[ "${#matches[@]}" -ne 1 || ! -f "${matches[0]:-}" ]]; then
    release_catalog_error "${field} pattern must resolve to exactly one file: ${pattern}"
    exit 1
  fi

  local resolved
  resolved="$(realpath "${matches[0]}")"
  if [[ "${resolved}" != "${artifact_directory}"/* ]]; then
    release_catalog_error "${field} must resolve inside artifact-directory: ${pattern}"
    exit 1
  fi
  printf '%s\n' "${resolved#"${artifact_directory}/"}"
}

# Normalize supported package formats into the common package identity stored
# in each catalog entry. Other formats supply the same fields via
# package_identity_file instead.

# Wheels expose Core Metadata in <name>.dist-info/METADATA.
describe_wheel_package() {
  local wheel_path="$1"
  local relative_path="${wheel_path#"${artifact_directory}/"}"
  local -a metadata_members=()
  local metadata_member
  while IFS= read -r metadata_member; do
    metadata_members+=("${metadata_member}")
  done < <(unzip -Z1 "${wheel_path}" | awk '/\.dist-info\/METADATA$/')
  if [[ "${#metadata_members[@]}" -ne 1 ]]; then
    release_catalog_error "wheel must contain exactly one .dist-info/METADATA file: ${relative_path}"
    exit 1
  fi

  local metadata package_name package_version
  metadata="$(unzip -p "${wheel_path}" "${metadata_members[0]}")"
  package_name="$(awk 'tolower($0) ~ /^name:[[:space:]]*/ {sub(/^[^:]*:[[:space:]]*/, ""); sub(/\r$/, ""); print; exit}' <<<"${metadata}")"
  package_version="$(awk 'tolower($0) ~ /^version:[[:space:]]*/ {sub(/^[^:]*:[[:space:]]*/, ""); sub(/\r$/, ""); print; exit}' <<<"${metadata}")"
  if [[ -z "${package_name}" || -z "${package_version}" ]]; then
    release_catalog_error "wheel metadata must contain non-empty Name and Version fields: ${relative_path}"
    exit 1
  fi

  jq -cn --arg name "${package_name}" --arg version "${package_version}" \
    '{ecosystem: "wheel", name: $name, version: $version}'
}

# Maven-built JARs normally expose their coordinates in exactly one
# META-INF/maven/<groupId>/<artifactId>/pom.properties file. Shaded JARs may
# contain several descriptors, so ambiguity is an error rather than a guess.
describe_maven_jar_package() {
  local jar_path="$1"
  local relative_path="${jar_path#"${artifact_directory}/"}"
  local -a metadata_members=()
  local metadata_member
  while IFS= read -r metadata_member; do
    metadata_members+=("${metadata_member}")
  done < <(unzip -Z1 "${jar_path}" | awk '$0 ~ "^META-INF/maven/[^/]+/[^/]+/pom[.]properties$"')
  if [[ "${#metadata_members[@]}" -ne 1 ]]; then
    release_catalog_error "Maven JAR must contain exactly one META-INF/maven/<groupId>/<artifactId>/pom.properties file: ${relative_path}"
    return 1
  fi

  local metadata coordinates_path coordinate_group_id coordinate_artifact_id
  local group_id artifact_id package_version
  metadata="$(unzip -p "${jar_path}" "${metadata_members[0]}")"
  coordinates_path="${metadata_members[0]#META-INF/maven/}"
  coordinates_path="${coordinates_path%/pom.properties}"
  coordinate_group_id="${coordinates_path%%/*}"
  coordinate_artifact_id="${coordinates_path#*/}"
  group_id="$(awk -F= '$1 == "groupId" {sub(/^[^=]*=/, ""); sub(/\r$/, ""); print; exit}' <<<"${metadata}")"
  artifact_id="$(awk -F= '$1 == "artifactId" {sub(/^[^=]*=/, ""); sub(/\r$/, ""); print; exit}' <<<"${metadata}")"
  package_version="$(awk -F= '$1 == "version" {sub(/^[^=]*=/, ""); sub(/\r$/, ""); print; exit}' <<<"${metadata}")"
  if [[ -z "${group_id}" || -z "${artifact_id}" || -z "${package_version}" ]]; then
    release_catalog_error "Maven pom.properties must contain non-empty groupId, artifactId, and version fields: ${relative_path}"
    return 1
  fi
  if [[ "${group_id}" != "${coordinate_group_id}" || "${artifact_id}" != "${coordinate_artifact_id}" ]]; then
    release_catalog_error "Maven pom.properties coordinates do not match its META-INF path: ${relative_path}"
    return 1
  fi

  jq -cn --arg name "${group_id}:${artifact_id}" --arg version "${package_version}" \
    '{ecosystem: "maven", name: $name, version: $version}'
}

# Conda packages expose info/index.json.
describe_conda_package() {
  local package_path="$1"
  local relative_path="${package_path#"${artifact_directory}/"}"
  local index_json
  case "${package_path}" in
    *.conda)
      local -a info_members=()
      local info_member
      while IFS= read -r info_member; do
        info_members+=("${info_member}")
      done < <(unzip -Z1 "${package_path}" | awk '/^info-.*\.tar\.zst$/')
      if [[ "${#info_members[@]}" -ne 1 ]]; then
        release_catalog_error ".conda package must contain exactly one info-*.tar.zst member: ${relative_path}"
        exit 1
      fi
      index_json="$(unzip -p "${package_path}" "${info_members[0]}" | zstd -dc | tar -xOf - info/index.json)"
      ;;
    *.tar.bz2)
      index_json="$(tar -xOjf "${package_path}" info/index.json)"
      ;;
    *)
      release_catalog_error "unsupported Conda package extension: ${relative_path}"
      exit 1
      ;;
  esac

  if ! jq -e '
    type == "object"
    and (.name | type == "string" and length > 0)
    and (.version | type == "string" and length > 0)
    and (.build | type == "string" and length > 0)
    and (.subdir | type == "string" and length > 0)
  ' <<<"${index_json}" >/dev/null; then
    release_catalog_error "Conda info/index.json must contain exact name, version, build, and subdir fields: ${relative_path}"
    exit 1
  fi

  jq -c '{ecosystem: "conda", name, version, build, platform: .subdir}' <<<"${index_json}"
}

prepare_artifacts() {
  local configured_artifacts="${RELEASE_ARTIFACTS:-}"
  local prepared_artifacts='[]'
  local conda_package descriptor package primary_path primary_file identity_file

  if [[ -z "${configured_artifacts}" ]]; then
    local -a detected_files=()
    while IFS= read -r primary_file; do
      detected_files+=("${primary_file}")
    done < <(find "${artifact_directory}" -type f \( -name '*.conda' -o -name '*.tar.bz2' -o -name '*.whl' -o -name '*.jar' \) -print | sort)

    for primary_file in "${detected_files[@]}"; do
      primary_path="${primary_file#"${artifact_directory}/"}"
      case "${primary_file}" in
        *.whl)
          if ! package="$(describe_wheel_package "${primary_file}")"; then
            return 1
          fi
          ;;
        *.jar)
          if ! package="$(describe_maven_jar_package "${primary_file}")"; then
            return 1
          fi
          ;;
        *)
          if ! package="$(describe_conda_package "${primary_file}")"; then
            return 1
          fi
          ;;
      esac
      descriptor="$(jq -cn --arg path "${primary_path}" --argjson package "${package}" '{path: $path, package: $package}')"
      prepared_artifacts="$(jq -cn --argjson current "${prepared_artifacts}" --argjson descriptor "${descriptor}" '$current + [$descriptor]')"
    done
  else
    if ! jq -e 'type == "array" and length > 0' <<<"${configured_artifacts}" >/dev/null; then
      release_catalog_error "release-artifacts must be a non-empty JSON array when supplied"
      exit 1
    fi
    while IFS= read -r descriptor; do
      if jq -e 'has("package")' <<<"${descriptor}" >/dev/null; then
        release_catalog_error "package identity must not be supplied inline: ${descriptor}"
        return 1
      fi
      primary_path="$(resolve_one_file path "$(jq -r '.path' <<<"${descriptor}")")"
      primary_file="${artifact_directory}/${primary_path}"
      identity_file="$(jq -r '.package_identity_file // empty' <<<"${descriptor}")"
      package=''

      case "${primary_file}" in
        *.whl)
          if [[ -n "${identity_file}" ]]; then
            release_catalog_error "package_identity_file is not allowed when wheel identity can be extracted: $(jq -r '.path' <<<"${descriptor}")"
            exit 1
          fi
          if ! package="$(describe_wheel_package "${primary_file}")"; then
            return 1
          fi
          ;;
        *.conda)
          if [[ -n "${identity_file}" ]]; then
            release_catalog_error "package_identity_file is not allowed when Conda identity can be extracted: $(jq -r '.path' <<<"${descriptor}")"
            exit 1
          fi
          if ! package="$(describe_conda_package "${primary_file}")"; then
            return 1
          fi
          ;;
        *.jar)
          if package="$(describe_maven_jar_package "${primary_file}" 2>/dev/null)"; then
            if [[ -n "${identity_file}" ]]; then
              release_catalog_error "package_identity_file is not allowed when Maven JAR identity can be extracted: $(jq -r '.path' <<<"${descriptor}")"
              exit 1
            fi
          elif [[ -z "${identity_file}" ]]; then
            release_catalog_error "JAR does not contain one unambiguous Maven package identity and requires package_identity_file: $(jq -r '.path' <<<"${descriptor}")"
            exit 1
          fi
          ;;
        *.tar.bz2)
          if conda_package="$(describe_conda_package "${primary_file}" 2>/dev/null)"; then
            if [[ -n "${identity_file}" ]]; then
              release_catalog_error "package_identity_file is not allowed when Conda identity can be extracted: $(jq -r '.path' <<<"${descriptor}")"
              exit 1
            fi
            package="${conda_package}"
          elif [[ -z "${identity_file}" ]]; then
            release_catalog_error "artifact is not a valid Conda package and requires package_identity_file: $(jq -r '.path' <<<"${descriptor}")"
            exit 1
          fi
          ;;
        *)
          if [[ -z "${identity_file}" ]]; then
            release_catalog_error "artifact identity cannot be extracted; package_identity_file is required: $(jq -r '.path' <<<"${descriptor}")"
            exit 1
          fi
          ;;
      esac

      if [[ -n "${package}" ]]; then
        descriptor="$(jq -c --argjson package "${package}" '. + {package: $package}' <<<"${descriptor}")"
      fi
      prepared_artifacts="$(jq -cn --argjson current "${prepared_artifacts}" --argjson descriptor "${descriptor}" '$current + [$descriptor]')"
    done < <(jq -c '.[]' <<<"${configured_artifacts}")
  fi

  if [[ "$(jq 'length' <<<"${prepared_artifacts}")" -eq 0 ]]; then
    release_catalog_error "artifact-directory contains no detectable Conda, wheel, or Maven JAR artifacts; explicitly selected artifacts require package_identity_file when their identity cannot be parsed"
    exit 1
  fi
  printf '%s\n' "${prepared_artifacts}"
}

# From here onward, every descriptor has an exact path and either extracted
# package identity or a caller-created package identity file.
artifacts="$(prepare_artifacts)"
# Fixed name: upload-s3.sh, the schema, and consumers all depend on it.
entries_path="${artifact_directory}/release-catalog-entries.json"
temporary_manifest="$(mktemp "${artifact_directory}/.release-catalog.XXXXXX")"
trap 'rm -f "${temporary_manifest}"' EXIT

printf '%s\n' '{"entries":[]}' >"${temporary_manifest}"

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
  artifact_digest="$(sha256sum "${artifact_directory}/${primary_path}" | awk '{print $1}')"
  local artifact_label="${primary_path//\//_}"
  printf 'release-evidence/%s.%s.%s.json\n' "${artifact_label}" "${artifact_digest}" "${kind}"
}

# This initial CycloneDX implementation identifies the artifact only. Component
# inventory and the dependency graph are intentionally deferred until a future
# implementation can accept evidence captured by the producer during the build.
write_generated_cyclonedx_sbom() {
  local primary_path="$1"
  local package="$2"
  local destination="$3"
  local artifact_digest
  artifact_digest="$(sha256sum "${artifact_directory}/${primary_path}" | awk '{print $1}')"
  mkdir -p "$(dirname "${artifact_directory}/${destination}")"
  jq -n -S \
    --arg artifact_digest "${artifact_digest}" \
    --arg artifact_path "${primary_path}" \
    --argjson package "${package}" \
    '{
      bomFormat: "CycloneDX",
      specVersion: "1.6",
      version: 1,
      metadata: {
        timestamp: (now | strftime("%Y-%m-%dT%H:%M:%SZ")),
        tools: {
          components: [{
            type: "application",
            group: "rapidsai",
            name: "shared-actions/release-catalog"
          }]
        },
        component: {
          type: "file",
          "bom-ref": ("urn:sha256:" + $artifact_digest),
          name: $package.name,
          version: $package.version,
          hashes: [{alg: "SHA-256", content: $artifact_digest}],
          properties: [
            {name: "rapids:artifact:path", value: $artifact_path},
            {name: "rapids:package:ecosystem", value: $package.ecosystem}
          ]
        }
      }
    }' >"${artifact_directory}/${destination}"
}

write_generated_provenance() {
  local primary_path="$1"
  local package="$2"
  local destination="$3"
  local artifact_digest
  artifact_digest="$(sha256sum "${artifact_directory}/${primary_path}" | awk '{print $1}')"
  mkdir -p "$(dirname "${artifact_directory}/${destination}")"
  jq -n -S \
    --arg artifact_digest "${artifact_digest}" \
    --arg artifact_path "${primary_path}" \
    --arg repository "${github_repository}" \
    --arg run_attempt "${github_run_attempt}" \
    --arg run_id "${github_run_id}" \
    --arg source_sha "${source_sha}" \
    --arg workflow_ref "${github_workflow_ref}" \
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
    }' >"${artifact_directory}/${destination}"
}

shopt -s globstar nullglob
# Create one catalog entry, one identity-only SBOM document, and one provenance
# document per primary artifact. Caller-supplied dependency evidence is
# intentionally not accepted by this version of the schema.
while IFS= read -r descriptor; do
  if ! jq -e '
    type == "object"
    and (keys - ["path", "package_identity_file", "package"] | length == 0)
    and (.path | type == "string" and length > 0)
    and ((.package_identity_file // "") | type == "string")
    and ([has("package"), has("package_identity_file")] | map(select(.)) | length == 1)
  ' <<<"${descriptor}" >/dev/null; then
    release_catalog_error "release artifact descriptor must contain path and exactly one package identity source: ${descriptor}"
    exit 1
  fi

  primary_path="$(resolve_one_file path "$(jq -r '.path' <<<"${descriptor}")")"
  package_identity_file="$(jq -r '.package_identity_file // empty' <<<"${descriptor}")"
  if [[ -n "${package_identity_file}" ]]; then
    package_identity_path="$(resolve_one_file package_identity_file "${package_identity_file}")"
    if ! package="$(jq -ce . "${artifact_directory}/${package_identity_path}" 2>/dev/null)"; then
      release_catalog_error "package_identity_file must contain valid JSON: ${package_identity_file}"
      exit 1
    fi
  else
    package="$(jq -c '.package' <<<"${descriptor}")"
  fi
  if ! validate_package_identity <<<"${package}"; then
    release_catalog_error "package identity for ${primary_path} must contain non-empty ecosystem, name, and version strings and only optional build or platform strings"
    exit 1
  fi

  artifact_sha256="$(sha256sum "${artifact_directory}/${primary_path}" | awk '{print $1}')"
  sbom_path="$(generated_evidence_path "${primary_path}" "sbom.cdx")"
  write_generated_cyclonedx_sbom "${primary_path}" "${package}" "${sbom_path}"
  provenance_path="$(generated_evidence_path "${primary_path}" "provenance")"
  write_generated_provenance "${primary_path}" "${package}" "${provenance_path}"

  entry="$(jq -cn \
    --arg release_catalog_key "${RELEASE_CATALOG_KEY}" \
    --arg path "${primary_path}" \
    --arg sha256 "${artifact_sha256}" \
    --arg sbom "${sbom_path}" \
    --arg provenance "${provenance_path}" \
    --argjson package "${package}" \
    '{release_catalog_key: $release_catalog_key, path: $path, sha256: $sha256, sbom: $sbom, sbom_kind: "generated-identity", provenance: $provenance, package: $package}')"

  jq --argjson entry "${entry}" '.entries += [$entry]' "${temporary_manifest}" >"${temporary_manifest}.next"
  mv "${temporary_manifest}.next" "${temporary_manifest}"
done < <(jq -c '.[]' <<<"${artifacts}")

if ! jq -e '.entries as $items | ($items | map([.release_catalog_key, .path] | join("\u0000")) | unique | length) == ($items | length)' "${temporary_manifest}" >/dev/null; then
  release_catalog_error "release-artifacts contains duplicate release_catalog_key/path entries"
  exit 1
fi

# Store the job's source context (repository, commit, workflow, run) once at
# the top level rather than on every entry. The release platform keeps this
# association when it merges companions from many jobs into the catalog.
jq -n -S \
  --arg artifact_name "${RELEASE_SOURCE_ARTIFACT_NAME}" \
  --arg repository "${github_repository}" \
  --arg run_attempt "${github_run_attempt}" \
  --arg run_id "${github_run_id}" \
  --arg sha "${source_sha}" \
  --arg workflow_ref "${github_workflow_ref}" \
  --argjson entries "$(jq -c '.entries' "${temporary_manifest}")" \
  '{
    schema_version: 1,
    producer: "rapidsai/shared-actions/release-catalog",
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
