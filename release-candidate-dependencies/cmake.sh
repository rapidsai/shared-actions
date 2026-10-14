#!/usr/bin/env bash
# Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.

set -euo pipefail

original_cmake="${RAPIDS_CANDIDATE_ORIGINAL_CMAKE:?candidate CMake path is required}"
rapids_cmake_sha="${RAPIDS_CANDIDATE_RAPIDS_CMAKE_SHA:?candidate RAPIDS CMake SHA is required}"

# Callers may install a newer CMake after candidate setup. Honor the current
# PATH instead of forcing the executable captured before those installations,
# while skipping this wrapper (including symlinks) to avoid recursion.
IFS=: read -r -a cmake_search_path <<< "${PATH}"
for directory in "${cmake_search_path[@]}"; do
  candidate="${directory:-.}/cmake"
  if [[ -x "${candidate}" && ! -d "${candidate}" \
    && ! "${candidate}" -ef "${BASH_SOURCE[0]}" ]]; then
    original_cmake="${candidate}"
    break
  fi
done

# CMake's build/install/script modes do not accept configure definitions. Only
# add the immutable RAPIDS CMake override when the invocation configures a tree.
case "${1:-}" in
  --build|--install|--open|-E|-P|--help|--version)
    exec "${original_cmake}" "$@"
    ;;
esac

exec "${original_cmake}" "-Drapids-cmake-sha=${rapids_cmake_sha}" "$@"
