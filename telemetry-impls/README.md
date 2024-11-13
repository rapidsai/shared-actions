These actions are meant to be called by "telemetry-dispatch-*" scripts, not directly.

Consumers may set SHARED_ACTIONS_REPO and SHARED_ACTIONS_REF to test changes
  in their customized shared-actions repo. In general, you should not add inputs
  to these scripts.  Instead, they should add those inputs to the variables that
  get stashed in telemetry-impls/stash-base-env-vars. One exception to this rule
  is secrets. Secrets shouldn't be written to a file that then gets uploaded,
  and they are not directly usable in shared actions. They must be passed in
  as inputs.

Defaults are sometimes set in these implementation files. For example,
`stash-base-env-vars/action.yml` sets several default values.
