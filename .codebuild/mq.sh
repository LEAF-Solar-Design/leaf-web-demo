#!/usr/bin/env bash
set -euo pipefail

if [[ ! ${CODEBUILD_WEBHOOK_HEAD_REF:-} =~ ^refs/heads/gh-readonly-queue/main/ ]]; then
  echo 'Not a group: webhook ref is outside the main merge queue'
  exit 0
fi

exec python3 scripts/ci/mq_review.py --head-sha "${CODEBUILD_RESOLVED_SOURCE_VERSION:?missing built commit}"
