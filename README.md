# shared-actions

Contains all of the shared composite actions used by RAPIDS. Several of these actions,
especially the telemetry actions, use a pattern that we refer to as "dispatch actions."
The general idea of a dispatch action is to make it easier to depend on other actions
at a specific revision, and also to simplify using files beyond a given action .yml file.

## Release catalog companions

The release catalog action creates a companion archive for each build job. See
the [release catalog documentation](release-catalog/README.md) for its
configuration and archive contract.

The checked-in [cuVS Java companion example](release-catalog/examples/cuvs-java)
shows the complete output for one primary JAR:

- [`release-catalog-entries.json`](release-catalog/examples/cuvs-java/release-catalog-entries.json)
  associates the artifact with its package identity and evidence.
- [SLSA/in-toto provenance](release-catalog/examples/cuvs-java/release-evidence/cuvs-java-26.08.0.jar.fb8ce05502991565de98e3e21d9ab98151c1cd1715b14a3f7c349cba300cb2b9.provenance.json)
  identifies the source, workflow, invocation, and exact artifact digest.
- The SBOM is provided in both [SPDX
  2.3](release-catalog/examples/cuvs-java/release-evidence/cuvs-java-26.08.0.jar.fb8ce05502991565de98e3e21d9ab98151c1cd1715b14a3f7c349cba300cb2b9.sbom.spdx.json)
  and [CycloneDX
  1.6](release-catalog/examples/cuvs-java/release-evidence/cuvs-java-26.08.0.jar.fb8ce05502991565de98e3e21d9ab98151c1cd1715b14a3f7c349cba300cb2b9.sbom.cdx.json)
  formats so consumers can choose the serialization their tooling supports.

The [example companion notes](release-catalog/examples/README.md) explain how
the catalog, both SBOMs, provenance, and separately uploaded primary artifact
are bound together by the artifact SHA-256.

A dispatch action is one that:
* clones the shared-actions repository (repo/ref changeable using env vars)
* runs (dispatches to) another action within the clone, using a relative path

## Example dispatch action

```yaml
name: 'dispatch-example-action'
description: |
  The purpose of this wrapper is to keep it easy for external consumers to switch branches of
  the shared-actions repo when they are changing something about shared-actions and need to test it
  in their pipelines.

runs:
  using: 'composite'
  steps:
    - name: Clone shared-actions repo
      uses: actions/checkout@v4
      with:
        repository: ${{ env.SHARED_ACTIONS_REPO }}
        ref: ${{ env.SHARED_ACTIONS_REF }}
        path: ./shared-actions
    - name: Run local implementation action
      uses: ./shared-actions/impls/example-action
```

In this action, the "implementation action" is the
`./shared-actions/impls/example-action`.  You can have inputs in your
dispatch actions. You would just pass them through to the implementation action.
Environment variables do carry through from the parent workflow through the
dispatch action, and then into the implemetation action. In most cases, it is simpler
(though less explicit) to set environment variables instead of plumbing inputs
through each action.

## Implementation action

These are similar to dispatch actions, except that they should not clone
shared-actions. They can depend on other actions from the shared-actions
repository using the `./shared-actions` relative path.

```yaml
name: 'example-action'
description: |
  An example of calling a python script in an action. Both the action
  and the python file are part of the shared-actions repo.

runs:
  using: 'composite'
  steps:
    - name: Run local action
      uses: ./shared-actions/impls/another-action
    - name: Run local script file
      run: python -c "./shared-actions/impls/hello.py"
      shell: bash
```

## Example calling workflow

The key detail here is that the presence of the SHARED_ACTIONS_REPO and/or
SHARED_ACTIONS_REF environment variables is what changes the shared-actions
dispatch. The `uses` line should not change.

```yaml
env:
  # Change these in PRs
  SHARED_ACTIONS_REPO: some-fork/shared-actions
  SHARED_ACTIONS_REF: some-custom-branch

jobs:
  actions-user:
    runs-on: ubuntu-latest
    steps:
      - name: Call dispatch example
        # DO NOT change the branch here (@main) in PRs
        uses: rapidsai/shared-actions/dispatch-example-action@main
```

This works because the environment variables get passed into the shared action. They are then
used by the `actions/checkout` action, taking priority over the default values.

## Calling in child shared workflows

Shared workflows complicate matters because environment variables do not get
passed through. If you set the `SHARED_ACTIONS_REPO` and/or `SHARED_ACTIONS_REF`
variables in the top-level parent workflow, they will not take effect in any
dispatch actions that you may call in child workflows. You can pass them as inputs
to child shared workflows, but that ends up being very verbose.

To carry this information into child workflows, we use a scheme that writes a
file with environment variables, uploads this file as an artifact, then downloads
and loads the file at the start of the child workflow.

The general scheme is:

### Top-level workflow
```yaml
jobs:
  setup-env-vars:
    runs-on: ubuntu-latest
    steps:
        # implicitly picks up env vars for SHARED_ACTIONS_REPO and _REF
        - uses: rapidsai/shared-actions/telemetry-dispatch-stash-base-env@main

  <rest of jobs>

  summarize-telemetry:
    needs: <all other jobs, or just pr-builder>
    # private networks will affect your choice here. If your tempo server or
    # forwarder/collector is only accessible on some node types, then use one of
    # those instances here
    runs-on: <node>
    steps:
      - uses: rapidsai/shared-actions/telemetry-dispatch-summarize@main
```

### Child workflows
```yaml
jobs:
  tests:
    strategy:
      matrix: ${{ fromJSON(needs.compute-matrix.outputs.MATRIX) }}
    runs-on: "linux-${{ matrix.ARCH }}-gpu-${{ matrix.GPU }}-${{ matrix.DRIVER }}-1"
    steps:
      - name: Telemetry setup
        uses: rapidsai/shared-actions/telemetry-dispatch-setup@main
        continue-on-error: true
        extra_attributes: "rapids.cuda=${{ matrix.CUDA_VER }},rapids.py=${{ matrix.PY_VER }}"

      <other steps, as usual>
```

Behind the scenes, the implementation actions are:
* ./telemetry-impls/stash-base-env-vars: storing base environment variables (including setting default values):
* ./telemetry-impls/load-then-clone: Downloads base env var file, loads it, then
  clones shared-actions according to env vars that were just loaded
* ./telemetry-impls/summarize: Runs Python script to parse GitHub logs and send OpenTelemetry spans to endpoint
