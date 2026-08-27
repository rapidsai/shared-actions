# Release catalog companions

`release-catalog-dispatch` records the exact files produced by a package
build and uploads a companion GitHub Actions artifact named
`release-catalog-<source-artifact-name>`. Release tooling uses the
companion to associate primary artifacts with their build identity and generated
identity evidence without reconstructing the build later. This evidence supports:

- [assembling releases that can be tested and verified before formal tagging](https://github.com/rapidsai/release-scripts/issues/102)
- using build-time metadata to triage CVE scan records more quickly (internal
  GitLab project, `nspect-manager`)

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

Each matrix job produces its own companion artifact. The release platform merges
their entries into one composite release catalog.

## Configuration

Every build job that calls this action passes one `config` JSON object. Its
canonical schema and field documentation are in
[`config.schema.json`](config.schema.json).

The action validates the configuration before inspecting build outputs. It then
verifies properties that depend on produced files, including the package
identity contents and whether primary-artifact paths resolve unambiguously.

### `release_catalog_key`

The `release_catalog_key` is used to group artifacts in the catalog. Every file
from every matrix variant in the same publishable artifact set uses the same
key. The release platform aggregates multiple `release-catalog-entries.json`
files and coalesces entries with the same `release_catalog_key`. Standard RAPIDS
conda and wheel workflows construct the key as `<ecosystem>:<repository-name>`,
such as `conda:cudf`. Custom producers select a key, such as `maven:cuvs-java`,
that represents their release policy.

Use a distinct `release_catalog_key` only when the outputs intentionally have
different release policies. Reasons include differences in:

- versioning schemes
- validation requirements
- dependency ordering
- publication destinations
- promotion strategy

Multiple package names from one repository do not by themselves justify separate
keys: for example, `cudf` and `dask-cudf` Conda packages remain part of
`conda:cudf` when they share one release policy.

### `artifacts`

This is a list of objects, where each object corresponds to exactly one artifact
(filename). Standard Conda and wheel workflows do not explicitly specify
`artifacts`. Instead, the action discovers every Conda and wheel output in
`artifact_directory` and creates corresponding artifact entries.

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

For artifacts other than conda and wheels, `path` and `package_identity_file` must
be specified:

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

Wildcards are allowed to allow for variance in filenames, but each wildcard must
resolve to only one file. In other words, each list object corresponds to
exactly one artifact, and any ambiguity is an error.

#### Package identity file

Package identity is high-level information about an artifact outside of its
filename. It requires `ecosystem`, `name`, and `version` and may include `build`
and `platform`. The action implicitly parses this information from conda and
wheel artifacts, but other artifact formats require the producer to provide it
explicitly using the `package_identity_file` parameter. Any referenced package
identity file must be created before this action runs. An artifact descriptor's
`package_identity_file` is the path to that file relative to
`artifact_directory`. Example contents of a package_identity_file:

```json
{
  "ecosystem": "maven",
  "name": "ai.rapids:cuvs-java",
  "version": "26.08.0"
}
```

Multiple artifact descriptors may reference the same identity file when those
artifacts have the same package identity.

## Prerequisite state

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

## Candidate build reuse

For a release-candidate build, `release-candidate-dependencies` writes
`RELEASE_CANDIDATE_UPSTREAM_INPUTS` to `GITHUB_ENV`. It is a sorted lock of the
exact upstream Conda and wheel files actually materialized for the current job
matrix. Each entry contains the file SHA-256 plus the producing RAPIDS
repository SHA and build-input digest. `release-catalog` incorporates that lock
into `build-record.json`, along with the train's frozen `shared-workflows`,
`shared-actions`, and `gha-tools` revisions. The record's SHA-256 determines
the canonical artifact location. Therefore an unchanged build can be reused
across candidate trains and GitHub retries, while a downstream build is
necessarily distinct when an upstream package byte, selected upstream build, or
shared build implementation changes. The lock intentionally omits GitHub run
and attempt IDs.

Before a candidate job can use this mechanism, its dependency setup compares
the running reusable workflow SHA and composite-action SHA with the train's
`shared-workflows` and `shared-actions` entries. It also installs `gha-tools`
at the train's exact SHA and makes that checkout the job's tool path. The
catalog uploader repeats the `shared-actions` and `gha-tools` comparison before
writing S3 content. A mismatch is a build error, not an informational warning:
one train must not combine artifacts produced by different shared build logic.

## Generated identity evidence

For every artifact, the action generates an SPDX artifact-identity envelope
classified as `generated-identity` and a build-context provenance statement.
They record the artifact SHA-256, package identity, source revision, and workflow
context, but contain no dependency or source-license inventory. They must not be
reported as producer-supplied dependency coverage.

Producer-supplied dependency SBOMs and richer build provenance, such as build
dependencies and compiler flags, would make package contents easier to
understand without downloading them. Associating that input-side evidence with
artifacts is left for future work.
