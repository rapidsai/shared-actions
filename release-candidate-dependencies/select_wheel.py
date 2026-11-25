# Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.

"""Select wheel files compatible with the target Python interpreter and architecture.

The producing job's Python matrix is useful for ranking otherwise equivalent
outputs, but it is not the wheel compatibility contract.  PEP 425 wheel tags
are that contract: pure Python and ``abi3`` wheels often serve more than the
one interpreter that built them.

A wheel for another architecture is never installed by the row, so it is not
downloaded either. That keeps it out of the row's reuse key, so a rebuild of
one architecture's upstream wheel does not rebuild the other architecture's
downstream rows.
"""

from __future__ import annotations

import argparse
import re

from packaging.utils import InvalidWheelFilename, parse_wheel_filename


def supports_python(wheel_name: str, python_version: str) -> bool:
    """Return whether one wheel's interpreter and ABI tags allow this Python."""
    target = _python_version(python_version)
    try:
        _name, _version, _build, tags = parse_wheel_filename(wheel_name)
    except InvalidWheelFilename:
        return False
    return any(_tag_supports_python(tag.interpreter, tag.abi, target) for tag in tags)


# Linux platform tags end in the machine name: manylinux_2_28_x86_64, musllinux_1_2_aarch64, linux_x86_64.
_MACHINES = {"amd64": "x86_64", "x86_64": "x86_64", "arm64": "aarch64", "aarch64": "aarch64"}
_LINUX_PLATFORM = re.compile(r"(?:manylinux\d*(?:_\d+_\d+)?|musllinux_\d+_\d+|linux)_(x86_64|aarch64)")


def supports_arch(wheel_name: str, arch: str) -> bool:
    """Return whether one wheel's platform tags allow a Linux row of this architecture."""
    machine = _machine(arch)
    try:
        _name, _version, _build, tags = parse_wheel_filename(wheel_name)
    except InvalidWheelFilename:
        return False
    for tag in tags:
        if tag.platform == "any":
            return True
        match = _LINUX_PLATFORM.fullmatch(tag.platform)
        if match is not None and match.group(1) == machine:
            return True
    return False


def _machine(arch: str) -> str:
    try:
        return _MACHINES[arch]
    except KeyError:
        raise ValueError(f"unsupported candidate architecture: {arch}") from None


def _python_version(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"(\d+)\.(\d+)", value)
    if match is None:
        raise ValueError("python version must use MAJOR.MINOR form")
    return int(match.group(1)), int(match.group(2))


def _tag_supports_python(interpreter: str, abi: str, target: tuple[int, int]) -> bool:
    if interpreter == f"py{target[0]}" or interpreter == f"py{target[0]}{target[1]}":
        return True
    match = re.fullmatch(r"cp(\d)(\d+)", interpreter)
    if match is None:
        return False
    producer = int(match.group(1)), int(match.group(2))
    if producer[0] != target[0]:
        return False
    if abi == "abi3":
        return producer <= target
    return producer == target


def main() -> None:
    """Exit successfully only when the supplied wheel supports the target."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--python-version", required=True)
    parser.add_argument("--arch", required=True)
    parser.add_argument("wheel")
    args = parser.parse_args()
    try:
        _machine(args.arch)  # an unknown architecture is a usage error even when the Python version already fails
        compatible = supports_python(args.wheel, args.python_version) and supports_arch(args.wheel, args.arch)
    except ValueError as error:
        parser.error(str(error))
    if not compatible:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
