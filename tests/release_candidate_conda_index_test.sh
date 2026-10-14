#!/usr/bin/env bash
# Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.

set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
temp_dir="$(mktemp -d)"
trap 'rm -rf "${temp_dir}"' EXIT
mkdir -p "${temp_dir}/bin" "${temp_dir}/channel"

cat >"${temp_dir}/bin/conda" <<'EOF'
#!/usr/bin/env bash
[[ "$#" -eq 2 && "$1" == index && "$2" == /candidate/channel ]]
printf 'conda %s\n' "$2" >>"${CALL_LOG}"
EOF
chmod +x "${temp_dir}/bin/conda"
CALL_LOG="${temp_dir}/calls" PATH="${temp_dir}/bin:/usr/bin:/bin" \
  bash "${root}/release-candidate-dependencies/index-conda-channel.sh" /candidate/channel
[[ "$(cat "${temp_dir}/calls")" == 'conda /candidate/channel' ]]

rm "${temp_dir}/bin/conda"
cat >"${temp_dir}/bin/python3" <<'EOF'
#!/usr/bin/env bash
if [[ "$#" -eq 1 && "$1" == '-c' ]]; then
  exit 0
fi
[[ "$#" -eq 3 && "$1" == -m && "$2" == conda_index && "$3" == /candidate/channel ]]
printf 'python %s\n' "$3" >>"${CALL_LOG}"
EOF
chmod +x "${temp_dir}/bin/python3"
CALL_LOG="${temp_dir}/calls" PATH="${temp_dir}/bin:/usr/bin:/bin" \
  bash "${root}/release-candidate-dependencies/index-conda-channel.sh" /candidate/channel
[[ "$(tail -n 1 "${temp_dir}/calls")" == 'python /candidate/channel' ]]

echo "Candidate Conda channel indexer passed."
