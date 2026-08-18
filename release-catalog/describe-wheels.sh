#!/usr/bin/env bash
# Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.

set -euo pipefail

if [[ "$#" -ne 1 || ! -d "$1" ]]; then
  echo "usage: $0 WHEEL_OUTPUT_DIRECTORY" >&2
  exit 1
fi

output_directory="$(realpath "$1")"
descriptors='[]'
wheel_count=0

while IFS= read -r wheel_path; do
  wheel_path="$(realpath "${wheel_path}")"
  if [[ "${wheel_path}" != "${output_directory}"/* ]]; then
    echo "wheel must resolve inside output directory: ${wheel_path}" >&2
    exit 1
  fi
  relative_path="${wheel_path#"${output_directory}/"}"

  metadata_members=()
  while IFS= read -r metadata_member; do
    metadata_members+=("${metadata_member}")
  done < <(unzip -Z1 "${wheel_path}" | awk '/\.dist-info\/METADATA$/')
  if [[ "${#metadata_members[@]}" -ne 1 ]]; then
    echo "wheel must contain exactly one .dist-info/METADATA file: ${relative_path}" >&2
    exit 1
  fi

  metadata="$(unzip -p "${wheel_path}" "${metadata_members[0]}")"
  package_name="$(awk 'tolower($0) ~ /^name:[[:space:]]*/ {sub(/^[^:]*:[[:space:]]*/, ""); sub(/\r$/, ""); print; exit}' <<<"${metadata}")"
  package_version="$(awk 'tolower($0) ~ /^version:[[:space:]]*/ {sub(/^[^:]*:[[:space:]]*/, ""); sub(/\r$/, ""); print; exit}' <<<"${metadata}")"
  if [[ -z "${package_name}" || -z "${package_version}" ]]; then
    echo "wheel metadata must contain non-empty Name and Version fields: ${relative_path}" >&2
    exit 1
  fi

  descriptors="$(jq -cn \
    --arg path "${relative_path}" \
    --arg name "${package_name}" \
    --arg version "${package_version}" \
    --argjson current "${descriptors}" \
    '$current + [{path: $path, package: {ecosystem: "wheel", name: $name, version: $version}}]')"
  wheel_count=$((wheel_count + 1))
done < <(find "${output_directory}" -type f -name '*.whl' -print | sort)

if [[ "${wheel_count}" -eq 0 ]]; then
  echo "wheel output directory contains no .whl files: ${output_directory}" >&2
  exit 1
fi

printf '%s\n' "${descriptors}"
