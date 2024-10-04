const { createHash } = require("crypto");

module.exports = async ({github, context, process}) => {
    const runAttempt = parseInt(process.env.GITHUB_RUN_ATTEMPT, 10)

    const trace_id = createHash("sha256")
        .update(context.repo.owner)
        .update(context.repo.repo)
        .update(String(context.runId))
        .update(String(runAttempt))
        .digest("hex");

    const job_info = await github.rest.actions.listJobsForWorkflowRunAttempt({
        attempt_number: runAttempt,
        owner: context.repo.owner,
        repo: context.repo.repo,
        run_id: context.runId
    });

    // We know what the run ID is, but we don't know which specific job we're being run from.
    // https://github.com/orgs/community/discussions/8945
    const this_job = job_info.data.jobs.find((job) => {
        return job.runner_name === process.env.RUNNER_NAME && job.run_attempt === runAttempt;
    });

    console.log(this_job)

    const span_id = createHash("sha256")
        .update(context.repo.owner)
        .update(context.repo.repo)
        .update(String(context.runId))
        .update(String(runAttempt))
        .update(this_job.name)
        .digest("hex");

    return {
        "job_name": this_job.name,
        "trace_id": trace_id,
        "span_id": span_id,
        "job_info_json": this_job,
    }
};