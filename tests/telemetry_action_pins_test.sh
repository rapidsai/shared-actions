#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
pinned_revision=39fc31647bd5514c99fca7b0463d45c2446148b7
for action in telemetry-dispatch-setup telemetry-dispatch-stash-job-artifacts; do
  action_file="${root}/${action}/action.yml"
  if ! rg -Fq "rapidsai/shared-actions/telemetry-impls/load-then-clone@${pinned_revision}" "${action_file}" ||
    rg -Fq 'telemetry-impls/load-then-clone@main' "${action_file}"; then
    echo "${action_file} must pin telemetry loader to the reviewed shared-actions revision" >&2
    exit 1
  fi
done
