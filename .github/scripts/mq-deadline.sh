#!/usr/bin/env bash
# Shared deadline for the merge queue's serial polling steps. Source this file.

mq_now() {
  if [[ ${MQ_NOW_OVERRIDE:-} =~ ^[0-9]+$ ]] && (( 10#${MQ_NOW_OVERRIDE} > 0 )); then
    echo "$((10#$MQ_NOW_OVERRIDE))"
  else
    date -u +%s
  fi
}

mq_deadline_init() {
  if [[ ${MQ_DEADLINE_EPOCH:-} =~ ^[0-9]+$ ]]; then
    export MQ_DEADLINE_EPOCH
    return 0
  fi

  local budget=${MQ_GROUP_BUDGET_MINUTES-60}
  local reserve=${MQ_DEADLINE_RESERVE_MINUTES-6}
  if ! [[ $budget =~ ^[0-9]+$ && $reserve =~ ^[0-9]+$ ]] ||
      ! (( 10#$budget > 0 && 10#$reserve > 0 && 10#$reserve < 10#$budget )); then
    echo "::warning::Invalid merge queue budget or reserve; using budget 60 and reserve 6."
    budget=60
    reserve=6
  fi
  export MQ_GROUP_BUDGET_MINUTES=$((10#$budget))
  export MQ_DEADLINE_RESERVE_MINUTES=$((10#$reserve))

  local created basis
  if [[ -n ${GITHUB_REPOSITORY:-} && -n ${GITHUB_RUN_ID:-} ]] &&
      created=$(timeout 30 gh api "repos/$GITHUB_REPOSITORY/actions/runs/$GITHUB_RUN_ID" --jq .created_at 2>/dev/null) &&
      [[ -n $created ]] &&
      basis=$(date -u -d "$created" +%s 2>/dev/null) &&
      [[ $basis =~ ^[0-9]+$ ]]; then
    echo "::notice::Merge queue deadline basis: workflow run creation time ($created)."
  else
    basis=$(mq_now)
    echo "::notice::Merge queue deadline basis: current time ($basis); workflow run creation time unavailable."
  fi
  export MQ_DEADLINE_EPOCH=$((10#$basis + (MQ_GROUP_BUDGET_MINUTES - MQ_DEADLINE_RESERVE_MINUTES) * 60))
}

mq_deadline_remaining() {
  local now remaining deadline=${MQ_DEADLINE_EPOCH:-0}
  now=$(mq_now)
  remaining=$((10#$deadline - now))
  if (( remaining < 0 )); then remaining=0; fi
  echo "$remaining"
}

mq_deadline_expired() {
  [[ $(mq_deadline_remaining) == 0 ]]
}

mq_deadline_wait() {
  local interval=${1:-0} remaining
  remaining=$(mq_deadline_remaining)
  (( remaining > 0 )) || return 1
  [[ $interval =~ ^[0-9]+$ ]] || return 1
  interval=$((10#$interval))
  if (( interval > remaining )); then interval=$remaining; fi
  if (( interval > 0 )); then sleep "$interval" || return 1; fi
  ! mq_deadline_expired
}

mq_deadline_report() {
  local now elapsed basis
  local budget=${MQ_GROUP_BUDGET_MINUTES:-60} reserve=${MQ_DEADLINE_RESERVE_MINUTES:-6}
  now=$(mq_now)
  basis=$((${MQ_DEADLINE_EPOCH:-0} - (budget - reserve) * 60))
  elapsed=$((now - basis))
  echo "::error::${1:-merge queue}: deadline reached after ${elapsed}s waiting for ${2:-receipt}; the merge queue ejects at $budget min (budget $budget, reserve $reserve). Failing now so this reports as a check instead of a silent ejection."
}
