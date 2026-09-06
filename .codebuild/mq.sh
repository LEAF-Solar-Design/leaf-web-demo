#!/usr/bin/env bash
set -euo pipefail

if [[ ${CODEBUILD_WEBHOOK_EVENT:-} =~ ^PULL_REQUEST_(CREATED|UPDATED|REOPENED)$ ]]; then
  if [[ ${CODEBUILD_WEBHOOK_BASE_REF:-} != refs/heads/main ]]; then
    echo 'Not main: pull request base is outside main'
    exit 0
  fi
  exec python3 "${MQ_REVIEW_PY:-scripts/ci/mq_review.py}" --deferred --head-sha "${CODEBUILD_RESOLVED_SOURCE_VERSION:?missing built commit}"
fi

if [[ ! ${CODEBUILD_WEBHOOK_HEAD_REF:-} =~ ^refs/heads/gh-readonly-queue/main/ ]]; then
  echo 'Not a group: webhook ref is outside the main merge queue'
  exit 0
fi

exec python3 "${MQ_REVIEW_PY:-scripts/ci/mq_review.py}" --head-sha "${CODEBUILD_RESOLVED_SOURCE_VERSION:?missing built commit}"
