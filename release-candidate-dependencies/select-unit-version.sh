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
        else ($contract.unit_package_versions[$unit_id]
              // $contract.package_versions[$unit.version_profile]
              // "")
          | if type == "string" and (length == 0 or test("^[0-9]+\\.[0-9]+(\\.[0-9]+)?$")) then .
            else error("release train unit version is invalid")
            end
        end
    end
' "${train_file}"
