#!/usr/bin/env bash
# Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.

set -euo pipefail

train=""
output=""
target_units=()
while [[ "$#" -gt 0 ]]; do
  case "$1" in
    --train)
      train="${2:?--train requires a path}"
      shift 2
      ;;
    --output)
      output="${2:?--output requires a path}"
      shift 2
      ;;
    --target-unit)
      target_units+=("${2:?--target-unit requires an ID}")
      shift 2
      ;;
    *)
      echo "unknown argument: $1" >&2
      exit 2
      ;;
  esac
done
if [[ -z "${train}" || -z "${output}" || ${#target_units[@]} -eq 0 ]]; then
  echo "usage: $0 --train FILE --output FILE --target-unit ID [--target-unit ID ...]" >&2
  exit 2
fi
if [[ ! -f "${train}" ]]; then
  echo "release train is not readable: ${train}" >&2
  exit 1
fi

targets_json="$(printf '%s\n' "${target_units[@]}" | jq -Rsc 'split("\n") | map(select(length > 0)) | unique')"
tool_repositories='{"dependency-file-generator":"rapidsai/dependency-file-generator","rapids-build-backend":"rapidsai/rapids-build-backend","rapids-cmake":"rapidsai/rapids-cmake"}'
jq -n -e -S \
  --slurpfile train "${train}" \
  --argjson targets "${targets_json}" \
  --argjson tool_repositories "${tool_repositories}" \
  '
    $train[0] as $document
    | ($document.release_units | map({key: .id, value: .}) | from_entries) as $units
    | def closure($id; $seen):
        if ($seen | index($id)) != null then []
        elif $units[$id] == null then error("dependency references an unknown release unit: " + $id)
        else
          ($units[$id].dependencies // []) as $dependencies
          | $dependencies + ([$dependencies[] | closure(.; $seen + [$id])[]])
        end;
    if ($targets | type) != "array" or ($targets | length) == 0 then
      error("at least one release unit is required")
    elif ($targets | all(.[]; $units[.] != null and $units[.].artifact_family != "source")) | not then
      error("target unit is absent or is not a build unit")
    elif ($document.source_revisions | type) != "object"
      or ($document.github_repositories | type) != "object" then
      error("release train source revision mappings are missing")
    elif ($document.build_tool_revisions | type) != "object"
      or (($document.build_tool_revisions | keys) - ($tool_repositories | keys) | length) != 0
      or (($tool_repositories | keys) - ($document.build_tool_revisions | keys) | length) != 0 then
      error("release train build tool revisions do not match supported tools")
    else
      ([ $targets[] | closure(.; []) ] | flatten | unique
        | map(select($units[.].artifact_family == "source"))) as $source_ids
      | ([$source_ids[] as $id
          | ($id | sub("^source:"; "")) as $logical
          | ($document.source_revisions[$logical] // error("source revision is missing: " + $logical)) as $revision
          | ($document.github_repositories[$logical] // error("source repository is missing: " + $logical)) as $repository
          | if ($revision | type) != "string" or ($revision | test("^[0-9a-f]{40}$")) | not
              or ($repository | type) != "string" or ($repository | test("^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")) | not
              or $units[$id].repository != $logical or $units[$id].source_revision != $revision
            then error("source unit does not match its frozen source mappings: " + $logical)
            else {key: $logical, value: {repository: $repository, revision: $revision}}
            end] | from_entries) as $sources
      | ([$document.build_tool_revisions | to_entries[]
          | .key as $name
          | .value as $revision
          | if ($revision | type) != "string" or ($revision | test("^[0-9a-f]{40}$")) | not
            then error("build tool revision is invalid: " + $name)
            else {key: $name, value: {repository: $tool_repositories[$name], revision: $revision}}
            end] | from_entries) as $build_tools
      | {schema_version: 1, sources: $sources, build_tools: $build_tools}
    end
  ' "${train}" >"${output}"
