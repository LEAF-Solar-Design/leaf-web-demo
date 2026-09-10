#!/usr/bin/env bash
set -euo pipefail
source "${BASH_SOURCE[0]%/*}/mq-deadline.sh"

PASSED=0
TOTAL=0
check() {
  local name=$1
  shift
  TOTAL=$((TOTAL + 1))
  if "$@"; then
    PASSED=$((PASSED + 1))
  else
    echo "FAIL: $name"
  fi
}

# Record requested sleeps and advance only the helper's clock seam.
# No real sleep, network access, gh, or aws is needed.
SLEPT=0
sleep() {
  SLEPT=$((SLEPT + $1))
  MQ_NOW_OVERRIDE=$((MQ_NOW_OVERRIDE + $1))
}
unset GITHUB_RUN_ID GITHUB_REPOSITORY MQ_DEADLINE_EPOCH
MQ_NOW_OVERRIDE=1000
MQ_DEADLINE_EPOCH=1100
check 'remaining seconds' test "$(mq_deadline_remaining)" -eq 100
MQ_NOW_OVERRIDE=1200
check 'remaining floors at zero' test "$(mq_deadline_remaining)" -eq 0
MQ_NOW_OVERRIDE=1099
check 'not expired before boundary' test "$(mq_deadline_remaining)" -gt 0
if mq_deadline_expired; then STATUS=0; else STATUS=1; fi
check 'expired returns false before boundary' test "$STATUS" -eq 1
MQ_NOW_OVERRIDE=1100
check 'expired at boundary' mq_deadline_expired
MQ_NOW_OVERRIDE=1101
check 'expired after boundary' mq_deadline_expired

MQ_NOW_OVERRIDE=1090
SLEPT=0
if mq_deadline_sleep 60; then STATUS=0; else STATUS=1; fi
check 'sleep capped to ten remaining seconds' test "$SLEPT" -eq 10
check 'capped sleep reports deadline' test "$STATUS" -eq 1
MQ_NOW_OVERRIDE=1000
SLEPT=0
if mq_deadline_sleep 30; then STATUS=0; else STATUS=1; fi
check 'sleep uses full interval with budget left' test "$SLEPT" -eq 30
check 'sleep reports remaining budget' test "$STATUS" -eq 0
MQ_NOW_OVERRIDE=1100
SLEPT=0
if mq_deadline_sleep 30; then STATUS=0; else STATUS=1; fi
check 'expired sleep does not sleep' test "$SLEPT" -eq 0
check 'expired sleep returns one' test "$STATUS" -eq 1

unset MQ_DEADLINE_EPOCH
MQ_NOW_OVERRIDE=2000
MQ_GROUP_BUDGET_MINUTES=60
MQ_DEADLINE_RESERVE_MINUTES=6
mq_deadline_init
check 'missing run id uses current time basis' test "$MQ_DEADLINE_EPOCH" -eq 5240
check 'fallback deadline is in the future' test "$MQ_DEADLINE_EPOCH" -gt "$MQ_NOW_OVERRIDE"
# A valid run created 100 seconds ago would have this earlier deadline.
check 'fallback is no earlier than valid creation basis' test "$MQ_DEADLINE_EPOCH" -ge "$((1900 + 54 * 60))"

for VALUES in '60 60' '60 61' 'invalid 6' '60 invalid' '0 6' '60 0'; do
  read -r MQ_GROUP_BUDGET_MINUTES MQ_DEADLINE_RESERVE_MINUTES <<< "$VALUES"
  unset MQ_DEADLINE_EPOCH
  mq_deadline_init
  check "invalid inputs ($VALUES) restore budget" test "$MQ_GROUP_BUDGET_MINUTES" -eq 60
  check "invalid inputs ($VALUES) restore reserve" test "$MQ_DEADLINE_RESERVE_MINUTES" -eq 6
  check "invalid inputs ($VALUES) use default deadline" test "$MQ_DEADLINE_EPOCH" -eq 5240
done

MQ_DEADLINE_EPOCH=5000
MQ_NOW_OVERRIDE=3000
mq_deadline_init
check 'init preserves existing numeric deadline' test "$MQ_DEADLINE_EPOCH" -eq 5000
MQ_NOW_OVERRIDE=5000
REPORT=$(mq_deadline_report mq-prewarm 'the relay receipt example')
check 'report names job and subject' test "${REPORT#*mq-prewarm:}" != "$REPORT"
check 'report includes elapsed time' test "${REPORT#*after 3240s waiting for the relay receipt example}" != "$REPORT"
check 'report includes budget and reserve' test "${REPORT#*budget 60, reserve 6}" != "$REPORT"

# Case 38: include workflow YAML parsing in the single verification command.
check 'workflow YAML parses (python and PyYAML required)' python -c 'import sys, yaml; yaml.safe_load(open(sys.argv[1], encoding="utf-8"))' "${BASH_SOURCE[0]%/*}/../workflows/merge-queue.yml"

if (( PASSED != TOTAL )); then
  echo "FAIL: $PASSED/$TOTAL passed"
  exit 1
fi
echo "PASS: $PASSED/$TOTAL"
