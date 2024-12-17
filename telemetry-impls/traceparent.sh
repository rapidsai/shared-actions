#!/bin/bash
# This emits a TRACEPARENT, which follows the w3c trace context standard.
# https://www.w3.org/TR/trace-context/
#
# This script can operate for two purposes:
# 1. The top level of a job, whether it is the job at the source repo (e.g. rmm) level, or
#      the matrix job level
# 2. The steps level within a job, which uses both the job name and the step name
#
# The job name must always be provided as the first argument.
# A step name MAY be provided as the second argument. If it is specified, the output corresponds to
#     the step within the context of its job.
#
# This is a shell script instead of an action because we need to call it
# in loops that iterate through job metadata.

JOB_NAME=$1
STEP_NAME=${2:-}

if [ "$JOB_NAME" = "" ]; then
    echo "ERROR: JOB_NAME (first parameter) is empty. This means your trace doesn't identify anything."
    exit 1
fi

SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )

sha="$(echo "${GITHUB_REPOSITORY}+${GITHUB_RUN_ID}+${GITHUB_RUN_ATTEMPT}" | sha256sum | cut -f1 -d' ')"
TRACE_ID="${sha:0:32}"
JOB_SPAN_ID="${TRACE_ID}-${JOB_NAME}"
STEP_SPAN_ID="${JOB_SPAN_ID}-${STEP_NAME}"

# echo "JOB_SPAN_ID pre-hash: \"$JOB_SPAN_ID\"" 1>&2
# echo "STEP_SPAN_ID pre-hash: \"$STEP_SPAN_ID\"" 1>&2

JOB_TRACEPARENT=$(echo -n "${JOB_SPAN_ID}" | sha256sum | cut -f1 -d' ')
STEP_TRACEPARENT=$(echo -n "${STEP_SPAN_ID}" | sha256sum | cut -f1 -d' ')

if [ "${STEP_NAME}" != "" ]; then
    echo "00-${TRACE_ID}-${STEP_TRACEPARENT:0:16}-01"
else
    echo "00-${TRACE_ID}-${JOB_TRACEPARENT:0:16}-01"
fi
