# Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.

"""Select wheel files compatible with the target Python interpreter.

The producing job's Python matrix is useful for ranking otherwise equivalent
outputs, but it is not the wheel compatibility contract.  PEP 425 wheel tags
are that contract: pure Python and ``abi3`` wheels often serve more than the
one interpreter that built them.
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
    parser.add_argument("wheel")
    args = parser.parse_args()
    try:
        compatible = supports_python(args.wheel, args.python_version)
    except ValueError as error:
        parser.error(str(error))
    if not compatible:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
