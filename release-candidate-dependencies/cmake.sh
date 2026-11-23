#!/usr/bin/env bash
# Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.

set -euo pipefail

original_cmake="${RAPIDS_CANDIDATE_ORIGINAL_CMAKE:?candidate CMake path is required}"
rapids_cmake_sha="${RAPIDS_CANDIDATE_RAPIDS_CMAKE_SHA:?candidate RAPIDS CMake SHA is required}"

# CMake's build/install/script modes do not accept configure definitions. Only
# add the immutable RAPIDS CMake override when the invocation configures a tree.
case "${1:-}" in
  --build|--install|--open|-E|-P|--help|--version)
    exec "${original_cmake}" "$@"
    ;;
esac

exec "${original_cmake}" "-Drapids-cmake-sha=${rapids_cmake_sha}" "$@"
