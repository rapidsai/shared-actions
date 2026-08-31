# Release catalog companion archives

This action creates a companion archive alongside our binary artifacts (conda,
wheel or otherwise) that contains an inventory of the binary artifacts that were
produced by that job. That inventory is used by the RAPIDS release system to
assemble release candidates. "Release candidates" here refers to a set of
artifacts that can be deployed to create a release, rather than a release
candidate version of one artifact.

## Companion contents

Each build job creates one companion archive with this shape:

```text
.
├── release-catalog-entries.json
└── release-evidence
    ├── <artifact-1>.<artifact-1-sha256>.provenance.json
    ├── <artifact-1>.<artifact-1-sha256>.sbom.cdx.json
    ├── <artifact-1>.<artifact-1-sha256>.sbom.spdx.json
    ├── <artifact-2>.<artifact-2-sha256>.provenance.json
    ├── <artifact-2>.<artifact-2-sha256>.sbom.cdx.json
    ├── <artifact-2>.<artifact-2-sha256>.sbom.spdx.json
    └── ...
```

`release-catalog-entries.json` records the artifacts produced by the job:
`.conda`, `.whl`, or otherwise, as well as the checksums of those files.

The release-evidence folder carries additional metadata that may be useful to
consumers of our packages: provenance and software bill of materials (SBOM).
Provenance describes how a package was built and by whom. An SBOM lists
components that make up the package. They complement each other: an SBOM without
provenance may describe the wrong or an untrusted artifact, while provenance
without an SBOM cannot efficiently answer what vulnerable components are inside.
For each artifact in its `entries` array, the action creates one provenance file
and equivalent identity SBOMs in SPDX and CycloneDX formats. The SHA-256 in each
evidence filename is the digest of that specific artifact's contents. The
catalog entry identifies both SBOM paths under `sboms`, allowing consumers to
select their preferred format without inferring it from the filename.

The provenance.json file follows the
[SLSA](https://slsa.dev/spec/v1.2/attestation-model) standard format.

The `sbom.spdx.json` file uses [SPDX
2.3](https://spdx.github.io/spdx-spec/v2.3/), while `sbom.cdx.json` uses
[CycloneDX 1.6](https://cyclonedx.org/docs/1.6/json/). These generated documents
identify the artifact and its digest; they do not contain a dependency
inventory.

Provenance remains separate because it is an independently verifiable statement
about how and where the artifact was built. Although both SBOM standards can
carry some build metadata, embedding provenance in the SBOMs would not replace
the in-toto/SLSA statement and would couple evidence with different consumers
and lifecycles.

## Action Configuration

Every caller passes one `config` JSON object. The canonical schema and field
documentation are in [`config.schema.json`](config.schema.json). This is
validated at runtime with the [validate-config.sh](validate-config.sh) script.

### `release_catalog_key`

This is a grouping label that represents a common "release policy." Artifacts
that share release_catalog_key are versioned, validated, ordered, published, and
promoted together. The key does not need to be unique per artifact, nor per
matrix variant. For example, `cudf` and `dask-cudf` Conda packages can both use
`conda:cudf` because they share the same version, validation, inter-package
order, publishing destination, and are ultimately published together.

Standard RAPIDS Conda and wheel workflows use `<ecosystem>:<repository-name>`,
such as `conda:cudf`, because those workflows apply a repository-level release
policy. A custom producer should use `<ecosystem>:<release-group>`, such as
`maven:cuvs-java`, or simply `maven:cuvs`. Use a separate key only when the
outputs intentionally have a separate release workflows that must be followed.

### `artifact_directory` and `artifacts`

`artifact_directory` is the base directory containing the primary artifacts.
For standard Conda and wheel jobs, omit `artifacts`; the action discovers all
Conda packages and wheels below that directory and parses metadata from them.

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

Other formats require an explicit `artifacts` list. Each dictionary in the list
corresponds to one artifact file. Wildcards are allowed, but a wildcard that
resolves to zero files or multiple files results in an error.

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

#### Package identity file

package_identity_file is a JSON file containing at least `ecosystem`, `name`,
and `version`; `build` and `platform` are optional. This path is relative to
`artifact_directory`.

```json
{
  "ecosystem": "maven",
  "name": "ai.rapids:cuvs-java",
  "version": "26.08.0"
}
```

Multiple artifacts in the list may reference the same identity file when the
files have the same package identity.
