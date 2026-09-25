#!/usr/bin/env bash
set -euo pipefail
# The image comes from terraform leaf-ci-base-image (tag web-<terraform main sha>). These values were
# read from the live project on 2026-09-25 (CI speed program, phase 1b-B). --no-webhook exists for the
# Forge relay cutover, which owns the project's trigger from then on.

project=leaf-ci-leaf-web-demo
image=807034087062.dkr.ecr.us-east-1.amazonaws.com/leaf-ci-base:web-ad9b9375382ab004bf640594c7a6a22f0258f918
fleet_arn=arn:aws:codebuild:us-east-1:807034087062:fleet/leaf-ci-heavy-pilot:f625198b-4219-4066-9edb-f68655cf7b0a
role_name=leaf-gha-runner-codebuild
account=807034087062
region=us-east-1
log_group=/codebuild/leaf-ci-leaf-web-demo
mode=${1:-apply}
if [[ $# -gt 1 || ! $mode =~ ^(apply|--dry-run|--delete|--no-webhook)$ ]]; then
  echo 'Usage: pr-codebuild-project.sh [--dry-run|--delete|--no-webhook]' >&2
  exit 2
fi

# STABLE LOADER: load the CI script from the default branch into /tmp.
buildspec_text=$(cat <<'BUILDSPEC'
version: 0.2
env:
  shell: bash
phases:
  build:
    commands:
      - |
        set -euo pipefail
        REF="$(git rev-parse --verify -q origin/main || git rev-parse --verify -q main || true)"
        if [ -z "$REF" ]; then echo "FATAL: default branch main is not in this clone"; git branch -a | head -20; exit 1; fi
        if ! git cat-file -e "$REF:.codebuild/ci.sh" 2>/dev/null; then echo "FATAL: .codebuild/ci.sh missing on main"; exit 1; fi
        git show "$REF:.codebuild/ci.sh" > /tmp/ci.sh
        echo "loader: running .codebuild/ci.sh from $REF ($(git rev-parse --short "$REF"))"
        bash /tmp/ci.sh
cache:
  paths:
    - '/root/.cache/ms-playwright/**/*'
    - '/root/.npm/**/*'
    - '/root/.cache/pip/**/*'
BUILDSPEC
)

# Renders one JSON document to stdout. $1 selects: project|webhook|dry-run.
# This function makes no AWS calls. The shared role is managed separately.
render_doc() {
  MSYS_NO_PATHCONV=1 MQ_DOC="$1" MQ_LOG_GROUP="$log_group" MQ_PROJECT="$project" \
  MQ_BUILDSPEC="$buildspec_text" MQ_ROLE_ARN="arn:aws:iam::${account}:role/${role_name}" \
  MQ_IMAGE="$image" MQ_FLEET_ARN="$fleet_arn" \
  python3 - <<'PY'
import json
import os

doc = os.environ["MQ_DOC"]
log_group = os.environ["MQ_LOG_GROUP"]
project = os.environ["MQ_PROJECT"]
buildspec = os.environ["MQ_BUILDSPEC"]
role_arn = os.environ["MQ_ROLE_ARN"]
image = os.environ["MQ_IMAGE"]
fleet_arn = os.environ["MQ_FLEET_ARN"]

project_doc = {
    "name": project,
    "source": {
        "type": "GITHUB",
        "location": "https://github.com/LEAF-Solar-Design/leaf-web-demo.git",
        "gitCloneDepth": 0,
        "reportBuildStatus": True,
        "buildspec": buildspec,
    },
    "artifacts": {"type": "NO_ARTIFACTS"},
    "environment": {
        "type": "LINUX_CONTAINER",
        "computeType": "BUILD_GENERAL1_XLARGE",
        "image": image,
        "imagePullCredentialsType": "SERVICE_ROLE",
        "fleet": {"fleetArn": fleet_arn},
    },
    "cache": {"type": "LOCAL", "modes": ["LOCAL_SOURCE_CACHE", "LOCAL_CUSTOM_CACHE"]},
    "serviceRole": role_arn,
    "timeoutInMinutes": 60,
    "logsConfig": {"cloudWatchLogs": {"status": "ENABLED", "groupName": log_group}},
}

webhook_doc = {
    "projectName": project,
    "filterGroups": [
        [
            {"type": "EVENT", "pattern": "PUSH"},
            {"type": "HEAD_REF", "pattern": "^refs/heads/main$"},
        ],
        [
            {"type": "EVENT", "pattern": "PULL_REQUEST_CREATED,PULL_REQUEST_UPDATED,PULL_REQUEST_REOPENED"},
            {"type": "BASE_REF", "pattern": "^refs/heads/main$"},
        ],
        [
            {"type": "EVENT", "pattern": "PUSH"},
            {"type": "HEAD_REF", "pattern": "^refs/heads/gh-readonly-queue/"},
        ],
    ],
}

if doc == "project":
    print(json.dumps(project_doc))
elif doc == "webhook":
    print(json.dumps(webhook_doc))
elif doc == "dry-run":
    print(json.dumps({"project": project_doc, "webhook": webhook_doc}))
else:
    raise SystemExit(f"unknown doc {doc}")
PY
}

if [[ $mode == --dry-run ]]; then
  render_doc dry-run
  exit 0
fi

: "${AWS_PROFILE:?set AWS_PROFILE to the named operator profile}"
aws_cli() { MSYS_NO_PATHCONV=1 aws --profile "$AWS_PROFILE" --region "$region" --no-cli-pager "$@"; }
identity=$(aws_cli sts get-caller-identity --query Arn --output text)
case "$identity" in
  arn:aws:sts::${account}:assumed-role/*|arn:aws:iam::${account}:user/*) ;;
  *) echo 'Refusing unexpected account or root identity' >&2; exit 1 ;;
esac

exists=$(aws_cli codebuild batch-get-projects --names "$project" --query 'length(projects)' --output text)

if [[ $mode == --delete ]]; then
  if [[ $exists != 0 ]]; then
    hook=$(aws_cli codebuild batch-get-projects --names "$project" --query 'projects[0].webhook.url' --output text)
    if [[ $hook != None && -n $hook ]]; then
      aws_cli codebuild delete-webhook --project-name "$project"
    fi
    aws_cli codebuild delete-project --name "$project"
  fi
  exit 0
fi

if [[ $exists == 0 ]]; then
  output=$(aws_cli codebuild create-project --cli-input-json "$(render_doc project)" --query project.arn --output text)
else
  output=$(aws_cli codebuild update-project --cli-input-json "$(render_doc project)" --query project.arn --output text)
fi
echo "$output"

if [[ $mode == --no-webhook ]]; then
  echo 'webhook: skipped (--no-webhook)'
  exit 0
fi

hook=$(aws_cli codebuild batch-get-projects --names "$project" --query 'projects[0].webhook.url' --output text)
if [[ $hook == None || -z $hook ]]; then
  aws_cli codebuild create-webhook --cli-input-json "$(render_doc webhook)" --query webhook.url --output text
else
  aws_cli codebuild update-webhook --cli-input-json "$(render_doc webhook)" --query webhook.url --output text
fi
