# shared-actions

Contains all of the shared composite actions used by RAPIDS.

Actions that refer to each other assume that they have been checked out to the
./shared-actions folder. This *should* be the root of the GitHub Actions workspace.
This assumption is what allow code reuse between actions.

In general, we should try to never call "implementation actions" here. Instead,
we should prefer to create "dispatch actions" that clone shared-actions from a particular repo
at a particular ref, and then dispatch to an implementation action from that repo.
This adds complexity, but has other advantages:

* simplifies specifying a custom branch for actions for development and testing
* changes all shared-actions calls in a workflow at once, instead of changing each one
* allows reuse of shared-actions within the shared-actions repo. Trying to use these
  without the clone and relative path would not otherwise keep the repo and ref
  consistent, leading to great confusion over why changes aren't being reflected.

## Example dispatch action

```yaml
name: 'Example dispatch action'
description: |
  The purpose of this wrapper is to keep it easy for external consumers to switch branches of
  the shared-actions repo when they are changing something about shared-actions and need to test it
  in their pipelines.

  Inputs here are all assumed to be env vars set outside of this script.
  Set them in your main repo's workflows.

runs:
  using: 'composite'
  steps:
    - name: Clone shared-actions repo
      uses: actions/checkout@v4
      with:
        repository: ${{ env.SHARED_ACTIONS_REPO}}
        ref: ${{ env.SHARED_ACTIONS_REF}}
        path: ./shared-actions
    - name: Stash base env vars
      uses: ./shared-actions/_stash-base-env-vars
```

In this action, the "implementation action" is the
`./shared-actions/_stash-base-env-vars`.  You can have inputs in your
dispatch actions. You would just pass them through to the implementation action.
Environment variables do carry through from the parent workflow through the
dispatch action, into the implemetation action. In most cases, it is simpler
(though less explicit) to set environment variables instead of plumbing inputs
through each action.

Environment variables are hard-coded, not detected. If you want to pass a different
environment variable through, you need to add it to implementation stash action,
like `telemetry-impls/stash-base-env-vars/action.yml`. You do not need to
explicitly specify it on the loading side.

## Implementation action

These are similar to dispatch actions, except that they should not clone
shared-actions. They can depend on other actions from the shared-actions
repository using the `./shared-actions` relative path.

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
      - name: Telemetry setup
        id: telemetry-setup
        # DO NOT change this in PRs
        uses: rapidsai/shared-actions/dispatch-script@main
```
