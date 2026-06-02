# Trigger Workflow and Wait

GitHub Action for trigger a workflow from another workflow. The action then waits for a response.

## Inputs

| Input Name           | Required | Default | Description                                                                                            |
| -------------------- | -------- | ------- | ------------------------------------------------------------------------------------------------------ |
| `owner`              | True     | N/A     | Owner of the repository where the workflow is contained.                                               |
| `repo`               | True     | N/A     | Repository where the workflow is contained.                                                            |
| `github_token`       | True     | N/A     | GitHub access token with access to the repository. It is recommended you put this token under secrets. |
| `workflow_file_name` | True     | N/A     | File in .github/workflows to run (e.g. 'build.yaml')                                                   |
| `github_user`        | False    | N/A     | GitHub user whose access token is being used to trigger the workflow.                                  |
| `ref`                | False    | main    | git reference (e.g. branch, tag, or commit SHA) for the workflow run.                                  |
| `wait_interval`      | False    | 10      | Delay (in seconds) between checks for result of run.                                                   |
| `client_payload`     | False    | `{}`    | Payload to pass to the workflow, must be a JSON string                                                 |
| `propagate_failure`  | False    | `true`  | Fail current job if downstream job fails.                                                              |
| `trigger_workflow`   | False    | `true`  | Trigger the specified workflow.                                                                        |
| `wait_workflow`      | False    | `true`  | Wait for workflow to finish.                                                                           |
| `summarize`          | False    | `false` | Print downstream job URL and ID to workflow job summary.                                               |

## Example

From https://github.com/rapidsai/workflows

```yaml
- uses: rapidsai/shared-actions/trigger-workflow-and-wait@main
  with:
    owner: rapidsai
    repo: rmm
    github_token: ${{ secrets.WORKFLOW_TOKEN }}
    github_user: GPUtester
    workflow_file_name: build.yaml
    ref: ${{ fromJSON(needs.get-run-info.outputs.obj).branch }}
    wait_interval: 120
    client_payload: ${{ toJSON(fromJSON(needs.get-run-info.outputs.obj).payloads.rmm) }}
    propagate_failure: true
    trigger_workflow: true
    wait_workflow: true
```

## Testing

To test locally:

```shell
INPUT_OWNER="rapidsai" \
INPUT_REPO="rmm" \
INPUT_GITHUB_TOKEN="$(gh auth token)" \
INPUT_GITHUB_USER="GPUtester" \
INPUT_WORKFLOW_FILE_NAME="build.yaml" \
INPUT_REF="main" \
INPUT_WAIT_INTERVAL=10 \
INPUT_CLIENT_PAYLOAD="{
    \"branch\": \"main\",
    \"date\": \"$(date +%Y-%m-%d)\",
    \"sha\": \"$(git ls-remote https://github.com/rapidsai/rmm.git refs/heads/main | cut -f1)\"
}" \
INPUT_PROPAGATE_FAILURE=true \
INPUT_TRIGGER_WORKFLOW=true \
INPUT_WAIT_WORKFLOW=true \
bash entrypoint.sh
```

## History

> [!NOTE]
> This action contains NVIDIA-specific commits on top of the archived project `convictional/trigger-workflow-and-wait` [link](https://github.com/convictional/trigger-workflow-and-wait).
> For details, see https://github.com/rapidsai/workflows/issues/118
