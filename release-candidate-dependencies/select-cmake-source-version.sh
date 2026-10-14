#!/usr/bin/env bash
# Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.

set -euo pipefail

train_file="${1:?usage: select-cmake-source-version.sh TRAIN_JSON UNIT_ID}"
unit_id="${2:?usage: select-cmake-source-version.sh TRAIN_JSON UNIT_ID}"
if [[ ! "${unit_id}" =~ ^wheel:([A-Za-z0-9_.-]+)$ ]]; then
  echo "candidate unit must be a wheel unit ID" >&2
  exit 1
fi
unit_name="${BASH_REMATCH[1]}"

jq -er --arg unit_id "conda:${unit_name}" '
  (.version_contract // {}) as $contract
  | ($contract.unit_package_versions // {}) as $unit_versions
  | ($contract.package_versions // {}) as $family_versions
  | if ($unit_versions | type) != "object" or ($family_versions | type) != "object" then
      error("release train version contract is invalid")
    else
      ($unit_versions[$unit_id] // $family_versions.conda) as $version
      | if ($version | type) == "string" and ($version | test("^[0-9]+\\.[0-9]+(\\.[0-9]+)?$")) then
          ($version | if test("^[0-9]+\\.[0-9]+$") then . + ".0" else . end)
        else error("release train has no final numeric Conda source version for this wheel unit")
        end
    end
' "${train_file}"
