# shared-actions

Contains all of the shared composite actions used by RAPIDS.

Actions that refer to each other assume that they have been checked out to the
./shared-actions folder. This assumption is what allow code reuse between
actions. Your general usage pattern for using these actions in other repos
should be:

```
...

env:
  SHARED_ACTIONS_REF: 'main'

jobs:
  actions-user:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout actions
        uses: actions/checkout@v4
        with:
            repository: rapidsai/shared-actions
            ref: ${{env.SHARED_ACTIONS_REF}}
            path: ./shared-actions
```

Instead of something like:

```
      - name: Telemetry setup
        id: telemetry-setup
        uses: rapidsai/shared-actions/telemetry-traceparent@add-telemetry
```

This latter syntax is difficult because the branch info does not cascade
recursively into any checkouts that might be done in an action, and also because
this syntax does not support actions calling other actions with relative paths.