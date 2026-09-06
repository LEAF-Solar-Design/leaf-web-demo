#!/usr/bin/env bash
set -euo pipefail

project=leaf-mq-leaf-web-demo
mode=${1:-apply}
if [[ $# -gt 1 || ! $mode =~ ^(apply|--dry-run|--delete)$ ]]; then
  echo 'Usage: mq-codebuild-project.sh [--dry-run|--delete]' >&2
  exit 2
fi

# Fleet native template supplied by the planner. Only the loader path differs.
project_json='{
  "name": "leaf-mq-leaf-web-demo",
  "source": {
    "type": "GITHUB",
    "location": "https://github.com/LEAF-Solar-Design/leaf-web-demo.git",
    "gitCloneDepth": 0,
    "reportBuildStatus": true,
    "buildspec": "version: 0.2\nenv:\n  shell: bash\nphases:\n  build:\n    commands:\n      - bash .codebuild/mq.sh\n"
  },
  "artifacts": {"type": "NO_ARTIFACTS"},
  "environment": {
    "type": "LINUX_CONTAINER",
    "computeType": "BUILD_GENERAL1_LARGE",
    "image": "aws/codebuild/standard:7.0",
    "environmentVariables": [
      {"name": "GH_TOKEN", "type": "SECRETS_MANAGER", "value": "leaf-github-runner-pat"}
    ]
  },
  "serviceRole": "arn:aws:iam::807034087062:role/leaf-gha-runner-codebuild",
  "timeoutInMinutes": 40,
  "logsConfig": {"cloudWatchLogs": {"status": "ENABLED", "groupName": "/codebuild/leaf-mq-leaf-web-demo"}}
}'
webhook_json='{
  "projectName": "leaf-mq-leaf-web-demo",
  "filterGroups": [[
    {"type": "EVENT", "pattern": "PUSH"},
    {"type": "HEAD_REF", "pattern": "^refs/heads/gh-readonly-queue/main/"}
  ]]
}'

if [[ $mode == --dry-run ]]; then
  printf '{"project": %s, "webhook": %s}\n' "$project_json" "$webhook_json"
  exit 0
fi

: "${AWS_PROFILE:?set AWS_PROFILE to the named operator profile}"
aws_cli() { aws --profile "$AWS_PROFILE" --region us-east-1 --no-cli-pager "$@"; }
identity=$(aws_cli sts get-caller-identity --query Arn --output text)
case "$identity" in
  arn:aws:sts::807034087062:assumed-role/*|arn:aws:iam::807034087062:user/*) ;;
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
  aws_cli codebuild create-project --cli-input-json "$project_json" --query project.arn --output text
else
  aws_cli codebuild update-project --cli-input-json "$project_json" --query project.arn --output text
fi
hook=$(aws_cli codebuild batch-get-projects --names "$project" --query 'projects[0].webhook.url' --output text)
if [[ $hook == None || -z $hook ]]; then
  aws_cli codebuild create-webhook --cli-input-json "$webhook_json" --query webhook.url --output text
else
  aws_cli codebuild update-webhook --cli-input-json "$webhook_json" --query webhook.url --output text
fi
