#!/usr/bin/env bash
set -euo pipefail
# Freeze policy before any candidate installation, import, or test collection.
TRUSTED_SHA=""
HEAD_SHA=""
selection_ready=0
selection_bootstrap_reason=trusted_load_failed
selection_force_full_reason=""
selection_mode=full
only_args=()
selection_dir="$(mktemp -d /tmp/leaf-selection.XXXXXXXX)"
chmod 700 "$selection_dir"
if TRUSTED_SHA="$(git --no-replace-objects rev-parse --verify 'refs/remotes/origin/main^{commit}')" \
   && HEAD_SHA="$(git --no-replace-objects rev-parse --verify 'HEAD^{commit}')"; then
  if [[ -n "${LEAF_LOADER_TRUSTED_SHA:-}" && "$TRUSTED_SHA" != "$LEAF_LOADER_TRUSTED_SHA" ]]; then
    selection_bootstrap_reason=loader_sha_mismatch
  elif git --no-replace-objects show "$TRUSTED_SHA:scripts/ci/select_tests.py" > "$selection_dir/select_tests.py" \
    && git --no-replace-objects show "$TRUSTED_SHA:scripts/ci/test-selection-map.json" > "$selection_dir/test-selection-map.json" \
    && git --no-replace-objects show "$TRUSTED_SHA:scripts/run-all-gates.py" > "$selection_dir/run-all-gates.py"; then
    chmod 400 "$selection_dir/select_tests.py" "$selection_dir/test-selection-map.json" "$selection_dir/run-all-gates.py"
    if python -I -B "$selection_dir/run-all-gates.py" --list --catalog-root "$CODEBUILD_SRC_DIR" > "$selection_dir/catalog.json"; then
      trusted_runner_blob="$(git --no-replace-objects rev-parse "$TRUSTED_SHA:scripts/run-all-gates.py")"
      candidate_runner_blob="$(git --no-replace-objects rev-parse "$HEAD_SHA:scripts/run-all-gates.py" 2>/dev/null || true)"
      if [[ "$trusted_runner_blob" != "$candidate_runner_blob" ]]; then
        selection_force_full_reason=runner_changed
      fi
        selection_bootstrap_reason=selector_error
        export TRUSTED_SHA HEAD_SHA
        if python -I -B - "$selection_dir" "$trusted_runner_blob" <<'LEAF_SELECTION_INPUTS'
import hashlib
import json
import os
from pathlib import Path
import re
import sys

directory = Path(sys.argv[1])
canonical = lambda value: json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
catalog = json.loads((directory / "catalog.json").read_text(encoding="utf-8"))
if catalog.get("schema") != "leaf.ci.catalog.v1" or not catalog.get("suites"):
    raise ValueError("invalid_catalog")
# S1 uses a document digest; the runner's existing fingerprint is a separate
# identity. Preserve the public --list document and adapt only the S1 input.
(directory / "runner-catalog.json").write_bytes(canonical(catalog) + b"\n")
catalog["runner_catalog_sha256"] = catalog.pop("catalog_sha256")
catalog.update(schema="leaf.ci.test-catalog.v1", kind="web",
               runner_path="scripts/run-all-gates.py", runner_blob_sha=sys.argv[2])
catalog["catalog_sha256"] = hashlib.sha256(canonical(catalog)).hexdigest()
(directory / "catalog.json").write_bytes(canonical(catalog) + b"\n")
e = os.environ
source = e.get("CODEBUILD_SOURCE_VERSION", "")
match = re.fullmatch(r"pr/([1-9][0-9]*)", source)
initiator = e.get("CODEBUILD_INITIATOR", "")
event = {"provider": "github", "repo": "LEAF-Solar-Design/leaf-web-demo",
         "initiator": "GitHub-Hook" if initiator == "GitHub-Hookshot" else initiator,
         "provider_bound": bool(e.get("CODEBUILD_BUILD_ID")),
         "build_id": e.get("CODEBUILD_BUILD_ID", ""),
         "evidence_ref": e.get("CODEBUILD_BUILD_ARN", ""),
         "event": e.get("CODEBUILD_WEBHOOK_EVENT", "unknown"),
         "source_version": source, "pr_number": int(match[1]) if match else None,
         "head_sha": e.get("CODEBUILD_RESOLVED_SOURCE_VERSION", ""),
         "head_ref": e.get("CODEBUILD_WEBHOOK_HEAD_REF", ""),
         "target_ref": e.get("CODEBUILD_WEBHOOK_BASE_REF", "")}
# No queue/filter/revocation claim is manufactured from webhook presence.
# Absent enforcement-producer evidence is a full-execution decision in S1.
(directory / "event.json").write_bytes(canonical(event) + b"\n")
LEAF_SELECTION_INPUTS
        then
          pr_number="${CODEBUILD_SOURCE_VERSION:-}"
          pr_number="${pr_number#pr/}"
          if python -I -B "$selection_dir/select_tests.py" decide \
            --repo "$CODEBUILD_SRC_DIR" --trusted-sha "$TRUSTED_SHA" --head-sha "$HEAD_SHA" \
            --map "$selection_dir/test-selection-map.json" --out-dir "$selection_dir" \
            --event-evidence "$selection_dir/event.json" --catalog "$selection_dir/catalog.json" \
            --repo-slug LEAF-Solar-Design/leaf-web-demo --pr-number "$pr_number" > "$selection_dir/decision-lines.txt"; then
            selection_ready=1
          fi
        fi
    else
      selection_bootstrap_reason=catalog_load_failed
    fi
  fi
fi

# The emitter independently validates substring-union closure and writes NULs.
if [[ "$selection_ready" == 1 ]]; then
  if [[ -n "$selection_force_full_reason" ]]; then
    if ! python -I -B - "$selection_dir/decision.json" "$selection_force_full_reason" <<'LEAF_SELECTION_FORCE_FULL'
import json
from pathlib import Path
import sys
path = Path(sys.argv[1])
decision = json.loads(path.read_text(encoding="utf-8"))
decision.update(selection_mode="full", execution_mode="full", apply_filter=False,
                executed_suite_ids=decision.get("catalog_suite_ids", []))
decision["reasons"] = sorted(set(decision.get("reasons", []) + [sys.argv[2]]))
path.write_text(json.dumps(decision, sort_keys=True) + "\n", encoding="utf-8")
LEAF_SELECTION_FORCE_FULL
    then
      selection_ready=0
      selection_bootstrap_reason=runner_changed
    fi
  fi
fi
if [[ "$selection_ready" == 1 ]]; then
  if python -I -B "$selection_dir/select_tests.py" emit-web-args \
    --decision "$selection_dir/decision.json" --catalog "$selection_dir/catalog.json" \
    --out "$selection_dir/only-args.nul"; then
    if python -I -B - "$selection_dir/decision.json" <<'LEAF_SELECTION_APPLY'
import json
import sys
decision = json.load(open(sys.argv[1], encoding="utf-8"))
raise SystemExit(0 if decision.get("apply_filter") is True else 1)
LEAF_SELECTION_APPLY
    then
      mapfile -d '' -t only_args < "$selection_dir/only-args.nul"
    fi
  else
    selection_ready=0
    selection_bootstrap_reason=selection_args_invalid
    only_args=()
  fi
fi
if [[ "$selection_ready" != 1 ]]; then
  if [[ -n "$selection_force_full_reason" ]]; then
    selection_bootstrap_reason="$selection_force_full_reason"
  fi
  # Inline trusted fallback, never imported from the candidate checkout.
  python -I -B - "$selection_dir" "$selection_bootstrap_reason" <<'LEAF_SELECTION_FALLBACK'
import json
from pathlib import Path
import sys
out = Path(sys.argv[1])
payload = {"schema": "leaf.ci.selection-decision.v1", "selection_mode": "full",
           "execution_mode": "full", "reasons": [sys.argv[2]], "apply_filter": False}
(out / "decision.json").write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
(out / "only-args.nul").write_bytes(b"")
LEAF_SELECTION_FALLBACK
fi

# Capture helpers share the frozen commit. Missing capture never grants coverage.
capture_ready=1
[[ -s "$selection_dir/select_tests.py" ]] || capture_ready=0
for helper in sitecustomize.py trace_reads.py pytest_selection.py; do
  if git --no-replace-objects show "$TRUSTED_SHA:scripts/ci/$helper" > "$selection_dir/$helper"; then
    chmod 400 "$selection_dir/$helper"
  else
    capture_ready=0
  fi
done
for reporter in vitest-leaf.mjs playwright-leaf.mjs; do
  if git --no-replace-objects show "$TRUSTED_SHA:scripts/ci/reporters/$reporter" > "$selection_dir/$reporter"; then
    chmod 400 "$selection_dir/$reporter"
  else
    capture_ready=0
  fi
done
export TRUSTED_SHA HEAD_SHA
if ! python -I -B - "$selection_dir" <<'LEAF_SELECTION_RECEIPTS'
import datetime
import json
import os
from pathlib import Path
import subprocess
import sys

out = Path(sys.argv[1])
e = os.environ
canonical = lambda value: json.dumps(value, sort_keys=True, separators=(",", ":"))
decision = json.loads((out / "decision.json").read_text(encoding="utf-8"))
if decision.get("execution_mode") not in ("full", "selected"):
    raise ValueError("invalid_execution_mode")
if (decision.get("execution_mode") == "selected") != (decision.get("apply_filter") is True):
    raise ValueError("invalid_filter")

def blob(path):
    proc = subprocess.run(["git", "--no-replace-objects", "rev-parse",
                           e.get("TRUSTED_SHA", "") + ":" + path],
                          capture_output=True, text=True)
    return proc.stdout.strip() if proc.returncode == 0 else None

def optional(name):
    try:
        return json.loads((out / name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}

catalog = optional("runner-catalog.json")
if not catalog:
    catalog = optional("catalog.json")
policy = optional("test-selection-map.json")
source = e.get("CODEBUILD_SOURCE_VERSION", "")
ref = e.get("CODEBUILD_WEBHOOK_HEAD_REF", "")
raw = e.get("CODEBUILD_WEBHOOK_EVENT", "")
event = ("merge_group" if "gh-readonly-queue/" in ref else
         "pr" if source.startswith("pr/") and raw.startswith("PULL_REQUEST_") else
         "main" if ref == "refs/heads/main" and raw == "PUSH" else
         "manual" if not raw and e.get("CODEBUILD_INITIATOR") else "unknown")
ids = decision.get("selected_test_ids")
reason = ",".join(decision.get("reasons", [])) or None
fallback = reason if decision.get("selection_mode") == "full" else None
receipt = {"schema": "leaf.ci-selection-mode.v1", "repo": "LEAF-Solar-Design/leaf-web-demo",
           "build_id": e.get("CODEBUILD_BUILD_ID", ""),
           "project": e.get("CODEBUILD_PROJECT_NAME") or e.get("CODEBUILD_BUILD_ID", "").split(":")[0],
           "sha": e.get("HEAD_SHA", ""), "source_version": source, "event": event,
           "mode": decision["execution_mode"], "trusted_policy_sha": e.get("TRUSTED_SHA", ""),
           "selector_sha": blob("scripts/ci/select_tests.py"),
           "map_sha": blob("scripts/ci/test-selection-map.json"),
           "selected_count": len(ids) if isinstance(ids, list) else None,
           "fallback_reason": fallback,
           "written_at": datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")}
detail = dict(decision, schema="leaf.ci.selection.v1", repo=receipt["repo"],
              build_id=receipt["build_id"], head_sha=receipt["sha"],
              trusted_sha=receipt["trusted_policy_sha"], selector_sha=receipt["selector_sha"],
              map_sha=receipt["map_sha"], ci_blob_sha=blob(".codebuild/ci.sh"),
              phase=policy.get("phase"), event_class=event, fallback_reason=fallback,
              runner_catalog_sha256=catalog.get("catalog_sha256"),
              catalog_suite_ids=[row["id"] for row in catalog.get("suites", [])],
              execution_complete=False, collection_complete=False, started_at=receipt["written_at"],
              attempts_ref="/tmp/gate-logs/attempts", readsets_ref="/tmp/gate-logs/readsets")
if detail["execution_mode"] == "full":
    detail["executed_suite_ids"] = detail["catalog_suite_ids"]
(out / "mode.json").write_text(canonical(receipt) + "\n", encoding="utf-8")
(out / "detail.json").write_text(canonical(detail) + "\n", encoding="utf-8")
(out / "execution-mode").write_text(receipt["mode"], encoding="ascii")
print("LEAF_SELECTION " + canonical(detail))
total = len(detail["catalog_suite_ids"])
count = len(detail.get("expanded_suite_ids", [])) if detail.get("selection_mode") != "full" else total
print("SELECTION {} selected={} of {} reasons={}".format(
    detail.get("selection_mode", "full"), count, total, reason or "none"))
print("SELECTION_ARM assigned={} bucket={} rule=sha256-pr-v1 window={}".format(
    detail.get("assigned_arm") or "none", detail.get("assignment_bucket"), detail.get("window_id") or "none"))
LEAF_SELECTION_RECEIPTS
then
  # Invalid receipt preparation cannot leave selected arguments executable.
  only_args=()
  selection_ready=0
  echo 'LEAF_SELECTION {"execution_mode":"full","reasons":["receipt_prepare_failed"],"apply_filter":false}'
fi
if [[ -f "$selection_dir/execution-mode" ]]; then
  selection_mode="$(<"$selection_dir/execution-mode")"
fi
export LEAF_SELECTION_EXECUTION_MODE="$selection_mode"
if [[ -n "${CODEBUILD_BUILD_ID:-}" ]]; then
  receipt_failed=0
  aws s3api put-object --bucket leaf-mq-transport-807034087062-us-east-1 \
    --key "mq/leaf-web-demo/selection/${CODEBUILD_BUILD_ID}.json" --body "$selection_dir/mode.json" \
    --content-type application/json --if-none-match '*' || receipt_failed=1
  aws s3api put-object --bucket leaf-mq-transport-807034087062-us-east-1 \
    --key "mq/leaf-web-demo/selection/${CODEBUILD_BUILD_ID}.detail.json" --body "$selection_dir/detail.json" \
    --content-type application/json --if-none-match '*' || receipt_failed=1
  if [[ "$receipt_failed" == 1 ]]; then
    if [[ "$selection_mode" == selected ]]; then
      echo 'receipt_write_failed: selected execution refused before candidate code' >&2
      exit 1
    fi
    echo 'WARNING: receipt_write_failed; full execution continues' >&2
  fi
fi
echo "LEAF_EVENT $(python3 -I -c '
import os, subprocess, sys, urllib.parse
e = os.environ
raw = e.get("CODEBUILD_WEBHOOK_EVENT", "")
allowed = {"PULL_REQUEST_CREATED", "PULL_REQUEST_UPDATED", "PULL_REQUEST_REOPENED", "PUSH"}
initiator = e.get("CODEBUILD_INITIATOR", "")
event = raw if raw in allowed else (
    "manual" if not raw and initiator and initiator != "GitHub-Hookshot"
    and not e.get("CODEBUILD_WEBHOOK_TRIGGER") else "unknown"
)
q = lambda value: urllib.parse.quote(value, safe="/:@._-+")
p = subprocess.run(
    ["git", "hash-object", "--no-filters", sys.argv[1]],
    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True
)
blob = p.stdout.strip() if p.returncode == 0 else "unknown"
fields = [
    ("event", event),
    ("head_ref", e.get("CODEBUILD_WEBHOOK_HEAD_REF", "")),
    ("base_ref", e.get("CODEBUILD_WEBHOOK_BASE_REF", "")),
    ("source_version", e.get("CODEBUILD_SOURCE_VERSION", "")),
    ("resolved", e.get("CODEBUILD_RESOLVED_SOURCE_VERSION", "")),
    ("ci_blob", blob),
    ("mode", e.get("LEAF_SELECTION_EXECUTION_MODE", "full")),
    ("workers", sys.argv[2]),
    ("image", e.get("CODEBUILD_BUILD_IMAGE", "")),
    ("compute", "unknown"),
]
print(" ".join(k + "=" + q(v) for k, v in fields))
' "${BASH_SOURCE[0]}" "$(case "${LEAF_GATE_JOBS:-auto}" in ''|*[!0-9]*|0*) echo unknown ;; *) echo "$LEAF_GATE_JOBS" ;; esac)")"
echo "LEAF_T start setup $(date +%s%3N)"

# Playwright supplies Chromium. The unused Chrome apt index must not prevent
# Ubuntu from supplying its OS libraries; signature/hash checks stay enabled.
python - <<'LEAF_CI_APT_SOURCE_FILTER'
from pathlib import Path
import re


def is_unused_chrome(uri):
    return uri in {
        f"{scheme}://dl.google.com/linux/chrome-stable/deb{suffix}"
        for scheme in ("http", "https") for suffix in ("", "/")
    }


def filter_list(text):
    result = []
    for line in text.splitlines(keepends=True):
        match = re.match(r"^\s*deb(?:-src)?\s+(?:\[[^\]\r\n]*\]\s+)?(\S+)", line)
        result.append("# leaf-ci unused Chrome source: " + line
                      if match and is_unused_chrome(match.group(1)) else line)
    return "".join(result)


def filter_deb822(text):
    # Retain separators and every unrelated stanza byte-for-byte.
    parts = re.split(r"((?:\r?\n)[ \t]*(?:\r?\n))", text)
    for index in range(0, len(parts), 2):
        stanza = parts[index]
        match = re.search(r"(?im)^URIs:[^\r\n]*(?:\r?\n[ \t]+[^\r\n]*)*", stanza)
        if not match:
            continue
        uris = match.group().split(":", 1)[1].split()
        kept = [uri for uri in uris if not is_unused_chrome(uri)]
        if kept == uris:
            continue
        if kept:
            stanza = stanza[:match.start()] + "URIs: " + " ".join(kept) + stanza[match.end():]
        else:
            # Comment the complete stanza; never leave an enabled URI-less entry.
            stanza = "".join("# leaf-ci unused Chrome source: " + line
                             for line in stanza.splitlines(keepends=True))
        parts[index] = stanza
    return "".join(parts)


def disable_unused_chrome_sources(root):
    candidates = [root / "sources.list"]
    directory = root / "sources.list.d"
    if directory.is_dir():
        candidates.extend(sorted(directory.glob("*.list")))
        candidates.extend(sorted(directory.glob("*.sources")))
    for path in candidates:
        if not path.exists():
            continue
        original = path.read_bytes()
        text = original.decode("utf-8")
        updated = filter_deb822(text) if path.suffix == ".sources" else filter_list(text)
        if updated != text:
            path.write_bytes(updated.encode("utf-8"))


if __name__ == "__main__":
    disable_unused_chrome_sources(Path("/etc/apt"))
LEAF_CI_APT_SOURCE_FILTER

# leaf-web-demo native CI: contract, license-fence, and Test gate UNSHARDED.
# The queue leg lives in .codebuild/mq.sh and is not this script's job.
# Workflows still dark: speculate-platform-images, prewarm-staging-cutover,
# the two PostgreSQL gates behind path filters, simulator-gate, the qualify-*
# dispatch workflows, and dispatch-staging-deploys.
npm i -g npm@10
echo "node=$(node -v 2>/dev/null || true) npm=$(npm -v 2>/dev/null || true) python=$(python --version 2>&1)"
BASE_REF="${CODEBUILD_WEBHOOK_BASE_REF:-}"; BASE_REF="${BASE_REF##refs/heads/}"; BASE_REF="${BASE_REF:-main}"
if git rev-parse --verify -q "origin/$BASE_REF" >/dev/null; then echo "origin/$BASE_REF"; elif git rev-parse --verify -q "$BASE_REF" >/dev/null; then echo "$BASE_REF"; else echo "FATAL: base ref $BASE_REF not in clone"; git branch -a | head -20; exit 1; fi > /tmp/base_ref
echo "base=$(cat /tmp/base_ref) head=$(git rev-parse --short HEAD) event=${CODEBUILD_WEBHOOK_EVENT:-manual}"
export BASE="$(cat /tmp/base_ref)"
echo "LEAF_T end setup $(date +%s%3N) rc=0"

echo "LEAF_T start contract $(date +%s%3N)"
echo "=== job contract ==="
cd "$CODEBUILD_SRC_DIR"
echo "--- 1/2 Install contract test dependencies"
python -m pip install pytest "PyYAML>=6"
cd "$CODEBUILD_SRC_DIR"
echo "--- 2/2 Run workflow shape contract"
PYTHONSAFEPATH=1 python -m pytest -q \
  tests/test_contract_workflow_shape.py \
  tests/test_dispatch_staging_deploys_shape.py
echo "LEAF_T end contract $(date +%s%3N) rc=0"

echo "=== job license-fence ==="
echo "LEAF_T start webdeps $(date +%s%3N)"
cd "$CODEBUILD_SRC_DIR/web"
echo "--- 1/4 Install web dependencies"
npm ci
echo "LEAF_T end webdeps $(date +%s%3N) rc=0"
echo "LEAF_T start webbundle $(date +%s%3N)"
cd "$CODEBUILD_SRC_DIR/web"
echo "--- 2/4 Build web bundle"
npm run build
echo "LEAF_T end webbundle $(date +%s%3N) rc=0"
echo "LEAF_T start license-fence $(date +%s%3N)"
cd "$CODEBUILD_SRC_DIR"
echo "--- 3/4 License fence self-test"
python scripts/check_license_fence.py --self-test
cd "$CODEBUILD_SRC_DIR"
echo "--- 4/4 License fence scan"
python scripts/check_license_fence.py .
echo "LEAF_T end license-fence $(date +%s%3N) rc=0"

echo "=== job test-gate ==="
echo "LEAF_T start pydeps $(date +%s%3N)"
cd "$CODEBUILD_SRC_DIR"
echo "--- 1/6 Upgrade pip"
python -m pip install --upgrade pip
cd "$CODEBUILD_SRC_DIR"
echo "--- 2/6 Install Python dependencies"
python -m pip install \
  -r server/requirements.txt \
  -r server/requirements-auth.txt \
  -r da/requirements.txt \
  -r platform/requirements.txt \
  -r scripts/requirements-ci.txt \
  -r executor/control_plane/requirements.txt \
  -r executor/runtime/requirements.txt
echo "LEAF_T end pydeps $(date +%s%3N) rc=0"
echo "LEAF_T start harness-deps $(date +%s%3N)"
cd "$CODEBUILD_SRC_DIR/harness"
echo "--- 3/6 Install harness dependencies"
npm ci
echo "LEAF_T end harness-deps $(date +%s%3N) rc=0"
cd "$CODEBUILD_SRC_DIR/web"
echo "--- 4/6 Reuse web dependencies"
# The license-fence job installed a clean web tree in this same build.
# No intervening step changes its dependencies, so reuse that npm ci.
echo "LEAF_T start chromium $(date +%s%3N)"
cd "$CODEBUILD_SRC_DIR/web"
echo "--- 5/6 Install Chromium for browser proofs"
# LEAF_CI_BROWSER_INSTALL_BEGIN
# standard:7.0 needs OS libraries, and --with-deps shells out to apt, so this
# step used to inherit archive.ubuntu.com's availability. Measured 2026-09-11
# between 06:37Z and 07:30Z: leaf-ci-leaf-web-demo failed SIX of seven
# consecutive builds right here, on main and on every merge-queue group, with
# "Could not connect to archive.ubuntu.com:80 ... connection timed out" against
# all six mirror IPs, before a single suite ran, and every queued PR ejected
# behind it. A fallback is honest rather than a papered-over failure because in
# that same log every library Chromium needs read "is already the newest
# version" (libasound2, libcairo2, libcups2, libdbus-1-3, libdrm2, libxkbcommon0,
# libxrandr2, libatk*); the only packages the mirror could not serve were
# optional FONT packages (fonts-ipafont-gothic, fonts-freefont-ttf,
# fonts-unifont, fonts-wqy-zenhei, xfonts-*), which change glyph coverage in a
# rendered page and nothing any suite in this repo asserts. Two attempts, not
# more: each failed --with-deps costs a full apt timeout against six unreachable
# IPs, and the fallback is what saves the build. A missing BROWSER BINARY is
# still a hard failure: the last call is unguarded, so set -e fails the build.
install_ci_browser() {
  local attempt
  for attempt in 1 2; do
    if "$@" install --with-deps chromium; then
      return 0
    fi
    if [[ "$attempt" == 1 ]]; then
      echo "WARNING: $* dependency install failed; retrying in 5 seconds (attempt 2/2)." >&2
      sleep 5
    fi
  done
  echo "WARNING: $* dependency install failed after 2 attempts; falling back to browser-only install. OS libraries may be missing; browser proofs will check runtime usability." >&2
  "$@" install chromium
}

install_ci_browser npx playwright
# Install the Python producer's pinned browser with the gate interpreter too.
install_ci_browser python -m playwright
echo "LEAF_T end chromium $(date +%s%3N) rc=0"
# LEAF_CI_BROWSER_INSTALL_END
echo "LEAF_T start gate $(date +%s%3N)"
cd "$CODEBUILD_SRC_DIR"
echo "--- 6/6 Run unsharded test gate and print scoreboard"
export LEAF_AUTOFILL_SOLVER_ABSENT_OK=1
export LEAF_MANAGED_WEB_BROWSER_MODE=trusted-template-container
mkdir -p /tmp/gate-results
export LEAF_READSET_DIR=/tmp/gate-logs/readsets
export LEAF_READSET_RUN="${CODEBUILD_BUILD_ID:-}"
export LEAF_READSET_ROOT="$CODEBUILD_SRC_DIR"
export LEAF_READSET_SOURCE_SHA="$HEAD_SHA"
export LEAF_READSET_SOURCE_TREE="$(git --no-replace-objects rev-parse "$HEAD_SHA^{tree}" 2>/dev/null || true)"
export LEAF_READSET_CAPTURE_SHA="$(git --no-replace-objects rev-parse "$TRUSTED_SHA:scripts/ci/trace_reads.py" 2>/dev/null || true)"
export LEAF_READSET_CATALOG_SHA256="$(python -I -B - "$selection_dir/detail.json" <<'LEAF_CAPTURE_ID'
import json
import sys
try:
    print(json.load(open(sys.argv[1], encoding="utf-8")).get("runner_catalog_sha256") or "")
except (OSError, ValueError):
    print("")
LEAF_CAPTURE_ID
)"
if [[ "$capture_ready" == 1 ]]; then
  export LEAF_TRUSTED_CI_DIR="$selection_dir"
  export PYTHONPATH="$selection_dir"
else
  unset LEAF_TRUSTED_CI_DIR
  echo 'WARNING: trusted capture unavailable; readsets and test reports remain incomplete' >&2
fi
gate_status=0
unset PYTHONSAFEPATH
python scripts/run-all-gates.py --jobs "${LEAF_GATE_JOBS:-auto}" --retry 1 --result-json /tmp/gate-results/gate-result.json --log-dir /tmp/gate-logs "${only_args[@]}" || gate_status=$?
echo "LEAF_T end gate $(date +%s%3N) rc=$gate_status"
if [[ -f /tmp/gate-results/gate-result.json ]]; then
  tail -n 200 /tmp/gate-results/gate-result.json || true
else
  echo "No gate result JSON was written (runner exit $gate_status)"
fi
echo "LEAF_T start change-impact $(date +%s%3N)"
echo "=== job change-impact ==="
# Advisory in S1: prints the assessment, never changes gate_status. Kill switch honoured.
python scripts/ci/change_impact_job.py --repo . --head "${CODEBUILD_RESOLVED_SOURCE_VERSION:-HEAD}" \
  --base-ref "${CODEBUILD_WEBHOOK_BASE_REF:-}" --head-ref "${CODEBUILD_WEBHOOK_HEAD_REF:-}" \
  --event "${CODEBUILD_WEBHOOK_EVENT:-manual}" --gate-result /tmp/gate-results/gate-result.json \
  --receipt-dir /tmp/impact || echo "change-impact: helper exit $? (advisory)"
echo "LEAF_T end change-impact $(date +%s%3N) rc=0"
python -I -B - "$selection_dir" "$gate_status" <<'LEAF_SELECTION_FINALIZE' || echo 'WARNING: selection evidence finalization failed' >&2
import datetime
import hashlib
import json
from pathlib import Path
import sys

out = Path(sys.argv[1])
canonical = lambda value: json.dumps(value, sort_keys=True, separators=(",", ":"))
try:
    detail = json.loads((out / "detail.json").read_text(encoding="utf-8"))
except (OSError, ValueError):
    detail = {"schema": "leaf.ci.selection.v1", "execution_mode": "full",
              "fallback_reason": "receipt_prepare_failed"}
records = []
raw = b""
valid = True
for path in sorted(Path("/tmp/gate-logs/attempts").glob("*.jsonl")):
    try:
        data = path.read_bytes()
        rows = [json.loads(line) for line in data.splitlines()]
        if any(not isinstance(row, dict) or not isinstance(row.get("test_ids", []), list) or
               not isinstance(row.get("failed_test_ids", []), list) or
               any(not isinstance(tid, str) for tid in row.get("test_ids", []) + row.get("failed_test_ids", []))
               for row in rows):
            raise ValueError("invalid_attempt_record")
        if any(row.get("run_id") != detail.get("build_id", "") for row in rows):
            valid = False
        records.extend(rows)
        raw += data
    except (OSError, ValueError, TypeError):
        valid = False
attempts_path = Path("/tmp/gate-results/selection-attempts.jsonl")
attempts_path.write_bytes(raw)
first = [row for row in records if row.get("attempt") == 1]
expected = set(detail.get("executed_suite_ids", []))
observed = {row.get("suite_id") for row in first}
all_ids = sorted({tid for row in first for tid in row.get("test_ids", [])})
failed = sorted({tid for row in first for tid in row.get("failed_test_ids", [])})
selected = sorted(set(detail.get("selected_test_ids", [])))
reporting = (valid and bool(expected) and observed == expected and len(first) == len(expected)
             and all(row.get("test_report_complete") is True for row in first))
complete = reporting and all(row.get("status") in ("PASS", "FAIL") for row in first)
detail.update(execution_complete=complete, test_exit_code=int(sys.argv[2]),
              build_exit_code=int(sys.argv[2]), collection_complete=reporting,
              collection_ids_sha256=hashlib.sha256(canonical(all_ids).encode("utf-8")).hexdigest(),
              attempts_ref=str(attempts_path), attempts_sha256=hashlib.sha256(raw).hexdigest(),
              test_id_reporting_complete=reporting,
              full_run_complete=complete and detail.get("execution_mode") == "full",
              finished_at=datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"))
print("LEAF_SELECTION_FINAL " + canonical(detail))
if detail.get("phase") == "shadow":
    shadow = {"id": detail.get("build_id"), "sha": detail.get("head_sha"),
              "selector_sha": detail.get("selector_sha"), "map_sha": detail.get("map_sha"),
              "selected_ids": len(selected), "full_failed_ids": failed,
              "contained": set(failed) <= set(selected), "first_attempt": True,
              "selected_test_ids": selected, "selected_suite_ids": detail.get("expanded_suite_ids", []),
              "catalog_sha256": detail.get("runner_catalog_sha256"),
              "collection_ids_sha256": detail["collection_ids_sha256"],
              "attempts_ref": detail["attempts_ref"], "attempts_sha256": detail["attempts_sha256"],
              "full_run_complete": detail["full_run_complete"],
              "test_id_reporting_complete": reporting, "synthetic": False,
              "fallback_reason": detail.get("fallback_reason"),
              "assigned_arm": detail.get("assigned_arm"), "execution_mode": detail.get("execution_mode")}
    print("LEAF_SHADOW " + canonical(shadow))
LEAF_SELECTION_FINALIZE
exit "$gate_status"
