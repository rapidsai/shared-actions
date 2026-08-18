# Release catalog companions

`release-catalog-dispatch` records the exact files produced by a package
build and uploads a companion GitHub Actions artifact named
`release-catalog-<source-artifact-name>`. Release tooling uses the
companion to associate primary artifacts with their build identity and generated
identity evidence without reconstructing the build later.

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

When `artifacts` is omitted, the action discovers every Conda and wheel output
in `artifact_directory` and extracts identity from each package independently.
When `artifacts` is supplied, each descriptor is resolved independently:
supported Conda and wheel files are parsed, while any other artifact requires
its own `package_identity_file`.

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

The action validates the configuration before inspecting build outputs. It then
verifies properties that depend on produced files, including the package
identity contents and whether primary-artifact paths resolve unambiguously.

## Source revision

The action requires `RAPIDS_SHA` in the job environment. It must identify the
repository revision actually checked out and built:

- Conda build workflows set `RAPIDS_SHA` to `git rev-parse HEAD` immediately
  after checkout.
- Wheel and custom workflows use `rapids-github-info`; it uses `inputs.sha`
  when supplied and otherwise sets `RAPIDS_SHA` to `git rev-parse HEAD`.

This distinction matters when a reusable workflow checks out a repository or
revision different from the workflow event. Direct callers must likewise set
`RAPIDS_SHA` to the checked-out commit instead of assuming `${{ github.sha }}`
names the built source. Variables written to `GITHUB_ENV` are available to the
action when it runs as a subsequent job step.

## Standard package example

```yaml
- name: Create wheel release catalog companion
  uses: rapidsai/shared-actions/release-catalog-dispatch@main
  with:
    config: >-
      {
        "release_catalog_key": "wheel:example",
        "artifact_directory": ${{ toJSON(steps.package-name.outputs.WHEEL_OUTPUT_DIR) }}
      }
    source-artifact-name: ${{ steps.package-name.outputs.RAPIDS_PACKAGE_NAME }}
```

## Custom package example

```yaml
- name: Create custom release catalog companion
  uses: rapidsai/shared-actions/release-catalog-dispatch@main
  with:
    config: >-
      {
        "release_catalog_key": "maven:cuvs-java",
        "artifact_directory": "java/cuvs-java/target",
        "artifacts": [{
          "path": "cuvs-java-*-x86_64-cuda*.jar",
          "package_identity_file": "cuvs-java.release-package-identity.json"
        }]
      }
    source-artifact-name: cuvs-java
```

Before the action runs, the producer creates
`java/cuvs-java/target/cuvs-java.release-package-identity.json`. The
artifact descriptor's `package_identity_file` value is the path to that file
relative to `artifact_directory`. For example:

```json
{
  "ecosystem": "maven",
  "name": "ai.rapids:cuvs-java",
  "version": "26.08.0"
}
```

## Generated identity evidence

For every artifact, the action generates an SPDX artifact-identity envelope
classified as `generated-identity` and a build-context provenance statement.
They record the artifact SHA-256, package identity, source revision, and workflow
context, but contain no dependency or source-license inventory. They must not be
reported as producer-supplied dependency coverage.

Producer-supplied SBOM, provenance, and signature inputs are intentionally
deferred until a future revision defines a generic, unambiguous way to associate
them with detected artifacts.
