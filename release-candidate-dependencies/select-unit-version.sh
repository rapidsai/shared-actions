#!/usr/bin/env bash
# Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.

set -euo pipefail

train_file="${1:?usage: select-unit-version.sh TRAIN_JSON UNIT_ID}"
unit_id="${2:?usage: select-unit-version.sh TRAIN_JSON UNIT_ID}"

jq -r --arg unit_id "${unit_id}" '
  . as $train
  | [$train.release_units[] | select(.id == $unit_id)] as $units
  | if ($units | length) != 1 then error("release train must contain exactly one candidate unit")
    else $units[0] as $unit
      | ($train.version_contract // {}) as $contract
      | if ($contract | type) != "object"
          or (($contract.unit_package_versions // {}) | type) != "object"
          or (($contract.package_versions // {}) | type) != "object"
        then error("release train version contract is invalid")
        else ($unit.version_profile // "") as $profile
          | ($contract.unit_package_versions[$unit_id]
             // $contract.package_versions[$profile]
             // null) as $version
          | if $version == null then
              if (["conda", "wheel", "maven"] | index($profile)) != null
              then error("release train is missing the frozen package version for \($unit_id)")
              else ""
              end
            elif ($version | type) == "string"
                 and ($version | test("^[0-9]+\\.[0-9]+(\\.[0-9]+)?$"))
              then $version
            else error("release train unit version is invalid")
            end
        end
    end
' "${train_file}"
