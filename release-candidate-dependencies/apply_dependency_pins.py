# Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.

"""Apply train-authorized dependency versions to a disposable source checkout."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

_SUPPORTED_PINS = {"ucx"}
_PACKAGE = re.compile(
    r"(?P<prefix>[ \t,\[\"'])"
    r"(?P<name>(?:lib)?ucx(?:-cu\d+)?)(?![\w-])"
    r"(?P<extras>\[[^\]]+\])?"
    r"(?P<spec>(?:\s*(?:==|!=|>=|<=|~=|=|>|<)\s*v?\d[^\s,;\"'\]]*"
    r"(?:\s*,\s*(?:==|!=|>=|<=|~=|=|>|<)?\s*v?\d[^\s,;\"'\]]*)*)?)"
)
_DIRECT_URL = re.compile(r"[ \t,\[\"'](?:lib)?ucx(?:-cu\d+)?(?![\w-])(?:\[[^\]]+\])?\s*@\s*[^\s,\"']+")
_VERSION = re.compile(r"^\d+\.\d+\.\d+$")


class PinError(ValueError):
    """The receipt or candidate source tree does not satisfy the pin contract."""


def read_pins(receipt_path: Path) -> dict[str, str]:
    """Read and validate the optional dependency pin mapping."""
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise PinError(f"cannot read variant receipt: {error}") from error
    if not isinstance(receipt, dict):
        raise PinError("variant receipt must be a JSON object")
    if "dependency_pins" not in receipt:
        return {}
    pins = receipt["dependency_pins"]
    if not isinstance(pins, dict):
        raise PinError("dependency_pins must be an object")
    unknown = set(pins) - _SUPPORTED_PINS
    if unknown:
        raise PinError(f"unsupported dependency pin(s): {', '.join(sorted(unknown))}")
    validated: dict[str, str] = {}
    for name, version in pins.items():
        if not isinstance(name, str) or not isinstance(version, str) or not _VERSION.fullmatch(version):
            raise PinError(f"invalid dependency pin for {name!r}: expected a full X.Y.Z version")
        validated[name] = version
    return validated


def _next_patch(version: str) -> str:
    major, minor, patch = (int(part) for part in version.split("."))
    return f"{major}.{minor}.{patch + 1}"


def _rewrite_requirement_line(line: str, version: str) -> str:
    wheel_spec = f">={version},<{_next_patch(version)}"
    if line.lstrip().startswith("#"):
        return line
    if _DIRECT_URL.search(line):
        raise PinError("direct URL UCX requirements cannot be safely pinned")

    protected_macros: list[str] = []

    def protect_macro(match: re.Match[str]) -> str:
        protected_macros.append(match.group(0))
        return f"__UCX_PIN_PROTECTED_MACRO_{len(protected_macros) - 1}__"

    line = re.sub(r"pin_compatible\(\s*(['\"])ucx\1", protect_macro, line)

    # Resolve UCXX's Jinja declarations before matching normal specs.
    line = re.sub(r"(\bucx\s+)=[ \t]*\$\{\{\s*ucx_min\s*\}\}", rf"\1=={version}", line)
    line = re.sub(
        r"(\bucx\s+)>=\$\{\{\s*ucx_min\s*\}\},\s*<\$\{\{\s*ucx_max\s*\}\}",
        rf"\1=={version}",
        line,
    )

    def replace_package(match: re.Match[str]) -> str:
        name = match.group("name")
        spec = wheel_spec if name.startswith("libucx") else f"=={version}"
        return f"{match.group('prefix')}{name}{match.group('extras') or ''}{spec}"

    line = _PACKAGE.sub(replace_package, line)
    for index, macro in enumerate(protected_macros):
        line = line.replace(f"__UCX_PIN_PROTECTED_MACRO_{index}__", macro)
    return line


def _rewrite_recipe(text: str, version: str) -> str:
    result: list[str] = []
    requirements_indent: int | None = None
    ignored_exports_indent: int | None = None
    for line in text.splitlines(keepends=True):
        indentation = len(line) - len(line.lstrip(" \t"))
        stripped = line.strip()
        if requirements_indent is not None and stripped and indentation <= requirements_indent:
            requirements_indent = None
        if ignored_exports_indent is not None:
            if stripped and indentation <= ignored_exports_indent:
                ignored_exports_indent = None
            else:
                result.append(line)
                continue
        if re.match(r"^\s*ignore_run_exports\s*:", line):
            ignored_exports_indent = indentation
            result.append(line)
            continue
        if re.match(r"^\s*requirements\s*:", line):
            requirements_indent = indentation
            result.append(line)
            continue
        result.append(_rewrite_requirement_line(line, version) if requirements_indent is not None else line)
    return "".join(result)


def _rewrite_yaml_list(text: str, version: str, key: str) -> str:
    result: list[str] = []
    list_indent: int | None = None
    for line in text.splitlines(keepends=True):
        indentation = len(line) - len(line.lstrip(" \t"))
        stripped = line.strip()
        if list_indent is not None and stripped:
            is_list_item = stripped.startswith("-")
            if indentation < list_indent or (indentation == list_indent and not is_list_item):
                list_indent = None
        inline_list = re.match(rf"^([ \t]*{re.escape(key)}[ \t]*:[ \t]*\[)(.*)(\][ \t]*(?:#.*)?\n?)$", line)
        if inline_list:
            result.append(_rewrite_requirement_line(line, version))
            continue
        if re.match(rf"^\s*{re.escape(key)}\s*:\s*(?:#.*)?$", line):
            list_indent = indentation
            result.append(line)
            continue
        result.append(_rewrite_requirement_line(line, version) if list_indent is not None else line)
    return "".join(result)


def _rewrite_pyproject(text: str, version: str) -> str:
    result: list[str] = []
    section = ""
    requirement_array: bool = False
    for line in text.splitlines(keepends=True):
        section_match = re.match(r"^\s*\[([^\]]+)\]", line)
        if section_match:
            section = section_match.group(1)
            requirement_array = False
            result.append(line)
            continue
        key_match = re.match(r"^\s*([A-Za-z0-9_.-]+)\s*=\s*\[", line)
        if key_match:
            key = key_match.group(1)
            requirement_array = (
                (section == "project" and key == "dependencies")
                or (section == "project.optional-dependencies")
                or (section in {"build-system", "tool.rapids-build-backend"} and key == "requires")
            )
            result.append(_rewrite_requirement_line(line, version) if requirement_array else line)
            if requirement_array and line.rstrip().endswith("]"):
                requirement_array = False
            continue
        if requirement_array:
            result.append(_rewrite_requirement_line(line, version))
            if line.strip().endswith("]"):
                requirement_array = False
        else:
            result.append(line)
    return "".join(result)


def _apply_ucx(text: str, version: str, path: Path) -> str:
    if path.name in {"conda_build_config.yaml", "variants.yaml"}:
        for key, pinned_value in (
            ("ucx_min", version),
            ("ucx_max", _next_patch(version)),
            ("ucx_version", version),
        ):
            text = re.sub(
                rf'(?m)^([ \t]*{key}:[ \t]*\n[ \t]*-[ \t]*)["\']?\d+(?:\.\d+){{0,2}}["\']?',
                rf'\g<1>"{pinned_value}"',
                text,
            )
            text = re.sub(
                rf'(?m)^([ \t]*{key}:[ \t]*\[)[ \t]*(["\']?)\d+(?:\.\d+){{0,2}}\2([ \t]*\])',
                rf'\g<1>"{pinned_value}"\g<3>',
                text,
            )
        return text
    if path.name == "dependencies.yaml":
        return _rewrite_yaml_list(text, version, "packages")
    if path.name == "recipe.yaml":
        return _rewrite_recipe(text, version)
    if path.name == "pyproject.toml":
        return _rewrite_pyproject(text, version)
    if path.parent.name == "environments":
        return _rewrite_yaml_list(text, version, "dependencies")
    return text


def apply_pins(checkout: Path, pins: dict[str, str]) -> list[Path]:
    """Apply supported pins to dependency declarations in the checkout."""
    if not pins:
        return []
    if not checkout.is_dir():
        raise PinError(f"candidate checkout is not a directory: {checkout}")
    changes: dict[Path, str] = {}
    checkout_root = checkout.resolve()
    for path in checkout.rglob("*"):
        if path.suffix not in {".yaml", ".yml", ".toml"}:
            continue
        relative_path = path.relative_to(checkout)
        is_dependency_file = path.name in {
            "dependencies.yaml",
            "recipe.yaml",
            "conda_build_config.yaml",
            "variants.yaml",
            "pyproject.toml",
        }
        is_conda_environment = len(relative_path.parts) > 2 and relative_path.parts[:2] == ("conda", "environments")
        if not is_dependency_file and not is_conda_environment:
            continue
        resolved = path.resolve()
        if not resolved.is_relative_to(checkout_root):
            raise PinError(f"dependency file resolves outside candidate checkout: {path}")
        if not path.is_file():
            continue
        original = resolved.read_text(encoding="utf-8")
        updated = original
        if "ucx" in pins:
            updated = _apply_ucx(updated, pins["ucx"], path)
        if updated != original:
            changes[resolved] = updated
    # Do all parsing/transforms before the first write so malformed inputs
    # cannot leave a partially pinned checkout behind.
    for path, content in changes.items():
        path.write_text(content, encoding="utf-8", newline="")
    return sorted(changes)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt", required=True, type=Path)
    parser.add_argument("--checkout", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        pins = read_pins(args.receipt)
        changed = apply_pins(args.checkout, pins)
    except PinError as error:
        print(f"dependency pin application failed: {error}", file=sys.stderr)
        return 1
    if pins:
        print(f"applied dependency pins {json.dumps(pins, sort_keys=True)} to {len(changed)} file(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
