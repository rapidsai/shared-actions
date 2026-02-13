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
      - name: Check if nightly CI is passing
        uses: rapidsai/shared-actions/check_nightly_success/dispatch@main
        with:
          repo: ${{ github.repository }}
          target_branch: ${{ github.base_ref }}
          workflow_id: 'test.yaml'
          max_days_without_success: 7
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

> Found 4 successful runs of workflow 'test.yaml' on branch 'main' in the previous 7 days.
The most recent successful run of workflow 'test.yaml' on branch 'main' was '2026-02-13 13:40:18+00:00', which is within the last 7 days. View logs:
 - https://github.com/rapidsai/cudf/actions/runs/21978265026

 To see it fail, try on a repo that doesn't have that workflow.

```shell
GH_TOKEN=$(gh auth token) \
python ./check-nightly-success/check.py \
  --repo 'rapidsai/build-planniing' \
  --branch 'main' \
  --workflow-id 'test.yaml' \
  --max-days-without-success 7
```

That'll return exit code `1` and output similar to this:

> RuntimeError: Failed to fetch https://api.github.com/repos/rapidsai/build-planniing/actions/workflows/test.yaml/runs after 5 attempts with the following errors:
        404 Client Error: Not Found for url: https://api.github.com/repos/rapidsai/build-planniing/actions/workflows/test.yaml/runs?branch=main&status=success&per_page=100&created=%3E%3D2026-02-05
        404 Client Error: Not Found for url: https://api.github.com/repos/rapidsai/build-planniing/actions/workflows/test.yaml/runs?branch=main&status=success&per_page=100&created=%3E%3D2026-02-05
        404 Client Error: Not Found for url: https://api.github.com/repos/rapidsai/build-planniing/actions/workflows/test.yaml/runs?branch=main&status=success&per_page=100&created=%3E%3D2026-02-05
        404 Client Error: Not Found for url: https://api.github.com/repos/rapidsai/build-planniing/actions/workflows/test.yaml/runs?branch=main&status=success&per_page=100&created=%3E%3D2026-02-05
        404 Client Error: Not Found for url: https://api.github.com/repos/rapidsai/build-planniing/actions/workflows/test.yaml/runs?branch=main&status=success&per_page=100&created=%3E%3D2026-02-05
