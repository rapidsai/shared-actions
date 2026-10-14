# Release-candidate dependency resolver

This composite action materializes only build dependencies declared by the
immutable release train and recorded by the corresponding candidate catalogs.
It checks the train digest, producer run binding, canonical artifact index, and
artifact bytes before exposing package files to the caller.

`artifact-family` selects the target release unit. `conda` and `wheel` resolve
their same-family dependencies; `maven` resolves declared Maven dependencies
into a private local Maven repository. `docs` and `container` resolve only
declared Conda and Maven dependencies. Docs and container targets do not need a
repository Conda build lock because they are consumers rather than Conda
package builds.

Set `candidate-unit-id` when the train's release unit name differs from the
repository name (for example, a Java package built in the `cudf` repository
may use `maven:cudf-java`). The resolver verifies that the ID uses the chosen
family and exists in the frozen train.

For `maven`, the action extracts verified tar.gz Maven repository archives into
`MAVEN_LOCAL_REPO` and appends that path to `MAVEN_OPTS`. For docs and container
jobs, set `prepare-conda-channel: true` to build a job-local Conda channel from
the exact selected package files. The action exports its path as
`RAPIDS_CANDIDATE_CONDA_CHANNEL`; callers should not add public RAPIDS channels
as fallbacks.

The action also exposes immutable planning metadata for callers that need to
materialize a train-owned source in a container:

- `RAPIDS_RELEASE_CANDIDATE_TRAIN_FILE` is the verified release-train JSON.
- `RELEASE_CANDIDATE_SOURCE_MANIFEST` points to a validated JSON file with
  matching `source_revisions` and `github_repositories` maps. Every revision is
  a full commit SHA.
- `RAPIDS_RELEASE_CANDIDATE_UNIT_VERSIONS` contains the frozen family and
  unit-specific package version maps from `version_contract`.
- `RAPIDS_RELEASE_CANDIDATE_VERSION` is the train's YY.MM release version.

Source repositories that are not represented by the frozen train are not
available through these mappings. Callers must fail closed instead of cloning
mutable branches or using public nightly packages to fill a missing train
input.
