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

To see it fail, try on a repo that doesn't have that workflow.

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
