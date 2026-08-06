#!/usr/bin/env bash
# Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.

set -euo pipefail

if [[ "$#" -ne 1 || ! -d "$1" ]]; then
  echo "usage: $0 CONDA_OUTPUT_DIRECTORY" >&2
  exit 1
fi

output_directory="$(realpath "$1")"
descriptors='[]'
package_count=0

while IFS= read -r package_path; do
  package_path="$(realpath "${package_path}")"
  if [[ "${package_path}" != "${output_directory}"/* ]]; then
    echo "Conda package must resolve inside output directory: ${package_path}" >&2
    exit 1
  fi
  relative_path="${package_path#"${output_directory}/"}"

  case "${package_path}" in
    *.conda)
      info_members=()
      while IFS= read -r info_member; do
        info_members+=("${info_member}")
      done < <(unzip -Z1 "${package_path}" | awk '/^info-.*\.tar\.zst$/')
      if [[ "${#info_members[@]}" -ne 1 ]]; then
        echo ".conda package must contain exactly one info-*.tar.zst member: ${relative_path}" >&2
        exit 1
      fi
      index_json="$(unzip -p "${package_path}" "${info_members[0]}" | zstd -dc | tar -xOf - info/index.json)"
      ;;
    *.tar.bz2)
      index_json="$(tar -xOjf "${package_path}" info/index.json)"
      ;;
    *)
      echo "unsupported Conda package extension: ${relative_path}" >&2
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
    echo "Conda info/index.json must contain exact name, version, build, and subdir fields: ${relative_path}" >&2
    exit 1
  fi

  package="$(jq -c '{ecosystem: "conda", name, version, build, platform: .subdir}' <<<"${index_json}")"
  descriptors="$(jq -cn \
    --arg path "${relative_path}" \
    --argjson package "${package}" \
    --argjson current "${descriptors}" \
    '$current + [{path: $path, package: $package}]')"
  package_count=$((package_count + 1))
done < <(find "${output_directory}" -type f \( -name '*.conda' -o -name '*.tar.bz2' \) -print | sort)

if [[ "${package_count}" -eq 0 ]]; then
  echo "Conda output directory contains no .conda or .tar.bz2 packages: ${output_directory}" >&2
  exit 1
fi

printf '%s\n' "${descriptors}"
