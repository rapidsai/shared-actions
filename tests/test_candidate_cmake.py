# Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.

import subprocess
from pathlib import Path

import pytest

_WRAPPER = Path(__file__).parents[1] / "release-candidate-dependencies" / "cmake.sh"


@pytest.mark.parametrize("arguments", [["-S", ".", "-B", "build"], ["--build", "build"], ["--version"]])
def test_cmake_installed_after_setup_is_used_without_losing_source_pin(tmp_path, arguments):
    tools = tmp_path / "candidate-tools"
    tools.mkdir()
    wrapper = tools / "cmake"
    wrapper.write_text(_WRAPPER.read_text())
    wrapper.chmod(0o755)
    alias = tmp_path / "wrapper-alias"
    alias.mkdir()
    (alias / "cmake").symlink_to(wrapper)

    system = tmp_path / "old-system-cmake"
    system.write_text('#!/bin/bash\nprintf "old CMake was selected\\n" >&2\nexit 99\n')
    system.chmod(0o755)
    installed = tmp_path / "python-bin"
    installed.mkdir()
    (installed / "cmake").write_text("#!/bin/bash\nprintf '%s\\n' \"$@\"\n")
    (installed / "cmake").chmod(0o755)

    result = subprocess.run(  # noqa: S603
        ["/bin/bash", str(wrapper), *arguments],
        env={
            "PATH": f"{tools}:{alias}:{installed}",
            "RAPIDS_CANDIDATE_ORIGINAL_CMAKE": str(system),
            "RAPIDS_CANDIDATE_RAPIDS_CMAKE_SHA": "a" * 40,
        },
        capture_output=True,
        text=True,
        check=True,
        timeout=5,
    )
    expected = arguments if arguments[0].startswith("--") else ["-Drapids-cmake-sha=" + "a" * 40, *arguments]
    if result.stdout.splitlines() != expected:
        pytest.fail("newly installed CMake did not receive the expected source pin and arguments")
    if result.stderr:
        pytest.fail("candidate CMake unexpectedly invoked the old executable")
