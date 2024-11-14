# shared-actions

Contains all of the shared composite actions used by RAPIDS. Several of these actions,
especially the telemetry actions, use a pattern that we refer to as "dispatch actions."
The general idea of a dispatch action is to make it easier to depend on other actions
at a specific revision, and also to simplify using files beyond a given action .yml file.

A dispatch action is one that:
* clones the shared-actions repository (repo/ref changeable using env vars)
* runs (dispatches to) another action within the clone, using a relative path

There can be more complicated arrangements of more actions, but the idea is to
have the local clone of the shared-actions repository be the first step of an action.

Actions that refer to each other assume that they have been checked out to the
./shared-actions folder. This *should* be the root of the GitHub Actions workspace.
This assumption is what allow code reuse between actions.

Actions that use this pattern should include "dispatch" in their folder name, so
that they can be readily distinguished from any actions that are either
standalone or otherwise implementations that assume that the ./shared-actions
folder is already cloned, so that they can use relative paths to reference other
actions and files.

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
        repository: ${{ env.SHARED_ACTIONS_REPO}}
        ref: ${{ env.SHARED_ACTIONS_REF}}
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
  An example of calling a python script in an action

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
        # DO NOT change this in PRs
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
    # forwarder/collector is only accessible on some instances, then use one of
    # those instances here
    runs-on: <node>
    steps:
      - uses: rapidsai/shared-actions/telemetry-dispatch-summarize@main
        # if you use mTLS, this is probably the right place to pass in the certificates
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

      <other steps, as usual>

      - name: Stash metadata for this job (matrix values)
        uses: rapidsai/shared-actions/telemetry-dispatch-stash-job-attributes@main
        continue-on-error: true
        if: always()
        with:
          extra_attributes: "rapids.cuda=${{matrix.CUDA_VER}},rapids.py=${{matrix.PY_VER}},rapids.gpu=${{matrix.GPU}}"
```

Behind the scenes, the implementation actions are:
* ./telemetry-impls/stash-base-env-vars: storing base environment variables (including setting default values):
* ./telemetry-impls/load-then-clone: Downloads base env var file, loads it, then
  clones shared-actions according to env vars that were just loaded
* ./telemetry-impls/summarize: Runs Python script to parse GitHub logs and send OpenTelemetry spans to endpoint
