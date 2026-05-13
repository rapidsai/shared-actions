# Security Policy

`shared-actions` is a repository of composite GitHub Actions shared by the
RAPIDS organization. Workflows in other `rapidsai/*` repos reference these
actions (`uses: rapidsai/shared-actions/<name>@<ref>`) to handle DockerHub
authentication, devcontainer image builds, changed-file detection across
PRs, nightly-build status checks, sccache distribution, and OpenTelemetry
trace dispatch.

Because these actions execute inside CI workflows with whatever secrets
and `GITHUB_TOKEN` permissions the caller passes them, the repository's
security posture is dominated by how callers reference and parameterize
the actions — and by the actions' own handling of secrets, network
operations, and inputs sourced from PR metadata.

## Reporting a Vulnerability

Please report security vulnerabilities privately through one of the channels
below. **Do not open a public GitHub issue, PR, or discussion** for a
suspected vulnerability.

1. **NVIDIA Vulnerability Disclosure Program (preferred)**
   <https://www.nvidia.com/en-us/security/>
   Submit through the NVIDIA PSIRT web form. This is the fastest path to
   triage and tracking.

2. **Email NVIDIA PSIRT**
   psirt@nvidia.com — encrypt sensitive reports with the
   [NVIDIA PSIRT PGP key](https://www.nvidia.com/en-us/security/pgp-key).

3. **GitHub Private Vulnerability Reporting**
   Use the **Security** tab on this repository → *Report a vulnerability*.

Please include, where possible:

- Affected action (e.g. `dockerhub-script`, `build-devcontainer`,
  `changed-files`, a specific `telemetry-impls/*` action)
- Whether the issue is in this repo's action source, in how a caller
  consumes it, or in a third-party action referenced from here
- Reproduction (workflow snippet + inputs + observed behavior)
- Impact assessment (secret leak, code execution in the runner,
  supply-chain weakness, log/telemetry data leak)
- Any relevant CWE / CVE identifiers

NVIDIA PSIRT will acknowledge receipt and coordinate triage, fix
development, and coordinated disclosure. More on NVIDIA's response
process: <https://www.nvidia.com/en-us/security/psirt-policies/>.

## Security Architecture & Context

**Classification:** CI / build-tooling library (composite GitHub Actions).
Distributed as YAML in this repository and consumed by reference from
caller workflows.

**Primary security responsibility:** Provide composite actions that
behave predictably given trusted inputs from a calling workflow,
without amplifying that workflow's trust assumptions — i.e. without
leaking secrets, sourcing code from caller-controlled refs of
unexpected repositories, or interpolating PR-controlled text into
shell commands.

**Components:**

- **`dockerhub-script/`** — generates a short-lived DockerHub JWT from
  `DOCKERHUB_USER` / `DOCKERHUB_TOKEN` inputs, sources a
  caller-supplied script with the JWT in the environment, then
  revokes the JWT via `trap … EXIT`.
- **`build-devcontainer/`** — builds (and optionally pushes) a
  devcontainer image. Uses `docker/setup-qemu-action` and
  `actions/setup-node` pinned to commit SHAs.
- **`changed-files/`** — wraps `step-security/changed-files` and
  `nv-gha-runners/get-pr-info` (both pinned to commit SHAs) to
  determine which file groups in a PR changed.
- **`check_nightly_success/`** — dispatches a sub-action that checks
  whether overnight builds for a given component succeeded.
- **`rapids-github-info/`** — extracts repo/PR metadata from the
  GitHub context.
- **`setup-sccache-dist/`** — installs and configures sccache for the
  distributed-compile cache topology used by RAPIDS builds.
- **`telemetry-dispatch-*` + `telemetry-impls/*`** — OpenTelemetry
  span dispatch. Computes a deterministic traceparent from the GitHub
  runtime, stashes / unstashes base env vars and per-job artifacts
  for tracing, and summarizes spans to OTEL collectors.
- **The "dispatch action" pattern** (documented in `README.md`). Many
  actions in this repo follow a two-step shape: a thin top-level
  action checks out `shared-actions` itself and then `uses:
  ./shared-actions/<sub>` to run the real implementation. This is
  controlled by environment variables `SHARED_ACTIONS_REPO` and
  `SHARED_ACTIONS_REF` — see threat #1 below.

**Out of scope for this policy:** vulnerabilities in GitHub Actions
itself, the upstream third-party actions referenced from here
(`actions/setup-node`, `actions/checkout`, `docker/setup-qemu-action`,
`step-security/changed-files`, `nv-gha-runners/get-pr-info`,
`equinix-labs/otel-cli`, the `gha-tools` and `gha-runner-launcher`
projects), or DockerHub. Vulnerabilities in *how this repo consumes
those upstreams* — pinning, secret passing, input interpolation,
default refs — are in scope.

## Threat Model

The threats below trace to specific actions and patterns in this
repository. Several have already been remediated through the
[RAPIDS Security Audit](https://github.com/orgs/rapidsai/projects/207).

1. **Dispatch-pattern default ref defeats caller SHA pins.**
   The dispatch actions check out `shared-actions` at
   `${{ env.SHARED_ACTIONS_REPO || 'rapidsai/shared-actions' }}` and
   `${{ env.SHARED_ACTIONS_REF || 'main' }}`. When a caller uses one
   of these actions with a pinned SHA (e.g. `uses:
   rapidsai/shared-actions/telemetry-dispatch-setup@<sha>`), the
   *inner* clone still defaults to `main` unless the caller also
   sets `SHARED_ACTIONS_REF`. The integrity guarantee of the
   caller's outer pin therefore does not extend to the dispatched
   sub-action, which is read from a mutable ref of this repo. A
   compromise of `main` flows immediately into every dispatching
   call site. Additionally, callers that allow `SHARED_ACTIONS_REPO`
   to come from a PR-controlled input can be redirected to an
   attacker fork.

2. **`dockerhub-script` exposes the DockerHub JWT to caller-supplied
   code.**
   The action authenticates against DockerHub and sources a
   caller-provided shell script with the JWT in env. This is the
   action's documented purpose — but it means any caller who allows
   the `script` input to be influenced by PR data, or who passes a
   script whose contents are themselves PR-controlled, hands the
   JWT to arbitrary code. The JWT scope is bounded by what the
   credentials grant, not by the action.

3. **GitHub Actions template injection in `${{ }}` interpolation.**
   Composite actions in this repo historically interpolated `${{ ... }}`
   into shell `run:` blocks at evaluation time, including values
   derivable from PR metadata. The audit remediated the specific
   instances; the risk class recurs on new action contributions and
   on changes that route untrusted strings (PR titles, branch
   names, author logins) into shell context.

4. **Mutable refs to third-party actions.**
   Actions referenced by tag rather than commit SHA permit upstream
   maintainers to retroactively change behavior in any caller. This
   repo's current actions pin upstreams to SHAs (e.g.
   `step-security/changed-files@2e07db73…`,
   `actions/setup-node@48b55a01…`), but the broader RAPIDS audit
   found this pattern recurring across the org. New action
   contributions must keep SHA pinning intact.

5. **`secrets: inherit` blast radius for callers.**
   Workflows that call this repo's actions with `secrets: inherit`
   hand every repository secret to whatever the action does or
   sub-dispatches. The audit moved RAPIDS workflows toward explicit
   secret passing; callers should continue that practice when
   integrating shared-actions.

6. **Token / secret echo in CI logs.**
   The audit closed an issue around tokens reaching CI logs and
   process listings across RAPIDS workflows. shared-actions uses
   `::add-mask::` for the DockerHub JWT — preserving that masking,
   and never placing secrets on command lines (visible in `ps`), is
   ongoing.

7. **Default `GITHUB_TOKEN` permissions.**
   Workflows without an explicit top-level `permissions:` block
   receive a broader default token scope than most actions need.
   Some of this repo's actions write status comments / push images
   / dispatch follow-on workflows; callers should grant only the
   per-job permissions those actions require, not the default.

8. **Telemetry data leak.**
   The OpenTelemetry dispatch actions stash environment variables
   and job artifacts into the traced spans (`OTEL_RESOURCE_ATTRIBUTES`,
   `git.repository=${GITHUB_REPOSITORY}`, etc.). Callers should
   verify that no secret-shaped values reach `OTEL_RESOURCE_ATTRIBUTES`
   or span attributes, since OTEL collectors are typically separate
   trust domains from CI runners.

9. **`actions/checkout` token persistence.**
   `actions/checkout` defaults to persisting `GITHUB_TOKEN` in
   `.git/config`. Any subsequent step in a job that uses checkout
   without `persist-credentials: false` shares the token through
   git operations. The audit remediated specific occurrences; new
   checkouts in this repo should set the flag where appropriate.

## Critical Security Assumptions

The following are assumed of caller workflows and the runners they
execute on. These are load-bearing — violating them turns documented
behavior into a vulnerability.

- **Caller workflows control what reaches action inputs.**
  Composite actions assume their inputs come from workflow-controlled
  sources. A caller that lets PR metadata (titles, branch names,
  comment bodies, fork-supplied workflow inputs) reach
  `inputs.script` (`dockerhub-script`), `SHARED_ACTIONS_REPO` /
  `SHARED_ACTIONS_REF` (dispatch pattern), or other input slots
  hands those inputs trust they cannot uphold.

- **Callers pin the outer action *and* the dispatched ref.**
  For dispatch actions, pinning only `uses: rapidsai/shared-actions/<name>@<sha>`
  is insufficient. Set `SHARED_ACTIONS_REF` to the matching SHA (or
  another immutable ref) at the job level, otherwise the inner
  clone reads from `main`.

- **Secrets stay in `env:`, never on the command line.**
  GitHub Actions masks env-passed secrets in logs and provides the
  masking primitive (`::add-mask::`) used by `dockerhub-script`.
  Tokens placed on command lines are visible in `ps` and may appear
  in error traces.

- **Caller workflows declare minimal `permissions:`.**
  GitHub's default `GITHUB_TOKEN` permissions are broader than most
  jobs need. Workflows that use shared-actions should declare a
  minimal top-level `permissions:` block and only grant per-job
  elevations where required.

- **Reusable-workflow secret passing is explicit.**
  Callers should pass only the secrets a downstream workflow or
  action needs — not `secrets: inherit`.

- **DockerHub credentials are scoped to publishing namespaces.**
  `dockerhub-script` exposes whatever scope the supplied JWT
  represents. Use a token (DockerHub access token) scoped to the
  specific repositories the script needs to push to, not an
  account-level token.

- **Telemetry destinations are trusted endpoints.**
  Operators using the telemetry dispatch actions must trust the
  configured OTEL collector. The actions assume traceparent /
  resource-attribute content is non-sensitive; callers that ever
  set OTEL env vars from secret-shaped data violate that
  assumption.

- **The runner is not actively malicious.**
  shared-actions assumes the runner is a stock GitHub-hosted or
  trusted self-hosted runner. Shared-runner reuse across PR jobs
  from forks violates this assumption and should not be used for
  jobs that consume secrets through these actions.

## Supported Versions

Composite actions follow a rolling-`main` model with tagged releases.
Callers should pin to commit SHAs; release tags are mutable.
Security fixes ship to `main` and the next tag; there is no formal
back-port policy.

## Dependency Security

shared-actions depends on a small set of upstream GitHub Actions
(`actions/checkout`, `actions/setup-node`, `docker/setup-qemu-action`,
`step-security/changed-files`, `nv-gha-runners/get-pr-info`,
`equinix-labs/otel-cli-getter`), on the `gha-tools` shell utilities,
and on the `otel-cli` binary. Upstream CVE-driven updates are
applied as commit-SHA bumps in this repo; high-severity advisories
in any of those projects may trigger an out-of-band update.
