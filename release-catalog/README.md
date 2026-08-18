# Release catalog companions

`release-catalog-dispatch` records the exact files produced by a package
build and uploads a companion GitHub Actions artifact named
`release-catalog-<source-artifact-name>`. Release tooling uses the
companion to associate primary artifacts with their build identity and
available evidence without reconstructing the build later.

The companion contains:

```text
.
├── release-catalog-entries.json
└── release-evidence
    ├── <artifact evidence>.provenance.json
    └── <artifact evidence>.spdx.json
```

`release-catalog-entries.json` is one atomic job-level envelope. Its `source`
object records the source artifact and build context, while its `entries` array
contains the release catalog entries produced by that job. The release platform
validates and aggregates entry arrays from selected builds into the release
catalog; there is no separate metadata document to keep synchronized.

## Configuration

Every producer passes one `config` JSON object. Its canonical schema and field
documentation are in [`config.schema.json`](config.schema.json).

The `artifact_type` field determines how the action discovers primary files:

- `conda` and `wheel` derive artifact descriptors and package identity from the
  built packages;
- `custom` requires explicit artifact descriptors and exactly one of `package`
  or `package_file`.

`release_catalog_key` identifies the release catalog entry that owns these
artifacts. Every file from every matrix variant in the same publishable artifact
set uses the same key, and multiple files are aggregated. Standard RAPIDS Conda
and wheel workflows construct it as `<ecosystem>:<repository-name>`, such as
`conda:cudf`; custom producers select an existing catalog key, such as
`maven:cuvs-java`. Do not generate a UUID or a per-build value.

Use a distinct `release_catalog_key` only when the release catalog intentionally gives the
outputs different release policy. Examples include:

* different versioning
* validation requirements
* dependency ordering
* publication destinations
* promotion strategy.

Multiple package names from one repository do not by themselves justify separate
keys: for example, `cudf` and `dask-cudf` Conda packages remain part of
`conda:cudf` when they share one release policy.

The action validates the complete configuration before inspecting build
outputs. It then verifies properties that depend on produced files, including
whether package, primary-artifact, and evidence paths resolve unambiguously.

## Source revision

`source-sha` must identify the repository revision actually checked out and
built. RAPIDS shared workflows pass `${{ env.RAPIDS_SHA }}`:

- Conda build workflows set `RAPIDS_SHA` to `git rev-parse HEAD` immediately
  after checkout.
- Wheel and custom workflows use `rapids-github-info`; it uses `inputs.sha`
  when supplied and otherwise sets `RAPIDS_SHA` to `git rev-parse HEAD`.

This distinction matters when a reusable workflow checks out a repository or
revision different from the workflow event. Direct callers should likewise
resolve the checked-out commit instead of assuming `${{ github.sha }}` names
the built source.

## Standard package example

```yaml
- name: Create wheel release catalog companion
  uses: rapidsai/shared-actions/release-catalog-dispatch@main
  with:
    config: >-
      {
        "artifact_type": "wheel",
        "release_catalog_key": "wheel:example",
        "output_directory": ${{ toJSON(steps.package-name.outputs.WHEEL_OUTPUT_DIR) }}
      }
    source-artifact-name: ${{ steps.package-name.outputs.RAPIDS_PACKAGE_NAME }}
    source-sha: ${{ env.RAPIDS_SHA }}
```

## Custom package example

```yaml
- name: Create custom release catalog companion
  uses: rapidsai/shared-actions/release-catalog-dispatch@main
  with:
    config: >-
      {
        "artifact_type": "custom",
        "release_catalog_key": "maven:cuvs-java",
        "output_directory": "java/cuvs-java/target",
        "package_file": "cuvs-java.release-package.json",
        "artifacts": [{"path": "cuvs-java-*-x86_64-cuda*.jar"}]
      }
    source-artifact-name: cuvs-java
    source-sha: ${{ env.RAPIDS_SHA }}
```

## Evidence semantics

A descriptor-selected producer SBOM is classified as `producer-dependency`.
It is evidence supplied by the producer and may contain a dependency inventory.

When no SBOM is selected, the action generates an SPDX artifact-identity
envelope classified as `generated-identity`. It records package identity and
the primary artifact SHA-256, but contains no dependency or source-license
inventory. It must not be reported as producer-supplied dependency coverage.

Producer-supplied SBOM, provenance, and signature sidecars are copied under
`release-evidence/` so the companion remains independently consumable.
