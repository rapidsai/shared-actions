#!/usr/bin/env python3
# Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.

"""Add release-candidate lock metapackages to a disposable Rattler recipe."""

from __future__ import annotations

import argparse
import hashlib
import re
import shutil
from pathlib import Path

import yaml


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recipe", type=Path, required=True)
    parser.add_argument("--build-lock", required=True)
    parser.add_argument("--host-lock", required=True)
    parser.add_argument("--rapids-cmake-sha", required=True)
    arguments = parser.parse_args()

    source_is_directory = arguments.recipe.is_dir()
    recipe = _resolve_recipe(arguments.recipe, parser)
    destination_directory = _copy_recipe_directory(recipe, arguments.build_lock, arguments.host_lock)
    destination_recipe = destination_directory / "recipe.yaml"
    document = yaml.safe_load(destination_recipe.read_text())
    if not isinstance(document, dict):
        parser.error(f"recipe must be a YAML mapping: {recipe}")
    exact_variant_keys = _local_exact_variant_keys(recipe)
    outputs = document.get("outputs", [])
    if outputs is not None:
        if not isinstance(outputs, list):
            parser.error(f"recipe outputs must be a list: {recipe}")
        for output in outputs:
            if not isinstance(output, dict):
                parser.error(f"recipe output must be a mapping: {recipe}")
    _prepare_recipe_documents(
        document,
        arguments.build_lock,
        arguments.host_lock,
        arguments.rapids_cmake_sha,
        exact_variant_keys,
    )

    destination_recipe.write_text(yaml.safe_dump(document, sort_keys=False))
    print(destination_directory if source_is_directory else destination_recipe)
    return 0


def _add_requirements(document: dict, build_lock: str, host_lock: str) -> None:
    requirements = document.setdefault("requirements", {})
    if not isinstance(requirements, dict):
        raise ValueError("recipe requirements must be a mapping")
    for section, lock in (("build", build_lock), ("host", host_lock)):
        values = requirements.setdefault(section, [])
        if not isinstance(values, list):
            raise ValueError(f"recipe requirements.{section} must be a list")
        if lock not in values:
            values.append(lock)


def _candidate_cmake_preamble(rapids_cmake_sha: str) -> str:
    """Return Bash setup that pins configure-mode CMake and scikit-build."""
    return f"""# Release candidates must never fetch RAPIDS CMake from a moving branch.
cmake() {{
  case "${{1:-}}" in
    --build|--install|--open|-E|-P|--help|--version) command cmake "$@" ;;
    *) command cmake "-Drapids-cmake-sha={rapids_cmake_sha}" "$@" ;;
  esac
}}
export -f cmake
export SKBUILD_CMAKE_DEFINE="${{SKBUILD_CMAKE_DEFINE:+${{SKBUILD_CMAKE_DEFINE}};}}rapids-cmake-sha={rapids_cmake_sha}"
"""


def _prepend_cmake_preamble(script: object, rapids_cmake_sha: str) -> object:
    preamble = _candidate_cmake_preamble(rapids_cmake_sha)
    if isinstance(script, str):
        return f"{preamble}\n{script}"
    if isinstance(script, list):
        return [preamble, *script]
    if isinstance(script, dict):
        content = script.get("content")
        if content is None:
            raise ValueError("release-candidate recipe script object must define content")
        patched = dict(script)
        patched["content"] = _prepend_cmake_preamble(content, rapids_cmake_sha)
        return patched
    raise ValueError("release-candidate recipe script must be a string, list, or content object")


def _pin_cmake_in_build_script(document: dict, rapids_cmake_sha: str) -> None:
    """Place the exact CMake definition inside Rattler's isolated shells.

    Rattler does not inherit the workflow PATH into its build shell, so a
    workflow-level CMake wrapper cannot protect scripts that invoke ``cmake``
    directly. Package tests intentionally remain unmodified: they validate
    the produced package rather than the candidate build environment.
    """
    build = document.get("build")
    if isinstance(build, dict) and "script" in build:
        build["script"] = _prepend_cmake_preamble(build["script"], rapids_cmake_sha)


def _resolve_recipe(candidate: Path, parser: argparse.ArgumentParser) -> Path:
    recipe = candidate.resolve()
    if recipe.is_dir():
        recipe = recipe / "recipe.yaml"
    if not recipe.is_file():
        parser.error(f"recipe must be a file or a directory containing recipe.yaml: {candidate}")
    return recipe


def _copy_recipe_directory(recipe: Path, build_lock: str, host_lock: str) -> Path:
    """Create a same-parent disposable recipe directory named for Rattler."""
    digest = hashlib.sha256(f"{build_lock}\0{host_lock}".encode()).hexdigest()[:12]
    destination = recipe.parent.parent / f".{recipe.parent.name}.release-candidate-{digest}"
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(recipe.parent, destination)
    return destination


def _prepare_recipe_documents(
    document: dict, build_lock: str, host_lock: str, rapids_cmake_sha: str, exact_variant_keys: set[str]
) -> None:
    """Inject locks into the actual build target of a Rattler recipe.

    A recipe without outputs produces its root package, while every member of
    a multi-output recipe is an independent build target. Rattler-Build does
    not permit top-level requirements for the latter, so first distribute the
    shared requirements into every output and remove the root field.
    """
    _pin_cmake_in_build_script(document, rapids_cmake_sha)
    cache = document.get("cache")
    if cache is not None:
        if not isinstance(cache, dict):
            raise ValueError("recipe cache must be a mapping")
        # Cache recipes have a shared build phase before output packages are
        # assembled. Its solve and shell are just as much part of the
        # candidate build as an output's own requirements and script.
        _add_requirements(cache, build_lock, host_lock)
        _pin_cmake_in_build_script(cache, rapids_cmake_sha)
    outputs = document.get("outputs")
    if not outputs:
        _prepare_document(document, build_lock, host_lock, exact_variant_keys, add_locks=True)
        return

    _move_root_requirements_to_outputs(document, outputs)
    for output in outputs:
        _pin_cmake_in_build_script(output, rapids_cmake_sha)
        _prepare_document(output, build_lock, host_lock, exact_variant_keys, add_locks=True)


def _prepare_document(
    document: dict, build_lock: str, host_lock: str, exact_variant_keys: set[str], *, add_locks: bool
) -> None:
    if add_locks:
        _add_requirements(document, build_lock, host_lock)
    requirements = document["requirements"]
    for section, values in requirements.items():
        if not isinstance(values, list):
            continue
        requirements[section] = [_add_exact_operator(value, exact_variant_keys) for value in values]


def _move_root_requirements_to_outputs(document: dict, outputs: list[dict]) -> None:
    """Copy shared requirements into every output before removing the root field."""
    root_requirements = document.pop("requirements", None)
    if root_requirements is None:
        return
    if not isinstance(root_requirements, dict):
        raise ValueError("recipe requirements must be a mapping")
    for section, shared_values in root_requirements.items():
        if not isinstance(shared_values, list):
            raise ValueError(f"recipe requirements.{section} must be a list")
        for output in outputs:
            requirements = output.setdefault("requirements", {})
            if not isinstance(requirements, dict):
                raise ValueError("recipe output requirements must be a mapping")
            values = requirements.setdefault(section, [])
            if not isinstance(values, list):
                raise ValueError(f"recipe output requirements.{section} must be a list")
            requirements[section] = [*shared_values, *(value for value in values if value not in shared_values)]


def _local_exact_variant_keys(recipe: Path) -> set[str]:
    keys = set()
    for filename in ("variants.yaml", "conda_build_config.yaml"):
        path = recipe.parent / filename
        if not path.is_file():
            continue
        document = yaml.safe_load(path.read_text())
        if not isinstance(document, dict):
            continue
        for key, values in document.items():
            if (
                isinstance(key, str)
                and isinstance(values, list)
                and any(isinstance(value, str) and value.strip().startswith("==") for value in values)
            ):
                keys.add(key)
    return keys


def _add_exact_operator(value: object, exact_variant_keys: set[str]) -> object:
    if not isinstance(value, str) or not exact_variant_keys:
        return value
    names = "|".join(re.escape(key) for key in sorted(exact_variant_keys))
    expression = rf"^(\S+)\s+(\$\{{\{{\s*(?:{names})\s*\}}\}})$"
    return re.sub(expression, r"\1 ==\2", value)


if __name__ == "__main__":
    raise SystemExit(main())
