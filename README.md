# shared-actions

Contains all of the shared composite actions used by RAPIDS. Several of these actions
use a pattern that we refer to as "dispatch actions."
The general idea of a dispatch action is to make it easier to depend on other actions
at a specific revision, and also to simplify using files beyond a given action .yml file.

A dispatch action is one that:
* clones the shared-actions repository (repo/ref changeable using env vars)
* runs (dispatches to) another action within the clone, using a relative path

The checkout is the important part of the pattern. It gives every subsequent
step a stable `./shared-actions` path and lets a caller test changes by setting
`SHARED_ACTIONS_REPO` and `SHARED_ACTIONS_REF` without changing the caller's
`uses:` line. When those variables are absent, dispatch actions normally use
`rapidsai/shared-actions` at `main`.

The public action and its implementation may use either of two layouts:

* A small dispatch action can invoke a separate implementation action under the
  checked-out `./shared-actions` directory. This is useful when several public
  actions share one implementation.
* A self-dispatching action can keep its `action.yml`, scripts, schemas, and
  documentation in one folder. After checkout, its shell steps invoke the files
  in the corresponding `./shared-actions/<action-name>` folder. This is simpler
  when the implementation has no independent callers.

In both layouts, actions must use files from the checked-out `./shared-actions`
tree rather than from the revision that initially loaded the public
`action.yml`. Otherwise `SHARED_ACTIONS_REPO` and `SHARED_ACTIONS_REF` would
select only the wrapper while the implementation continued to come from a
different revision.

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
        repository: ${{ env.SHARED_ACTIONS_REPO || 'rapidsai/shared-actions' }}
        ref: ${{ env.SHARED_ACTIONS_REF || 'main' }}
        path: ./shared-actions
        persist-credentials: false
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

Reusable workflows do not inherit environment variables from their callers.
Set `SHARED_ACTIONS_REPO` and `SHARED_ACTIONS_REF` in the workflow that invokes
the dispatch action when testing an alternative implementation.
