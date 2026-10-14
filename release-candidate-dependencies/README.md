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

The action defaults `implementation-mode` to `reusable`, which verifies that
the caller is running through the exact frozen `shared-workflows` revision.
Set `implementation-mode: direct` only when a producer workflow invokes this
action itself rather than using the reusable workflow. Direct mode still
verifies the exact frozen `shared-actions` repository and revision, train
membership, and selected source/run bindings; it skips only the
`shared-workflows` caller-revision assertion.

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
- `RAPIDS_RELEASE_CANDIDATE_VERSION` is the train's YY.MM release version;
  `RAPIDS_RELEASE_CANDIDATE_UNIT_VERSION` is the selected unit's exact frozen
  package version when the unit declares one.
- For wheel jobs, the resolver writes a separate CMake source-version override
  from the matching frozen Conda unit version (or the frozen Conda family
  version). This keeps RAPIDS CMake's padded source version distinct from the
  Python wheel's normalized wheel version; project-specific Conda overrides,
  such as rapids-logger's `0.3.0`, remain intact.
- UCXX declares its Conda and wheel package version as `0.53`, while its source
  root version is `0.53.00`. For the `wheel:ucxx` unit only, the resolver
  validates the three frozen metadata symlinks and materializes their contents
  as the exact unit version before the build. The CMake generator separately
  receives `0.53.0`, which satisfies UCXX's three-component CMake parser while
  leaving wheel metadata at the exact frozen `0.53` string.

Source repositories that are not represented by the frozen train are not
available through these mappings. Callers must fail closed instead of cloning
mutable branches or using public nightly packages to fill a missing train
input.
