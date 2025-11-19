# Copyright (c) 2025, NVIDIA CORPORATION.

from dunamai import Version


version = Version.from_git()
with open("VERSION") as f:
    version_contents = f.read()

if version_contents != f"{version}\n":
    print(f"Expected \"{version}\" in VERSION file, got:")
    print(version_contents)
    exit(1)
