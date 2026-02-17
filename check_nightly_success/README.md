# check_nightly_success

Action that can be used to fail CI if a given GitHub Actions workflow hasn't had at least 1 recent succcessful run.

Add it to any GitHub Actions workflow configuration like this:

```yaml
  check-nightly-ci:
    runs-on: ubuntu-latest
    permissions:
      actions: read
      id-token: write
    env:
      GH_TOKEN: ${{ github.token }}
    steps:
      - name: Get PR Info
        id: get-pr-info
        uses: nv-gha-runners/get-pr-info@main
      - name: Check if nightly CI is passing
        uses: rapidsai/shared-actions/check_nightly_success/dispatch@main
        with:
          repo: ${{ github.repository }}
          target-branch: ${{ fromJSON(steps.get-pr-info.outputs.pr-info).base.ref }}
          workflow-id: 'test.yaml'
          max-days-without-success: 7
```

## Testing

The code for the actions is implemented in Python.

### Case 1: Succeed on recent nightly test successes

Try the following locally to test it.

```shell
python -m venv .venv/
source .venv/bin/activate
python -m pip install requests

GH_TOKEN=$(gh auth token) \
python ./check-nightly-success/check.py \
  --repo 'rapidsai/cudf' \
  --branch 'main' \
  --workflow-id 'test.yaml' \
  --max-days-without-success 7
```

If this succeeds, you'll see a `0` exit code and output text similar to the following:

> Found 4 successful runs of workflow 'test.yaml' on branch 'main' in the previous 7 days (most recent: '2026-02-16 06:26:04+00:00'). View logs:
 - https://github.com/rapidsai/cudf/actions/runs/22052428055

### Case 2: Fail when branch has 0 runs (of any status)

The check should fail on a repo without any runs of this workflow:

```shell
GH_TOKEN=$(gh auth token) \
python ./check-nightly-success/check.py \
  --repo 'rapidsai/build-planning' \
  --branch 'main' \
  --workflow-id 'test.yaml' \
  --max-days-without-success 7
```

That'll return exit code `1` and output similar to this:

> RuntimeError: Failed to fetch https://api.github.com/repos/rapidsai/build-planning/actions/workflows/test.yaml/runs after 5 attempts with the following errors:
        404 Client Error: Not Found for url: https://api.github.com/repos/rapidsai/build-planning/actions/workflows/test.yaml/runs?branch=main&status=success&per_page=100&created=%3E%3D2026-02-10
        404 Client Error: Not Found for url: https://api.github.com/repos/rapidsai/build-planning/actions/workflows/test.yaml/runs?branch=main&status=success&per_page=100&created=%3E%3D2026-02-10
        404 Client Error: Not Found for url: https://api.github.com/repos/rapidsai/build-planning/actions/workflows/test.yaml/runs?branch=main&status=success&per_page=100&created=%3E%3D2026-02-10
        404 Client Error: Not Found for url: https://api.github.com/repos/rapidsai/build-planning/actions/workflows/test.yaml/runs?branch=main&status=success&per_page=100&created=%3E%3D2026-02-10
        404 Client Error: Not Found for url: https://api.github.com/repos/rapidsai/build-planning/actions/workflows/test.yaml/runs?branch=main&status=success&per_page=100&created=%3E%3D2026-02-10

### Case 3: Success on new branches with only very-recent runs

Branches with only very-recent runs should be exempted from the check.

```shell
# NOTE: this example requires write access to 'rapidsai/ucxx'
git clone -o upstream https://github.com/rapidsai/ucxx
pushd./ucxx
git checkout -b delete-me
git push upstream delete-me
popd

gh workflow run \
    --repo rapidsai/ucxx \
    --ref delete-me \
    test.yaml \
    -f branch="delete-me" \
    -f date="$(date +%Y-%m-%d)" \
    -f sha="$(git rev-parse HEAD)" \
    -f build_type=nightly

# (MANUAL - go to https://github.com/rapidsai/ucxx/actions/runs/22109183034 and manuall cancel that run)

# run the check
GH_TOKEN=$(gh auth token) \
python ./check-nightly-success/check.py \
  --repo 'rapidsai/ucxx' \
  --branch 'delete-me' \
  --workflow-id 'test.yaml' \
  --max-days-without-success 7
```

That'll exit with code `0` and print something like this:

> The oldest run of workflow 'test.yaml' on branch 'delete-me' was 0 days ago (2026-02-17 17:42:05+00:00).
Because the latest run was less than 'max-days-without-success = 7' days ago, this workflow is exempted from check-nightly-success. The check will start failing if there is not a successful run in the next few days.

### Other testing: pagination

Set `--request-page-size` to `1` to test that pagination is working.

```shell
GH_TOKEN=$(gh auth token) \
python ./check-nightly-success/check.py \
  --repo 'rapidsai/cudf' \
  --branch 'main' \
  --workflow-id 'test.yaml' \
  --max-days-without-success 30 \
  --request-page-size 5
```
