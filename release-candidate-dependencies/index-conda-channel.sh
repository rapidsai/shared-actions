#!/usr/bin/env bash
# Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.

set -euo pipefail

if [[ $# -ne 1 || -z "$1" ]]; then
  echo "usage: index-conda-channel.sh CHANNEL_DIRECTORY" >&2
  exit 2
fi

if command -v conda >/dev/null; then
  conda index "$1"
elif python3 -c 'import conda_index' >/dev/null 2>&1; then
  python3 -m conda_index "$1"
else
  echo "conda or the conda-index Python module is required to index the private candidate channel" >&2
  exit 1
fi
