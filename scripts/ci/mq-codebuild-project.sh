#!/usr/bin/env bash
set -euo pipefail

project=leaf-mq-leaf-web-demo
role_name=leaf-mq-codebuild
policy_name=leaf-mq-codebuild-scoped
account=807034087062
region=us-east-1
log_group=/codebuild/leaf-mq-leaf-web-demo
secret_name=leaf-github-runner-pat
connection_display_name=leaf-gha-runners
mode=${1:-apply}
if [[ $# -gt 1 || ! $mode =~ ^(apply|--dry-run|--delete)$ ]]; then
  echo 'Usage: mq-codebuild-project.sh [--dry-run|--delete]' >&2
  exit 2
fi

# STABLE LOADER: the checked-out group commit is never executed. Both scripts
# are pulled from origin/main via `git show` into /tmp before the run.
buildspec_text=$(cat <<'BUILDSPEC'
version: 0.2
env:
  shell: bash
phases:
  build:
    commands:
      - REF=$(git rev-parse --verify -q origin/main || git rev-parse --verify -q main)
      - test -n "$REF" || { echo "FATAL: cannot resolve origin/main or main" >&2; exit 1; }
      - git cat-file -e "$REF:.codebuild/mq.sh" || { echo "FATAL: .codebuild/mq.sh missing at $REF" >&2; exit 1; }
      - git cat-file -e "$REF:scripts/ci/mq_review.py" || { echo "FATAL: scripts/ci/mq_review.py missing at $REF" >&2; exit 1; }
      - git show "$REF:.codebuild/mq.sh" > /tmp/mq.sh
      - git show "$REF:scripts/ci/mq_review.py" > /tmp/mq_review.py
      - SHORT=$(git rev-parse --short "$REF")
      - echo "Loading mq-review from $REF ($SHORT)"
      - MQ_REVIEW_PY=/tmp/mq_review.py bash /tmp/mq.sh
BUILDSPEC
)

# Renders one JSON document to stdout. $1 selects: trust|policy|project|webhook|dry-run.
# All secret/connection material is passed in by the caller; this function makes no AWS calls.
render_doc() {
  MSYS_NO_PATHCONV=1 MQ_DOC="$1" MQ_ROLE="$role_name" MQ_POLICY_NAME="$policy_name" MQ_ACCOUNT="$account" \
  MQ_REGION="$region" MQ_LOG_GROUP="$log_group" MQ_PROJECT="$project" \
  MQ_SECRET_ARN="${secret_arn:-}" MQ_CONN_ARN_1="${conn_arn_1:-}" MQ_CONN_ARN_2="${conn_arn_2:-}" \
  MQ_BUILDSPEC="$buildspec_text" MQ_ROLE_ARN="arn:aws:iam::${account}:role/${role_name}" \
  python3 - <<'PY'
import json
import os

doc = os.environ["MQ_DOC"]
account = os.environ["MQ_ACCOUNT"]
region = os.environ["MQ_REGION"]
role = os.environ["MQ_ROLE"]
policy_name = os.environ["MQ_POLICY_NAME"]
log_group = os.environ["MQ_LOG_GROUP"]
project = os.environ["MQ_PROJECT"]
secret_arn = os.environ["MQ_SECRET_ARN"]
conn_arn_1 = os.environ["MQ_CONN_ARN_1"]
conn_arn_2 = os.environ["MQ_CONN_ARN_2"]
buildspec = os.environ["MQ_BUILDSPEC"]
role_arn = os.environ["MQ_ROLE_ARN"]

trust_policy = {
    "Version": "2012-10-17",
    "Statement": [{
        "Effect": "Allow",
        "Principal": {"Service": "codebuild.amazonaws.com"},
        "Action": "sts:AssumeRole",
        "Condition": {"StringEquals": {"aws:SourceAccount": account}},
    }],
}

scoped_policy = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Sid": "MqLogsOwnGroupOnly",
            "Effect": "Allow",
            "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
            "Resource": [
                f"arn:aws:logs:{region}:{account}:log-group:{log_group}",
                f"arn:aws:logs:{region}:{account}:log-group:{log_group}:*",
            ],
        },
        {
            "Sid": "MqSecretReadOnly",
            "Effect": "Allow",
            "Action": "secretsmanager:GetSecretValue",
            "Resource": secret_arn,
        },
        {
            "Sid": "MqConnectionUseOnly",
            "Effect": "Allow",
            "Action": [
                "codeconnections:GetConnection",
                "codeconnections:GetConnectionToken",
                "codeconnections:UseConnection",
                "codestar-connections:GetConnection",
                "codestar-connections:GetConnectionToken",
                "codestar-connections:UseConnection",
            ],
            "Resource": [conn_arn_1, conn_arn_2],
        },
    ],
}

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
        "computeType": "BUILD_GENERAL1_LARGE",
        "image": "aws/codebuild/standard:7.0",
        "environmentVariables": [
            {"name": "GH_TOKEN", "type": "SECRETS_MANAGER", "value": "leaf-github-runner-pat"},
        ],
    },
    "serviceRole": role_arn,
    "timeoutInMinutes": 40,
    "logsConfig": {"cloudWatchLogs": {"status": "ENABLED", "groupName": log_group}},
}

webhook_doc = {
    "projectName": project,
    "filterGroups": [[
        {"type": "EVENT", "pattern": "PUSH"},
        {"type": "HEAD_REF", "pattern": "^refs/heads/gh-readonly-queue/main/"},
    ]],
}

if doc == "trust":
    print(json.dumps(trust_policy))
elif doc == "policy":
    print(json.dumps(scoped_policy))
elif doc == "project":
    print(json.dumps(project_doc))
elif doc == "webhook":
    print(json.dumps(webhook_doc))
elif doc == "dry-run":
    print(json.dumps({
        "role": {
            "name": role,
            "trustPolicy": trust_policy,
            "policyName": policy_name,
            "policy": scoped_policy,
        },
        "project": project_doc,
        "webhook": webhook_doc,
    }))
else:
    raise SystemExit(f"unknown doc {doc}")
PY
}

# Prints the secret's ARN on stdout. In --dry-run mode this makes no AWS call:
# it prints the placeholder and says so on stderr.
secret_arn_value() {
  if [[ $mode == --dry-run ]]; then
    echo 'dry-run: secret ARN resolved at run time via secretsmanager describe-secret' >&2
    printf '%s' '<resolved at run time>'
    return 0
  fi
  local arn
  arn=$(aws_cli secretsmanager describe-secret --secret-id "$secret_name" --query ARN --output text 2>/dev/null) || arn=""
  if [[ -z $arn || $arn == None ]]; then
    echo "FATAL: secret $secret_name not found in Secrets Manager" >&2
    exit 1
  fi
  printf '%s' "$arn"
}

# Prints the connection's two ARN spellings (codeconnections, codestar-connections), one per
# line. In --dry-run mode this makes no AWS call: it prints two placeholders and says so on stderr.
connection_arns_value() {
  if [[ $mode == --dry-run ]]; then
    echo 'dry-run: connection ARNs resolved at run time via codeconnections list-connections' >&2
    printf '%s\n%s\n' '<resolved at run time>' '<resolved at run time>'
    return 0
  fi
  local arn status id
  arn=$(aws_cli codeconnections list-connections \
    --query "Connections[?ConnectionName=='${connection_display_name}'].ConnectionArn | [0]" \
    --output text 2>/dev/null) || arn=""
  status=$(aws_cli codeconnections list-connections \
    --query "Connections[?ConnectionName=='${connection_display_name}'].ConnectionStatus | [0]" \
    --output text 2>/dev/null) || status=""
  if [[ -z $arn || $arn == None ]]; then
    echo "FATAL: connection $connection_display_name not found" >&2
    exit 1
  fi
  if [[ $status != AVAILABLE ]]; then
    echo "FATAL: connection $connection_display_name is not AVAILABLE (status: $status)" >&2
    exit 1
  fi
  id=${arn##*/}
  printf '%s\n%s\n' \
    "arn:aws:codeconnections:${region}:${account}:connection/${id}" \
    "arn:aws:codestar-connections:${region}:${account}:connection/${id}"
}

if [[ $mode == --dry-run ]]; then
  secret_arn=$(secret_arn_value)
  readarray -t conn_arns < <(connection_arns_value)
  conn_arn_1=${conn_arns[0]}
  conn_arn_2=${conn_arns[1]}
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
  aws_cli iam delete-role-policy --role-name "$role_name" --policy-name "$policy_name" 2>/dev/null || true
  aws_cli iam delete-role --role-name "$role_name" 2>/dev/null || true
  exit 0
fi

secret_arn=$(secret_arn_value)
readarray -t conn_arns < <(connection_arns_value)
conn_arn_1=${conn_arns[0]}
conn_arn_2=${conn_arns[1]}

role_created=0
role_arn=$(aws_cli iam get-role --role-name "$role_name" --query 'Role.Arn' --output text 2>/dev/null) || role_arn=""
if [[ -z $role_arn || $role_arn == None ]]; then
  aws_cli iam create-role --role-name "$role_name" \
    --assume-role-policy-document "$(render_doc trust)" >/dev/null
  role_created=1
else
  aws_cli iam update-assume-role-policy --role-name "$role_name" \
    --policy-document "$(render_doc trust)"
fi
aws_cli iam put-role-policy --role-name "$role_name" --policy-name "$policy_name" \
  --policy-document "$(render_doc policy)"

waited=0
max_wait=60
sleep_for=5
while true; do
  if [[ $exists == 0 ]]; then
    output=$(aws_cli codebuild create-project --cli-input-json "$(render_doc project)" --query project.arn --output text 2>&1) && break
  else
    output=$(aws_cli codebuild update-project --cli-input-json "$(render_doc project)" --query project.arn --output text 2>&1) && break
  fi
  if [[ $role_created == 1 && $waited -lt $max_wait && $output == *InvalidInputException* \
        && ( $output == *sts:AssumeRole* || $output == *"is not authorized"* || $output == *"Not authorized"* ) ]]; then
    sleep "$sleep_for"
    waited=$((waited + sleep_for))
    continue
  fi
  echo "$output" >&2
  exit 1
done
echo "$output"

hook=$(aws_cli codebuild batch-get-projects --names "$project" --query 'projects[0].webhook.url' --output text)
if [[ $hook == None || -z $hook ]]; then
  aws_cli codebuild create-webhook --cli-input-json "$(render_doc webhook)" --query webhook.url --output text
else
  aws_cli codebuild update-webhook --cli-input-json "$(render_doc webhook)" --query webhook.url --output text
fi
