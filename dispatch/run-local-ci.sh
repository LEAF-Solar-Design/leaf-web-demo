#!/usr/bin/env bash
# dispatch/run-local-ci.sh — CI bucket runner for leaf-web-demo.
#
# Contract (fleet CI worker / ci_runnerd): invoked from the repo root as
#     bash dispatch/run-local-ci.sh --only <bucket>
# inside a hermetic sandbox — NO network, host filesystem read-only, only the
# cloned workspace (and /tmp) writable, running as an unprivileged uid.
# Verdict protocol: exactly one line per requested bucket on stdout
#     [run-local-ci] <bucket>: PASS|FAIL|SKIP
# PASS/SKIP count green, FAIL is a red bucket; NO line at all is recorded as an
# infra error by the worker — so an unknown bucket deliberately emits nothing.
#
# Buckets:
#   demo-gate — the presenter-kit whole-mission gate (mirrors the mission YAML
#               success_oracle): the seven node oracles, the vite build with a
#               >=2-JS-chunk assertion, the offline pre-flight (every golden
#               number RECOMPUTED from the real intake), and the authored-tool
#               registry probe. Hermetic: npm ci is impossible offline, so deps
#               come from web/vendor/node_modules-linux-x64.tar.gz (built with
#               `npm ci` from web/package-lock.json on amazonlinux:2023, the
#               fleet worker OS) or an already-present web/node_modules.
#   change-impact: advisory change-impact assessment from the CI helper.
#                  Maps COMPLETE/INCOMPLETE receipts to PASS/FAIL and honours
#                  helper SKIP output or missing receipts as SKIP offline.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

ONLY=""
while [ $# -gt 0 ]; do
  case "$1" in
    --only) ONLY="${2:-}"; shift 2 ;;
    --list) printf '%s\n' demo-gate change-impact; exit 0 ;;
    *) shift ;;
  esac
done

emit() { printf '[run-local-ci] %s: %s\n' "$1" "$2"; }

ensure_deps() {
  if [ -x web/node_modules/.bin/vite ]; then
    echo "deps: using existing web/node_modules"
    return 0
  fi
  if [ "$(uname -s)" = "Linux" ] && [ "$(uname -m)" = "x86_64" ] \
      && ls web/vendor/node_modules-linux-x64.tar.gz* >/dev/null 2>&1; then
    echo "deps: extracting vendored linux-x64 node_modules"
    # cat over the glob supports a split tarball (…tar.gz.part-aa/-ab) as well
    # as the single-file form; both stream into one tar.
    cat web/vendor/node_modules-linux-x64.tar.gz* | tar -xz -C web
    return $?
  fi
  echo "deps: no web/node_modules and no vendored deps usable on $(uname -sm); cannot run hermetically"
  return 1
}

demo_gate() {
  local py=python3
  command -v python3 >/dev/null 2>&1 || py=python

  echo "toolchain: node=$(node --version 2>&1) npm=$(npm --version 2>&1) ${py}=$(${py} --version 2>&1) on $(uname -sm)"
  ensure_deps || return 1

  # npm must not try the network for its own housekeeping during `npm run`.
  export NO_UPDATE_NOTIFIER=1 npm_config_update_notifier=false npm_config_fund=false npm_config_audit=false

  echo "--- oracles (8, all numbers recomputed from the real engine/intake)"
  ( cd web \
    && node scripts/check_author.mjs \
    && node scripts/check_demostate.mjs \
    && node scripts/check_writeloop.mjs \
    && node scripts/check_errors.mjs \
    && node scripts/check_tourscript.mjs \
    && node --test scripts/check_tour_anchors.mjs \
    && node test/check_routes.mjs \
    && node test/check_integration.mjs
  ) || { echo "--- FAILED: node oracles"; return 1; }

  echo "--- vite build (must produce >=2 JS chunks)"
  ( cd web && rm -rf dist && npm run build \
    && [ "$(ls dist/assets/*.js | wc -l)" -ge 2 ]
  ) || { echo "--- FAILED: build / chunk count"; return 1; }

  echo "--- offline pre-flight"
  "$py" scripts/demo-preflight.py --offline || { echo "--- FAILED: preflight"; return 1; }

  echo "--- authored-tool registry probe"
  grep -q delete-marked-panel web/src/mock/registry.json \
    || { echo "--- FAILED: delete-marked-panel missing from registry"; return 1; }

  echo "LEAF_DEMO_MISSION_OK"
  return 0
}

change_impact_bucket() {
  local output="" receipt_verdict="" verdict="SKIP"
  output="$(mktemp /tmp/change-impact.XXXXXX)" || { emit change-impact SKIP; return 0; }
  python scripts/ci/change_impact_job.py --repo . --head HEAD \
    --base-ref "${CHANGE_IMPACT_BASE_REF:-refs/heads/main}" --event manual \
    --receipt-dir /tmp/impact >"$output" 2>&1
  cat "$output"
  printf '\n'
  if [ -f /tmp/impact/receipt.json ] \
      && ! grep -Eq '^change-impact: SKIP|checker not installed|disabled' "$output"; then
    receipt_verdict="$(python -c '
import json
try:
    with open("/tmp/impact/receipt.json") as receipt:
        data = json.loads(receipt.read(1048577))
    print(data.get("verdict", "") if isinstance(data, dict) else "")
except (OSError, ValueError, RecursionError):
    print("")
' 2>/dev/null)"
    case "$receipt_verdict" in
      COMPLETE) verdict="PASS" ;;
      INCOMPLETE) verdict="FAIL" ;;
    esac
  fi
  rm -f "$output"
  emit change-impact "$verdict"
  [ "$verdict" != "FAIL" ]
}

case "${ONLY:-demo-gate}" in
  demo-gate)
    if demo_gate; then emit demo-gate PASS; exit 0; else emit demo-gate FAIL; exit 1; fi
    ;;
  change-impact)
    change_impact_bucket
    exit $?
    ;;
  *)
    # Unknown bucket: say so, emit NO verdict line -> the worker records an
    # infra 'error' rather than a silent green.
    echo "unknown bucket: ${ONLY} (available: demo-gate change-impact)"
    exit 2
    ;;
esac
