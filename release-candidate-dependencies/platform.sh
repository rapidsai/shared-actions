#!/usr/bin/env bash
# Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.

# Normalize the architecture names used by RAPIDS CI and Conda.
conda_platform_for_arch() {
  case "$1" in
    amd64|x86_64) printf 'linux-64\n' ;;
    arm64|aarch64) printf 'linux-aarch64\n' ;;
    *)
      echo "unsupported candidate architecture: $1" >&2
      return 1
      ;;
  esac
}

wheel_platform_for_arch() {
  case "$1" in
    amd64|x86_64) printf 'x86_64-manylinux_2_28\n' ;;
    arm64|aarch64) printf 'aarch64-manylinux_2_28\n' ;;
    *)
      echo "unsupported candidate architecture: $1" >&2
      return 1
      ;;
  esac
}
