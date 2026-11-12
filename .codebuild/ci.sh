#!/usr/bin/env bash
set -euo pipefail
LEAF_CI_SCRIPT_PATH="$(realpath "${BASH_SOURCE[0]}")"
# BEGIN SHARD ENV
CI_SHARD_TOTAL_N=1
CI_SHARD_INDEX_N=0
CI_SHARD_MODE=all
shard_env_valid=0
if [[ -z "${CI_SHARD_TOTAL:-}" && -z "${CI_SHARD_INDEX:-}" ]]; then
  shard_env_valid=1
elif [[ "${CI_SHARD_TOTAL:-}" =~ ^0*1$ && ( -z "${CI_SHARD_INDEX:-}" || "${CI_SHARD_INDEX:-}" =~ ^0+$ ) ]]; then
  shard_env_valid=1
elif [[ "${CI_SHARD_TOTAL:-}" =~ ^0*[2-8]$ && "${CI_SHARD_INDEX:-}" =~ ^0*[0-7]$ ]]; then
  CI_SHARD_TOTAL_N=$((10#$CI_SHARD_TOTAL))
  CI_SHARD_INDEX_N=$((10#$CI_SHARD_INDEX))
  if (( CI_SHARD_INDEX_N < CI_SHARD_TOTAL_N )); then
    CI_SHARD_MODE=shard
    shard_env_valid=1
  fi
fi
if [[ "$shard_env_valid" != 1 ]]; then
  echo "ci.sh: refusing unsupported shard env CI_SHARD_INDEX=${CI_SHARD_INDEX:-} CI_SHARD_TOTAL=${CI_SHARD_TOTAL:-}" >&2
  exit 2
fi
echo "LEAF_EVENT shard mode=$CI_SHARD_MODE index=$CI_SHARD_INDEX_N total=$CI_SHARD_TOTAL_N"
# END SHARD ENV
# A post-cheap-phase callback re-enters this exact trusted script, not a
# candidate helper. Only dependency-backed heavy prerequisites run here.
if [[ "${1:-}" == --heavy-prerequisites ]]; then
  if [[ "${LEAF_CI_POSTGRES_SETUP_STATUS:-0}" != 0 ]]; then
    echo "FATAL: ephemeral PostgreSQL did not start; refusing browser and web setup so this build fails before them" >&2
    exit 1
  fi
browser_log_dir="$(mktemp -d)"
# Give the browser and its children a process group for failure cleanup.
set -m
(
set -euo pipefail
trap 'exit 143' TERM
echo "LEAF_T start chromium $(date +%s%3N)"
cd "$CODEBUILD_SRC_DIR/web"
echo "--- 5/6 Install Chromium for browser proofs"
# LEAF_CI_BROWSER_INSTALL_BEGIN
# Install the browser first; a missing binary is a hard failure under set -e.
# Then check a real headless launch through the same producer, bounded to 60 s.
# Run apt via --with-deps only after a failed launch, bounded to 420 s.
# On 2026-09-11 mirror failures stopped six of seven builds even though runtime
# libraries were already installed; only optional fonts could not be served.
# On 2026-11-10 jobs 18313 and 18316 waited over 8m39s on silent mirrors.
# Recent per-job apt updates fetched 50.9 MB with zero new packages; setup
# medians were 31.8 s on run workers and 58.8 s on WSL, with stalls over 1000 s.
# timeout kills the whole process group. After one dependency timeout, skip
# later dependency stages; browser proofs still check OS runtime usability.
browser_deps_timeout_s=420
browser_launch_check_timeout_s=60
browser_deps_unavailable=0
install_ci_browser() {
  local status=0 launch_file launch_started
  "$@" install chromium
  launch_file="$(mktemp)"
  launch_started="$(date +%s%3N)"
  timeout --kill-after=15 "$browser_launch_check_timeout_s" "$@" pdf about:blank "$launch_file" || status=$?
  rm -f "$launch_file"
  if [[ "$status" == 0 ]]; then
    echo "browser launch check passed for $*; OS dependency stage skipped ($(( $(date +%s%3N) - launch_started )) ms)"
    return 0
  fi
  echo "WARNING: $* launch check failed; running the OS dependency stage" >&2
  status=0
  if [[ "$browser_deps_unavailable" == 0 ]]; then
    timeout --kill-after=15 "$browser_deps_timeout_s" "$@" install --with-deps chromium || status=$?
    if [[ "$status" == 0 ]]; then
      return 0
    fi
    if [[ "$status" == 124 || "$status" == 137 ]]; then
      browser_deps_unavailable=1
      echo "WARNING: $* dependency install exceeded ${browser_deps_timeout_s}s; skipping later dependency stages. Browser proofs still check OS runtime usability." >&2
    else
      echo "WARNING: $* dependency install failed. Browser proofs still check OS runtime usability." >&2
    fi
  else
    echo "WARNING: an earlier dependency stage exceeded ${browser_deps_timeout_s}s; skipping $* dependency stage. Browser proofs still check OS runtime usability." >&2
  fi
  return 0
}

if [[ -n "${LEAF_DEP_CACHE_HELPER:-}" && -n "${LEAF_CI_CACHE_DIR:-}" ]]; then
  timeout --kill-after=5 120 python -I -B "$LEAF_DEP_CACHE_HELPER" seed --kind pw --repo "$CODEBUILD_SRC_DIR" \
    --cache-dir "$LEAF_CI_CACHE_DIR" --writable "${LEAF_CI_CACHE_WRITABLE:-0}" --run "${CODEBUILD_BUILD_ID:-local}" \
    || echo "LEAF_DEP_CACHE kind=pw action=seed result=error"
fi
install_ci_browser npx playwright
# Install the Python producer's pinned browser with the gate interpreter too.
install_ci_browser python -m playwright
if [[ -n "${LEAF_DEP_CACHE_HELPER:-}" && -n "${LEAF_CI_CACHE_DIR:-}" && "${LEAF_CI_CACHE_WRITABLE:-0}" == 1 ]]; then
  timeout --kill-after=5 240 python -I -B "$LEAF_DEP_CACHE_HELPER" publish --kind pw --repo "$CODEBUILD_SRC_DIR" \
    --cache-dir "$LEAF_CI_CACHE_DIR" --writable 1 --run "${CODEBUILD_BUILD_ID:-local}" \
    || echo "LEAF_DEP_CACHE kind=pw action=publish result=error"
fi
echo "LEAF_T end chromium $(date +%s%3N) rc=0"
# LEAF_CI_BROWSER_INSTALL_END
) > "$browser_log_dir/chromium.log" 2>&1 &
browser_pid=$!
set +m
cleanup_ci_browser() {
  local rc=$?
  kill -- "-$browser_pid" 2>/dev/null || true
  wait "$browser_pid" 2>/dev/null || true
  cat "$browser_log_dir/chromium.log"
  rm -rf "$browser_log_dir"
  return "$rc"
}
trap cleanup_ci_browser EXIT
if [[ "${CI_SHARD_MODE:-all}" == all || "$CI_SHARD_INDEX_N" == 0 ]]; then
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
else
  echo "license fence: runs on shard 0"
fi
browser_rc=0
wait "$browser_pid" || browser_rc=$?
trap - EXIT
cat "$browser_log_dir/chromium.log"
rm -rf "$browser_log_dir"
if (( browser_rc != 0 )); then
  exit "$browser_rc"
fi
  exit 0
fi

# Freeze policy before any candidate installation, import, or test collection.
TRUSTED_SHA=""
HEAD_SHA=""
trusted_sha_override=0
proof_override_reason=trusted_load_failed
loader_check=not_supplied
selection_ready=0
selection_bootstrap_reason=trusted_load_failed
selection_force_full_reason=""
selection_mode=full
only_args=()
selection_dir="$(mktemp -d /tmp/leaf-selection.XXXXXXXX)"
chmod 700 "$selection_dir"
if TRUSTED_SHA="$(git --no-replace-objects rev-parse --verify 'refs/remotes/origin/main^{commit}')" \
   && HEAD_SHA="$(git --no-replace-objects rev-parse --verify 'HEAD^{commit}')"; then
  if [[ "${LEAF_PROOF_TRUSTED_SHA+x}" == x ]]; then
    if [[ "${CODEBUILD_WEBHOOK_EVENT+x}" == x || "${CODEBUILD_WEBHOOK_HEAD_REF+x}" == x || "${CODEBUILD_WEBHOOK_TRIGGER+x}" == x ]]; then
      proof_override_reason=webhook_build
    elif [[ ! "$LEAF_PROOF_TRUSTED_SHA" =~ ^[0-9a-fA-F]{40}$ ]]; then
      proof_override_reason=malformed_sha
    elif [[ "$LEAF_PROOF_TRUSTED_SHA" != "$HEAD_SHA" ]]; then
      proof_override_reason=head_sha_mismatch
    else
      TRUSTED_SHA="$HEAD_SHA"
      trusted_sha_override=1
      loader_check=override
    fi
  fi
  if [[ "$trusted_sha_override" != 1 && -n "${LEAF_LOADER_TRUSTED_SHA:-}" ]]; then
    loader_check=checked
  fi
  if [[ "$trusted_sha_override" != 1 && -n "${LEAF_LOADER_TRUSTED_SHA:-}" && "$TRUSTED_SHA" != "$LEAF_LOADER_TRUSTED_SHA" ]]; then
    loader_check=loader_sha_mismatch
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
try:
    head, trusted = e["HEAD_SHA"], e["TRUSTED_SHA"]
    relay_head = re.fullmatch(r"refs/pull/([1-9][0-9]*)/head", event["head_ref"])
    relay_base = event["target_ref"]
    operation = e.get("FORGE_CI_OPERATION", "")
    if (initiator == "states/leaf-forge-ci-relay" and
            event["event"] in ("PULL_REQUEST_CREATED", "PULL_REQUEST_UPDATED", "PULL_REQUEST_REOPENED") and
            re.fullmatch(r"[0-9A-Fa-f]{40}", head) and re.fullmatch(r"[0-9A-Fa-f]{40}", trusted) and
            relay_head and re.fullmatch(r"refs/heads/[A-Za-z0-9._/-]+", relay_base) and
            ".." not in relay_base and re.fullmatch(r"[A-Za-z0-9._-]+", operation)):
        manifest_sha256 = None
        manifest_status = "unreadable"
        try:
            manifest_bytes = Path(e.get("LEAF_RELAY_MANIFEST", "/tmp/manifest.json")).read_bytes()
            manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
            manifest = json.loads(manifest_bytes)
            checks = (
                ("schema", "leaf.forge-ci-relay.snapshot.v1"),
                ("repository", "LEAF-Solar-Design/leaf-web-demo"),
                ("head_sha", head), ("base_sha", trusted), ("base_ref", relay_base),
                ("operation", operation),
            )
            manifest_status = "verified"
            for field, expected in checks:
                if not isinstance(manifest, dict) or manifest.get(field) != expected:
                    manifest_status = "mismatch:" + field
                    break
            if manifest_status == "verified" and (
                    not isinstance(manifest.get("bundle_sha256"), str) or
                    not re.fullmatch(r"[0-9A-Fa-f]{64}", manifest["bundle_sha256"])):
                manifest_status = "mismatch:bundle_sha256"
        except FileNotFoundError:
            manifest_status = "missing"
        except (OSError, ValueError, UnicodeError):
            pass
        event.update(provider="forge", initiator="forge-ci-relay", source_version="",
                     head_sha=head, target_sha=trusted, pr_number=int(relay_head[1]),
                     provider_bound=manifest_status == "verified", relay_manifest=manifest_status,
                     evidence_ref={"build": e.get("CODEBUILD_BUILD_ARN") or e.get("CODEBUILD_BUILD_ID", ""),
                                   "manifest_sha256": manifest_sha256, "operation": operation})
    elif (re.fullmatch(r"device:dv-[0-9a-f]{16}", event["build_id"]) and
            event["event"] in ("PULL_REQUEST_CREATED", "PULL_REQUEST_UPDATED", "PULL_REQUEST_REOPENED") and
            relay_head and re.fullmatch(r"refs/heads/[A-Za-z0-9._/-]+", relay_base) and
            ".." not in relay_base and
            re.fullmatch(r"[0-9A-Fa-f]{40}", head) and re.fullmatch(r"[0-9A-Fa-f]{40}", trusted) and
            e.get("CODEBUILD_RESOLVED_SOURCE_VERSION") == head and
            operation == "ci-" + hashlib.sha256(
                ("LEAF-Solar-Design/leaf-web-demo|pull_request|" + relay_head[1] + "|" + head)
                .encode("utf-8")).hexdigest()[:40]):
        event.update(provider="forge-device", initiator="leaf-ci-device", provider_bound=True,
                     source_version="", pr_number=int(relay_head[1]), head_sha=head, target_sha=trusted,
                     evidence_ref={"build": event["build_id"], "operation": operation})
except Exception:
    pass
# No queue/filter/revocation claim is manufactured from webhook presence.
# Absent enforcement-producer evidence is a full-execution decision in S1.
(directory / "event.json").write_bytes(canonical(event) + b"\n")
LEAF_SELECTION_INPUTS
        then
          pr_number="${CODEBUILD_SOURCE_VERSION:-}"
          pr_number="${pr_number#pr/}"
          if [[ ! "${CODEBUILD_SOURCE_VERSION:-}" =~ ^pr/([1-9][0-9]*)$ && ( "${CODEBUILD_INITIATOR:-}" == "states/leaf-forge-ci-relay" || "${CODEBUILD_BUILD_ID:-}" =~ ^device:dv-[0-9a-f]{16}$ ) && "${CODEBUILD_WEBHOOK_HEAD_REF:-}" =~ ^refs/pull/([1-9][0-9]*)/head$ ]]; then
            pr_number="${BASH_REMATCH[1]}"
          fi
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

if [[ "${LEAF_CI_FULL:-}" == 1 && -z "$selection_force_full_reason" ]]; then
  selection_force_full_reason=explicit_full
fi

if [[ "${LEAF_PROOF_TRUSTED_SHA+x}" == x && "$trusted_sha_override" != 1 ]]; then
  echo "WARNING: LEAF_PROOF_TRUSTED_SHA ignored ($proof_override_reason)" >&2
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
reporters_ready=1
tracing_helpers_ready=1
[[ -s "$selection_dir/select_tests.py" ]] || tracing_helpers_ready=0
for helper in sitecustomize.py trace_reads.py; do
  if git --no-replace-objects show "$TRUSTED_SHA:scripts/ci/$helper" > "$selection_dir/$helper"; then
    chmod 400 "$selection_dir/$helper"
  else
    tracing_helpers_ready=0
  fi
done
if git --no-replace-objects show "$TRUSTED_SHA:scripts/ci/pytest_selection.py" > "$selection_dir/pytest_selection.py"; then
  chmod 400 "$selection_dir/pytest_selection.py"
else
  reporters_ready=0
fi
for reporter in vitest-leaf.mjs playwright-leaf.mjs; do
  if git --no-replace-objects show "$TRUSTED_SHA:scripts/ci/reporters/$reporter" > "$selection_dir/$reporter"; then
    chmod 400 "$selection_dir/$reporter"
  else
    reporters_ready=0
  fi
done
# The manifest helper is advisory; its absence must not disable read capture.
if git --no-replace-objects show "$TRUSTED_SHA:scripts/ci/full_run_manifest.py" > "$selection_dir/full_run_manifest.py"; then
  chmod 400 "$selection_dir/full_run_manifest.py"
else
  echo 'WARNING: trusted full-run manifest helper unavailable' >&2
fi
# LEAF_DEP_CACHE_HELPER_BEGIN
# The dependency cache helper shares the frozen commit; without it every job downloads as before.
unset LEAF_DEP_CACHE_HELPER
if [[ -n "${LEAF_CI_CACHE_DIR:-}" && "${LEAF_DEP_CACHE_OFF:-}" != 1 && "$TRUSTED_SHA" =~ ^[0-9a-f]{40}$ ]] \
   && git --no-replace-objects show "$TRUSTED_SHA:scripts/ci/dep_cache.py" > "$selection_dir/dep_cache.py" 2>/dev/null \
   && chmod 400 "$selection_dir/dep_cache.py"; then
  export LEAF_DEP_CACHE_HELPER="$selection_dir/dep_cache.py"
elif [[ -n "${LEAF_CI_CACHE_DIR:-}" ]]; then
  echo "LEAF_DEP_CACHE kind=all action=load result=off"
fi
# LEAF_DEP_CACHE_HELPER_END
# Only explicit tracing proofs enable capture; webhook builds stay fast.
tracing_ready=0
if [[ "$tracing_helpers_ready" == 1 && "${LEAF_PROOF_TRACING:-}" == 1 ]]; then
  tracing_ready=1
fi
export TRUSTED_SHA HEAD_SHA trusted_sha_override loader_check
if ! python -I -B - "$selection_dir" "$tracing_ready" "$reporters_ready" <<'LEAF_SELECTION_RECEIPTS'
import datetime
import json
import os
from pathlib import Path
import re
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
         "pr" if (e.get("CODEBUILD_INITIATOR") == "states/leaf-forge-ci-relay" or
                  re.fullmatch(r"device:dv-[0-9a-f]{16}", e.get("CODEBUILD_BUILD_ID", ""))) and
         raw.startswith("PULL_REQUEST_") and re.fullmatch(r"refs/pull/[1-9][0-9]*/head", ref) else
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
           "trusted_sha_override": e.get("trusted_sha_override") == "1",
           "loader_check": e.get("loader_check"),
           "selector_sha": blob("scripts/ci/select_tests.py"),
           "map_sha": blob("scripts/ci/test-selection-map.json"),
           "selected_count": len(ids) if isinstance(ids, list) else None,
           "fallback_reason": fallback,
           "written_at": datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")}
detail = dict(decision, schema="leaf.ci.selection.v1", repo=receipt["repo"],
              build_id=receipt["build_id"], head_sha=receipt["sha"],
              trusted_sha=receipt["trusted_policy_sha"], selector_sha=receipt["selector_sha"],
              trusted_sha_override=receipt["trusted_sha_override"], loader_check=receipt["loader_check"],
              map_sha=receipt["map_sha"], ci_blob_sha=blob(".codebuild/ci.sh"),
              phase=policy.get("phase"), event_class=event, fallback_reason=fallback,
              tracing_active=sys.argv[2] == "1", reporters_active=sys.argv[3] == "1",
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
  echo "LEAF_SELECTION {\"execution_mode\":\"full\",\"reasons\":[\"receipt_prepare_failed\"],\"apply_filter\":false,\"trusted_sha_override\":$([[ "$trusted_sha_override" == 1 ]] && echo true || echo false),\"loader_check\":\"$loader_check\"}"
fi
if [[ -f "$selection_dir/execution-mode" ]]; then
  selection_mode="$(<"$selection_dir/execution-mode")"
fi
# LEAF_PR_TIER_ACTIVATION_BEGIN
# Use the gate's policy loader once, before the startup event declares its mode.
pr_tier_active="$(python3 -I -B scripts/ci/build_pr_tier.py --active scripts/ci/pr-tier.json)"
export LEAF_SELECTION_EXECUTION_MODE="$selection_mode"
if [[ "$pr_tier_active" == 1 ]]; then
  export LEAF_SELECTION_EXECUTION_MODE=selected
fi
# LEAF_PR_TIER_ACTIVATION_END
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

# LEAF_DEP_CACHE_SEED_BEGIN
dep_cache_npm_seeded=0
dep_cache_pip_dir=""
dep_cache_npm_dir=""
if [[ -n "${LEAF_DEP_CACHE_HELPER:-}" && -n "${LEAF_CI_CACHE_DIR:-}" ]]; then
  echo "LEAF_T start depcache-seed $(date +%s%3N)"
  dep_cache_pip_dir="$(timeout 30 python -m pip cache dir 2>/dev/null || true)"
  dep_cache_npm_dir="$(timeout 30 npm config get cache 2>/dev/null || true)"
  dep_cache_line="$(timeout --kill-after=5 90 python -I -B "$LEAF_DEP_CACHE_HELPER" seed --kind dl \
    --repo "$CODEBUILD_SRC_DIR" --cache-dir "$LEAF_CI_CACHE_DIR" --writable "${LEAF_CI_CACHE_WRITABLE:-0}" \
    --run "${CODEBUILD_BUILD_ID:-local}" --day "$(date -u +%Y%m%d)" \
    --pip-cache "$dep_cache_pip_dir" --npm-cache "$dep_cache_npm_dir" --node-version "$(node -v 2>/dev/null || true)")" \
    || dep_cache_line="LEAF_DEP_CACHE kind=dl action=seed result=error"
  echo "$dep_cache_line"
  if [[ "$dep_cache_line" == *" npm=seeded"* ]]; then dep_cache_npm_seeded=1; fi
  echo "LEAF_T end depcache-seed $(date +%s%3N) rc=0"
fi
# LEAF_DEP_CACHE_SEED_END

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
# LEAF_CI_CORPUS_CAGE_SUPPLY_BEGIN
# The corpus executor requires this absolute path and seccomp capability.
# Reuse the pinned device-image recipe only on incapable ephemeral CI images.
echo "LEAF_T start corpus-cage-supply $(date +%s%3N)"
if ! /usr/bin/setpriv --help 2>/dev/null | grep -Fq -- '--seccomp-filter'; then
  if [[ "$(id -u)" != 0 ]]; then
    echo 'corpus cage supply refused: root required to provision /usr/bin/setpriv' >&2
    exit 1
  fi
  (
    set -euo pipefail
    supply_dir="$(mktemp -d /tmp/leaf-ci-setpriv.XXXXXX)"
    trap 'rm -rf -- "$supply_dir"' EXIT
    version=2.41.6
    archive="$supply_dir/util-linux-${version}.tar.xz"
    curl --proto '=https' --tlsv1.2 --fail --show-error --location \
      --connect-timeout 15 --max-time 120 --output "$archive" \
      "https://mirrors.edge.kernel.org/pub/linux/utils/util-linux/v2.41/util-linux-${version}.tar.xz"
    echo "e596083744e746be7d2823b62b43f4418dd7bf56303b4dc09e6fe8112fe3d7ed  $archive" | sha256sum -c -
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends libcap-ng-dev pkg-config
    tar -xJf "$archive" -C "$supply_dir"
    cd "$supply_dir/util-linux-${version}"
    ./configure --disable-all-programs --enable-setpriv --disable-nls \
      --without-python --without-systemd --without-udev --disable-bash-completion
    make -j2 setpriv
    dpkg-divert --local --rename --divert /usr/bin/setpriv.util-linux-2.37 --add /usr/bin/setpriv
    install -m 0755 setpriv /usr/bin/setpriv
  )
fi
/usr/bin/setpriv --help | grep -Fq -- '--seccomp-filter'
/usr/bin/setpriv --version
sha256sum /usr/bin/setpriv
echo "corpus cage supply uid=$(id -u)"
echo "LEAF_T end corpus-cage-supply $(date +%s%3N) rc=0"
# LEAF_CI_CORPUS_CAGE_SUPPLY_END
echo "LEAF_T end setup $(date +%s%3N) rc=0"

if [[ "${CI_SHARD_MODE:-all}" == all || "$CI_SHARD_INDEX_N" == 0 ]]; then
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
else
  echo "contract: runs on shard 0"
fi

# LEAF_CI_SETUP_DEPS_BEGIN
echo "=== job license-fence ==="
echo "=== job test-gate ==="
setup_log_dir="$(mktemp -d)"
(
set -euo pipefail
if [[ "${dep_cache_npm_seeded:-0}" == 1 ]]; then export npm_config_prefer_offline=true; fi
echo "LEAF_T start webdeps $(date +%s%3N)"
cd "$CODEBUILD_SRC_DIR/web"
echo "--- 1/4 Install web dependencies"
npm ci
echo "LEAF_T end webdeps $(date +%s%3N) rc=0"
) > "$setup_log_dir/webdeps.log" 2>&1 &
webdeps_pid=$!
(
set -euo pipefail
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
) > "$setup_log_dir/pydeps.log" 2>&1 &
pydeps_pid=$!
(
set -euo pipefail
if [[ "${dep_cache_npm_seeded:-0}" == 1 ]]; then export npm_config_prefer_offline=true; fi
echo "LEAF_T start harness-deps $(date +%s%3N)"
cd "$CODEBUILD_SRC_DIR/harness"
echo "--- 3/6 Install harness dependencies"
npm ci
echo "LEAF_T end harness-deps $(date +%s%3N) rc=0"
) > "$setup_log_dir/harness-deps.log" 2>&1 &
harness_deps_pid=$!
webdeps_rc=0
pydeps_rc=0
harness_deps_rc=0
wait "$webdeps_pid" || webdeps_rc=$?
wait "$pydeps_pid" || pydeps_rc=$?
wait "$harness_deps_pid" || harness_deps_rc=$?
cat "$setup_log_dir/webdeps.log" "$setup_log_dir/pydeps.log" "$setup_log_dir/harness-deps.log"
rm -rf "$setup_log_dir"
setup_rc=0
for setup_step in webdeps pydeps harness-deps; do
  case "$setup_step" in
    webdeps) step_rc=$webdeps_rc ;;
    pydeps) step_rc=$pydeps_rc ;;
    harness-deps) step_rc=$harness_deps_rc ;;
  esac
  if (( step_rc != 0 )); then
    echo "setup: $setup_step failed rc=$step_rc"
    if (( setup_rc == 0 )); then setup_rc=$step_rc; fi
  fi
done
if (( setup_rc != 0 )); then exit "$setup_rc"; fi
# LEAF_CI_SETUP_DEPS_END
# LEAF_DEP_CACHE_PUBLISH_BEGIN
dep_cache_publish_pid=""
if [[ -n "${LEAF_DEP_CACHE_HELPER:-}" && -n "${LEAF_CI_CACHE_DIR:-}" && "${LEAF_CI_CACHE_WRITABLE:-0}" == 1 ]]; then
  if dep_cache_publish_log="$(mktemp)"; then
    timeout --kill-after=5 180 python -I -B "$LEAF_DEP_CACHE_HELPER" publish --kind dl \
      --repo "$CODEBUILD_SRC_DIR" --cache-dir "$LEAF_CI_CACHE_DIR" --writable 1 \
      --run "${CODEBUILD_BUILD_ID:-local}" --day "$(date -u +%Y%m%d)" \
      --pip-cache "$dep_cache_pip_dir" --npm-cache "$dep_cache_npm_dir" --node-version "$(node -v 2>/dev/null || true)" \
      > "$dep_cache_publish_log" 2>&1 &
    dep_cache_publish_pid=$!
  else
    echo "LEAF_DEP_CACHE kind=dl action=publish result=error"
  fi
fi
# LEAF_DEP_CACHE_PUBLISH_END
cd "$CODEBUILD_SRC_DIR/web"
echo "--- 4/6 Reuse web dependencies"
# The license-fence job installed a clean web tree in this same build.
# No intervening step changes its dependencies, so reuse that npm ci.

echo "LEAF_T start gate $(date +%s%3N)"
cd "$CODEBUILD_SRC_DIR"
echo "--- 6/6 Run unsharded test gate and print scoreboard"
# LEAF_AUTOFILL_SOLVER_BEGIN
# Fetch the pinned autofill-solver from its Forge relay snapshot so the solver
# suites run against the real checkout. Bounded and fail-open: every command
# runs under timeout (about 80 s worst case), the function is only ever called
# as an if condition so errexit never aborts the fetch. Failures fall back to
# LEAF_AUTOFILL_SOLVER_ABSENT_OK except on device jobs without an explicit
# allowance. Integrity is git's own: the cloned HEAD must equal the pinned
# 40-hex revision.
autofill_solver_fail() {
  echo "autofill-solver: $1 failed; falling back to LEAF_AUTOFILL_SOLVER_ABSENT_OK" >&2
  return 1
}
fetch_autofill_solver() {
  local bucket=leaf-developer-platform-artifacts-807034087062-us-east-1
  local work listing
  autofill_solver_revision=""
  autofill_solver_key=""
  autofill_solver_revision="$(timeout 5 python3 -I -B - deploy/autofill-solver-sources.json <<'LEAF_AUTOFILL_PIN'
import json
import re
import sys
try:
    pins = json.load(open(sys.argv[1], encoding="utf-8"))
except (OSError, ValueError):
    sys.exit(1)
if not isinstance(pins, dict) or len(pins) != 1:
    sys.exit(1)
revision = next(iter(pins))
if not re.fullmatch(r"[0-9a-f]{40}", revision):
    sys.exit(1)
print(revision)
LEAF_AUTOFILL_PIN
)" || { autofill_solver_fail "pin read"; return 1; }
  [[ "$autofill_solver_revision" =~ ^[0-9a-f]{40}$ ]] || { autofill_solver_fail "pin read"; return 1; }
  local seeded=0
  if [[ -n "${LEAF_DEP_CACHE_HELPER:-}" && -n "${LEAF_CI_CACHE_DIR:-}" ]]; then
    work="$(mktemp -d)" || { autofill_solver_fail "mktemp"; return 1; }
    # Seed only into an absent destination; keep existing job caches untouched.
    if rmdir "$work" && { timeout --kill-after=5 30 python3 -I -B "$LEAF_DEP_CACHE_HELPER" seed --kind as --cache-dir "$LEAF_CI_CACHE_DIR" --writable "${LEAF_CI_CACHE_WRITABLE:-0}" --run "${CODEBUILD_BUILD_ID:-local}" --revision "$autofill_solver_revision" --dest "$work" >&2 || { echo "LEAF_DEP_CACHE kind=as action=seed result=error" >&2; false; }; } && [[ -s "$work/snapshot.bundle" ]]; then
      seeded=1
      autofill_solver_key="dep-cache"
    else
      rm -rf "$work"
    fi
  fi
  if [[ "$seeded" != 1 ]]; then
  local prefix="forge-ci-relay/LEAF-Solar-Design/autofill-solver/${autofill_solver_revision}/"
  listing="$(timeout 10 aws s3api list-objects-v2 --bucket "$bucket" --prefix "$prefix" \
    --max-items 1000 --output json 2>/dev/null)" || { autofill_solver_fail "snapshot list"; return 1; }
  autofill_solver_key="$(printf '%s' "$listing" | timeout 5 python3 -I -B -c '
import json
import sys
try:
    contents = json.load(sys.stdin).get("Contents") or []
except ValueError:
    sys.exit(1)
zips = [o for o in contents if str(o.get("Key", "")).endswith(".zip")]
if not zips:
    sys.exit(1)
print(max(zips, key=lambda o: str(o.get("LastModified", "")))["Key"])
')" || { autofill_solver_fail "snapshot select"; return 1; }
  [[ "$autofill_solver_key" == "$prefix"*.zip ]] || { autofill_solver_fail "snapshot select"; return 1; }
  work="$(mktemp -d)" || { autofill_solver_fail "mktemp"; return 1; }
  timeout 30 aws s3api get-object --bucket "$bucket" --key "$autofill_solver_key" \
    "$work/snapshot.zip" >/dev/null 2>&1 || { autofill_solver_fail "snapshot download"; return 1; }
  timeout 10 python3 -I -B - "$work/snapshot.zip" "$work" "$autofill_solver_revision" <<'LEAF_AUTOFILL_EXTRACT' || { autofill_solver_fail "snapshot extract"; return 1; }
import json
import shutil
import sys
import zipfile
from pathlib import Path
archive, out, revision = sys.argv[1], Path(sys.argv[2]), sys.argv[3]
try:
    with zipfile.ZipFile(archive) as zf:
        for name in ("manifest.json", "snapshot.bundle"):
            with zf.open(name) as src, open(out / name, "wb") as dst:
                shutil.copyfileobj(src, dst)
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
except (OSError, ValueError, KeyError, zipfile.BadZipFile):
    sys.exit(1)
if manifest.get("head_sha") != revision:
    sys.exit(1)
if manifest.get("repository") != "LEAF-Solar-Design/autofill-solver":
    sys.exit(1)
LEAF_AUTOFILL_EXTRACT
  fi
  rm -rf /tmp/autofill-solver
  timeout 15 git clone -q --no-checkout "$work/snapshot.bundle" /tmp/autofill-solver \
    || { autofill_solver_fail "bundle clone"; return 1; }
  timeout 5 git -C /tmp/autofill-solver checkout -q --detach "$autofill_solver_revision" \
    || { autofill_solver_fail "revision checkout"; return 1; }
  [[ "$(timeout 5 git -C /tmp/autofill-solver rev-parse HEAD)" == "$autofill_solver_revision" ]] \
    || { autofill_solver_fail "HEAD verification"; return 1; }
  if [[ -n "${LEAF_DEP_CACHE_HELPER:-}" && -n "${LEAF_CI_CACHE_DIR:-}" && "${LEAF_CI_CACHE_WRITABLE:-0}" == 1 ]]; then timeout --kill-after=5 30 python3 -I -B "$LEAF_DEP_CACHE_HELPER" publish --kind as --cache-dir "$LEAF_CI_CACHE_DIR" --writable 1 --run "${CODEBUILD_BUILD_ID:-local}" --revision "$autofill_solver_revision" --src "$work" >&2 || echo "LEAF_DEP_CACHE kind=as action=publish result=error" >&2; fi
  rm -rf "$work"
}
autofill_solver_fatal=0
if fetch_autofill_solver; then
  export AUTOFILL_SOLVER_ROOT=/tmp/autofill-solver AUTOFILL_SOLVER_REVISION="$autofill_solver_revision"
  echo "autofill-solver: pinned $autofill_solver_revision from relay snapshot $autofill_solver_key"
else
  if [[ "${CODEBUILD_BUILD_ID:-}" == device:* && "${LEAF_AUTOFILL_SOLVER_ABSENT_ALLOWED:-}" != 1 ]]; then
    echo "FATAL: autofill-solver unavailable on this worker (dep-cache seed and S3 fetch both failed); seed the leaf-web-demo dep cache or run on a credentialed worker" >&2
    autofill_solver_fatal=1
  else
    if [[ "${CODEBUILD_BUILD_ID:-}" == device:* && "${LEAF_AUTOFILL_SOLVER_ABSENT_ALLOWED:-}" == 1 ]]; then
      echo "autofill-solver: absent allowed by LEAF_AUTOFILL_SOLVER_ABSENT_ALLOWED"
    fi
    export LEAF_AUTOFILL_SOLVER_ABSENT_OK=1
  fi
fi
# LEAF_AUTOFILL_SOLVER_END
if [[ "${autofill_solver_fatal:-0}" == 1 ]]; then exit 1; fi
export LEAF_MANAGED_WEB_BROWSER_MODE=trusted-template-container
mkdir -p /tmp/gate-results
if [[ "$reporters_ready" == 1 ]]; then
  export LEAF_TRUSTED_CI_DIR="$selection_dir"
else
  unset LEAF_TRUSTED_CI_DIR
fi
export PYTHONPATH="$selection_dir"
# LEAF_PROCESS_CAPTURE_BEGIN
process_capture_active=0
if [[ "${LEAF_PROCESS_CAPTURE:-}" == 1 ]]; then
  # Explicit requests cannot inherit an ambient context around the manual-event guard.
  unset LEAF_PROCESS_CAPTURE_CONTEXT LEAF_PROCESS_CAPTURE_OUT
  process_capture_reason=""
  if [[ -n "${CODEBUILD_WEBHOOK_EVENT:-}" || -n "${CODEBUILD_WEBHOOK_HEAD_REF:-}" \
        || -n "${CODEBUILD_WEBHOOK_BASE_REF:-}" || -n "${CODEBUILD_WEBHOOK_TRIGGER:-}" \
        || "${CODEBUILD_INITIATOR:-}" == states/leaf-forge-ci-relay \
        || -n "${FORGE_CI_OPERATION:-}" || -n "${LEAF_RELAY_MANIFEST:-}" \
        || "${CODEBUILD_SOURCE_VERSION:-}" == pr/* || "${CODEBUILD_SOURCE_VERSION:-}" == refs/pull/* ]]; then
    process_capture_reason=non_manual_event
  elif [[ -n "${CI_SHARD_TOTAL:-}" && "${CI_SHARD_TOTAL}" != 1 ]]; then
    process_capture_reason=sharded_build
  fi
  if [[ -z "$process_capture_reason" ]]; then
    if ! { command -v strace >/dev/null 2>&1 && strace --version 2>/dev/null | head -1 | grep -q '^strace -- version 5\.16'; }; then
      if ! { timeout 120 apt-get update -qq >&2 \
          && DEBIAN_FRONTEND=noninteractive timeout 120 apt-get install -y 'strace=5.16*' >&2 \
          && command -v strace >/dev/null 2>&1 \
          && strace --version 2>/dev/null | head -1 | grep -q '^strace -- version 5\.16'; }; then
        process_capture_reason=strace_unavailable
      fi
    fi
  fi
  if [[ -z "$process_capture_reason" ]]; then
    capture_helpers_ready=1
    for helper in trace_process_tree.py trace_supervisor.py external_inventory.py epoch_probe.py; do
      if git --no-replace-objects show "$TRUSTED_SHA:scripts/ci/$helper" > "$selection_dir/$helper" \
          && chmod 400 "$selection_dir/$helper"; then
        :
      else
        capture_helpers_ready=0
      fi
    done
    if [[ "$capture_helpers_ready" != 1 || "$tracing_helpers_ready" != 1 || "$reporters_ready" != 1 ]]; then
      process_capture_reason=helpers_unavailable
    elif mkdir -p /tmp/process-capture/out \
      && python -I -B - "$selection_dir" "$CODEBUILD_SRC_DIR" "$HEAD_SHA" "${CODEBUILD_BUILD_ID:-}" \
        "$(git --no-replace-objects rev-parse "$HEAD_SHA^{tree}")" \
        "$(git --no-replace-objects rev-parse "$TRUSTED_SHA:scripts/ci/trace_reads.py")" \
        "$(python -I -B -c 'import json,sys; print(json.load(open(sys.argv[1]))["catalog_sha256"])' "$selection_dir/catalog.json")" \
        "${CODEBUILD_BUILD_IMAGE:-}" <<'LEAF_CAPTURE_BASE' >&2
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

out, repo, head, run_id, source_tree, capture_sha, catalog_sha, image = sys.argv[1:]
sys.path.insert(0, out)
import external_inventory
import full_run_manifest
import trace_process_tree as tree


def file_sha256(path):
    hasher = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1048576), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def git(*args, data=None):
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
    return subprocess.run(["git", "--no-replace-objects", "-C", repo, *args], input=data, env=env,
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True, timeout=120).stdout


# Inventory: every tracked name, and each symlink's target text from one batched read.
files, links = [], []
for record in git("ls-tree", "-r", "-z", "--full-tree", head).split(b"\0"):
    if not record:
        continue
    meta, _, name = record.partition(b"\t")
    fields = meta.split(b" ")
    files.append(name.decode("utf-8", "strict"))
    if fields[0] == b"120000":
        links.append((files[-1], fields[2].decode("ascii")))
symlinks = {}
if links:
    batch = git("cat-file", "--batch", data=b"".join(sha.encode("ascii") + b"\n" for _, sha in links))
    position = 0
    for name, sha in links:
        end = batch.index(b"\n", position)
        header = batch[position:end].split(b" ")
        if len(header) != 3 or header[0] != sha.encode("ascii") or header[1] != b"blob":
            raise SystemExit("symlink_target_unreadable")
        start = end + 1
        symlinks[name] = batch[start:start + int(header[2])].decode("utf-8", "strict")
        position = start + int(header[2]) + 1
os_image = {"class": "os-image", "origin_category": "os-image"}
supervisor = {"class": "supervisor", "origin_category": "supervisor"}
roots = external_inventory.resolve_default_roots(os.environ)
# Harness-owned: the trusted dir, the gate log directory and the result directory (Amendment 3).
for root in (out, "/tmp/gate-logs", "/tmp/gate-results", "/tmp/process-capture"):
    roots[root.rstrip("/")] = supervisor
# Installed dependencies are os-image, pinned by their lockfiles and bound by the root digest.
for path in sorted({Path(repo, "web", "node_modules"), *Path(repo).glob("node_modules"),
                    *Path(repo).glob("*/node_modules")}):
    if path.is_dir():
        roots[str(path)] = os_image
        if os.path.realpath(path) != str(path):
            roots[os.path.realpath(path)] = os_image
browsers = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or Path.home() / ".cache" / "ms-playwright")
if browsers.is_absolute() and browsers.is_dir():
    roots[str(browsers)] = os_image
    if os.path.realpath(browsers) != str(browsers):
        roots[os.path.realpath(browsers)] = os_image
strace_package = None
try:
    probe = subprocess.run(["dpkg-query", "-W", "-f", "${Version}", "strace"], stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30)
    if probe.returncode == 0 and probe.stdout.strip():
        strace_package = probe.stdout.decode("ascii", "strict").strip()
except (OSError, UnicodeError, subprocess.SubprocessError):
    pass
node_identity = None
node = shutil.which("node")
if node:
    try:
        version = subprocess.run([node, "--version"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, timeout=30, check=True).stdout.decode("ascii").strip()
        node_identity = {"executable": node, "version": version,
                         "executable_sha256": file_sha256(os.path.realpath(node))}
    except (OSError, UnicodeError, subprocess.SubprocessError):
        node_identity = None
distributions = sorted({(str(d.metadata["Name"] or ""), str(d.version or ""))
                        for d in importlib.metadata.distributions()})
helpers = {name: file_sha256(Path(out) / name)
           for name in ("pytest_selection.py", "full_run_manifest.py", "select_tests.py", "sitecustomize.py",
                        "vitest-leaf.mjs", "playwright-leaf.mjs", "trace_supervisor.py",
                        "trace_process_tree.py", "external_inventory.py", "epoch_probe.py") if (Path(out) / name).is_file()}
context = {
    "run_id": run_id, "source_sha": head, "source_tree": source_tree,
    "capture_sha": capture_sha, "catalog_sha256": catalog_sha,
    "source_root": repo, "initial_cwd": repo,
    "inventory": {"files": files, "symlinks": symlinks}, "external_roots": roots,
    "toolchain_fingerprint": full_run_manifest.toolchain_fingerprint(image, capture_sha),
    "helper_blobs": {"trace_reads_sha256": file_sha256(Path(out) / "trace_reads.py"),
                     "reporting_helper_sha256s": helpers},
    "strace_package": strace_package,
    # The provider exposes only the image label, never a manifest digest.
    "image_manifest_digest": None, "image_manifest_digest_reason": "os_image_identity_unavailable",
    "distribution_list_digest": tree.digest([list(row) for row in distributions]),
    "interpreter_identity": {"executable": sys.executable, "version": sys.version,
                             "executable_sha256": file_sha256(os.path.realpath(sys.executable))},
    "node_identity": node_identity,
}
# The supervisor's own check with the runner's per-suite fields; a context it would refuse is never written.
tree._validate(dict(context, capture_group="base", suites=[], suites_deferred=True,
                    seed_fds={"1": {"kind": "supervisor"}, "2": {"kind": "supervisor"}}))
context.update(capture_group="base", suites=[], suites_deferred=True,
               seed_fds={"0": {"kind": "devnull"}, "1": {"kind": "supervisor"}, "2": {"kind": "supervisor"}})
tree._atomic(Path("/tmp/process-capture/context.json"), context)
print("TRACING capture_base_context files={} symlinks={} roots={} strace_package={} node={} image_manifest_digest=null "
      "reason=os_image_identity_unavailable".format(len(files), len(symlinks), len(roots), strace_package,
                                                    (node_identity or {}).get("version")))
LEAF_CAPTURE_BASE
    then
      # -I implies -P, so a helper run as a script cannot import its siblings; load it from the dir.
      if python -I -B -c 'import sys; sys.path.insert(0, sys.argv.pop(1)); from epoch_probe import main; sys.exit(main())' \
          "$selection_dir" --context /tmp/process-capture/context.json \
          --out /tmp/process-capture/capture-epoch.json --sink file; then
        process_capture_active=1
        export LEAF_PROCESS_CAPTURE_CONTEXT=/tmp/process-capture/context.json
        export LEAF_PROCESS_CAPTURE_OUT=/tmp/process-capture/out
        # Reuse the advisory readset publication path; LEAF_PROOF_TRACING stays independent.
        tracing_ready=1
      else
        process_capture_reason=epoch_unavailable
      fi
    else
      process_capture_reason=context_unavailable
    fi
  fi
  if [[ -n "$process_capture_reason" ]]; then
    echo "LEAF_CAPTURE refused $process_capture_reason"
  fi
  export process_capture_active
fi
# LEAF_PROCESS_CAPTURE_END
if [[ "$tracing_ready" == 1 ]]; then
  export LEAF_READSET_DIR=/tmp/gate-logs/readsets
  export LEAF_READSET_RUN="${CODEBUILD_BUILD_ID:-}"
  export LEAF_READSET_ROOT="$CODEBUILD_SRC_DIR"
  export LEAF_READSET_SOURCE_SHA="$HEAD_SHA"
  export LEAF_READSET_SOURCE_TREE="$(git --no-replace-objects rev-parse "$HEAD_SHA^{tree}" 2>/dev/null || true)"
  export LEAF_READSET_CAPTURE_SHA="$(git --no-replace-objects rev-parse "$TRUSTED_SHA:scripts/ci/trace_reads.py" 2>/dev/null || true)"
  export LEAF_READSET_CATALOG_SHA256="$(python -I -B - "$selection_dir/catalog.json" <<'LEAF_CAPTURE_ID'
import json
import sys
try:
    print(json.load(open(sys.argv[1], encoding="utf-8")).get("catalog_sha256") or "")
except (OSError, ValueError):
    print("")
LEAF_CAPTURE_ID
)"
else
  unset "${!LEAF_READSET_@}"
  if [[ "$tracing_helpers_ready" == 1 && "$reporters_ready" == 1 ]]; then
    echo 'INFO: read-set tracing skipped (not a tracing build)' >&2
  fi
fi
if [[ "$tracing_helpers_ready" != 1 || "$reporters_ready" != 1 ]]; then
  echo 'WARNING: trusted capture unavailable; readsets and test reports remain incomplete' >&2
fi
if [[ -n "${dep_cache_publish_pid:-}" ]]; then
  wait "$dep_cache_publish_pid" || echo "LEAF_DEP_CACHE kind=dl action=publish result=error"
  cat "$dep_cache_publish_log" || true
  rm -f "$dep_cache_publish_log" || true
fi
if [[ "${CI_SHARD_MODE:-all}" == all || ${#only_args[@]} == 0 || "$CI_SHARD_INDEX_N" == 0 ]]; then
if [[ "${CI_SHARD_MODE:-all}" == shard && ${#only_args[@]} == 0 ]]; then
  only_args+=(--shard-count "$CI_SHARD_TOTAL_N" --shard-index "$CI_SHARD_INDEX_N")
fi
# Throwaway PostgreSQL so the db_gated and uses_database suites RUN instead of
# skipping. The runner hands LEAF_GATE_DATABASE_URL to those suites only, as
# their DATABASE_URL; an ambient DATABASE_URL would switch unrelated suites into
# PostgreSQL mode, so none is exported. LEAF_GATE_REQUIRE_DATABASE=1 makes an
# unreachable database a FAIL row per suite, and a server that never started
# fails the build below even when no database suite was selected.
echo "LEAF_T start postgres $(date +%s%3N)"
postgres_status=0
trap 'bash "$CODEBUILD_SRC_DIR/scripts/ci/start_ephemeral_postgres.sh" stop || true' EXIT
if ! LEAF_GATE_DATABASE_URL="$(bash "$CODEBUILD_SRC_DIR/scripts/ci/start_ephemeral_postgres.sh" start)"; then
  postgres_status=1
  LEAF_GATE_DATABASE_URL=""
  echo "FATAL: ephemeral PostgreSQL did not start; every PostgreSQL suite fails by name and this build fails" >&2
fi
export LEAF_GATE_DATABASE_URL
export LEAF_GATE_REQUIRE_DATABASE=1
export LEAF_CI_POSTGRES_SETUP_STATUS="$postgres_status"
echo "LEAF_T end postgres $(date +%s%3N) rc=$postgres_status"
# LEAF_DIAGNOSTIC_BEGIN
# Server-owned exact-suite diagnostics never grant a full or shard verdict.
if [[ "${LEAF_DIAGNOSTIC_PROFILE:-}" == web-demo-suite && "${LEAF_GATE_ONLY:-}" =~ ^[a-z0-9][a-z0-9-]{0,63}$ ]]; then
  unset PYTHONSAFEPATH
  export PYTHONUNBUFFERED=1
  diagnostic_status=2
  echo "LEAF_DIAGNOSTIC diagnostic-only/no-full-verdict suite=$LEAF_GATE_ONLY"
  if python scripts/run-all-gates.py --list --catalog-root "$CODEBUILD_SRC_DIR" > "$selection_dir/diagnostic-catalog.json" \
    && python -I -B - "$selection_dir/diagnostic-catalog.json" "$LEAF_GATE_ONLY" <<'LEAF_DIAGNOSTIC_EXACT'
import json
import sys

try:
    with open(sys.argv[1], encoding="utf-8") as source:
        catalog = json.load(source)
    if catalog.get("schema") != "leaf.ci.catalog.v1" or not isinstance(catalog.get("suites"), list):
        raise ValueError("invalid catalog")
    ids = [row["id"] for row in catalog["suites"]]
    if any(not isinstance(suite_id, str) for suite_id in ids):
        raise ValueError("invalid suite ID")
    if [suite_id for suite_id in ids if sys.argv[2] in suite_id] != [sys.argv[2]]:
        raise ValueError("--only must select exactly the requested suite ID")
except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
    print("LEAF_DIAGNOSTIC refused: " + str(exc)[:300], file=sys.stderr)
    raise SystemExit(2)
LEAF_DIAGNOSTIC_EXACT
  then
    diagnostic_status=0
    python scripts/run-all-gates.py --jobs 1 --retry 0 --fail-fast --early-feedback --result-json /tmp/gate-results/gate-result.json --log-dir /tmp/gate-logs --only "$LEAF_GATE_ONLY" --before-heavy-command bash "$LEAF_CI_SCRIPT_PATH" --heavy-prerequisites || diagnostic_status=$?
  else
    echo "LEAF_DIAGNOSTIC refused: catalog unavailable or selection not exact" >&2
  fi
  if [[ "$postgres_status" != 0 && "$diagnostic_status" == 0 ]]; then
    echo "FATAL: diagnostic suite passed without its PostgreSQL proofs; failing the diagnostic" >&2
    diagnostic_status=1
  fi
  printf 'LEAF_DIAGNOSTIC diagnostic-only/no-full-verdict suite=%s status=%s postgres_status=%s\n' \
    "$LEAF_GATE_ONLY" "$diagnostic_status" "$postgres_status" | tee /tmp/gate-results/diagnostic.txt || true
  # NO_SOURCE builds have no artifact download. Publish only sanitized retained evidence.
  python -I -B scripts/ci/retain_failed_gate_logs.py \
    --result /tmp/gate-results/gate-result.json --logs /tmp/gate-logs --output /tmp/gate-results/diagnostic-failed-gate-logs \
    --emit-stdout --source "$HEAD_SHA" --build-id "${CODEBUILD_BUILD_ID:-}" \
    || echo 'WARNING: failed diagnostic log publication unavailable' >&2
  exit "$diagnostic_status"
fi
# LEAF_DIAGNOSTIC_END
gate_status=0
gate_fail_fast_args=(--fail-fast)
if [[ "${process_capture_active:-0}" == 1 ]]; then
  # Capture records every suite's reads; a failure still fails the job below.
  gate_fail_fast_args=()
fi
gate_tier_args=()
if [[ "$pr_tier_active" == 1 ]]; then
  gate_tier_args=(--tier-file scripts/ci/pr-tier.json)
fi
echo "LEAF_EVENT gate fail_fast=${#gate_fail_fast_args[@]}" >&2
unset PYTHONSAFEPATH
# Ensure gate rows reach the log even when the job is killed at its deadline.
export PYTHONUNBUFFERED=1
python scripts/run-all-gates.py --jobs "${LEAF_GATE_JOBS:-auto}" --retry 0 --early-feedback "${gate_fail_fast_args[@]}" --result-json /tmp/gate-results/gate-result.json --log-dir /tmp/gate-logs "${only_args[@]}" "${gate_tier_args[@]}" --before-heavy-command bash "$LEAF_CI_SCRIPT_PATH" --heavy-prerequisites || gate_status=$?
python -I -B scripts/ci/retain_failed_gate_logs.py \
  --result /tmp/gate-results/gate-result.json --logs /tmp/gate-logs --output /tmp/gate-results/failed-gate-logs \
  --emit-stdout --source "$HEAD_SHA" --build-id "${CODEBUILD_BUILD_ID:-}" \
  || echo 'WARNING: failed gate log retention unavailable' >&2
if [[ "$postgres_status" != 0 && "$gate_status" == 0 ]]; then
  echo "FATAL: the gate passed without its PostgreSQL proofs; failing the build" >&2
  gate_status=1
fi
else
  echo "gate: selected mode runs on shard 0 only"
  gate_status=0
fi
echo "LEAF_T end gate $(date +%s%3N) rc=$gate_status"
# Reuse evidence (plan ci-test-selection-20260925 S1): the shard's own result, immutable and advisory.
if [[ -n "${CODEBUILD_BUILD_ID:-}" && -f /tmp/gate-results/gate-result.json ]]; then
  aws s3api put-object --bucket leaf-mq-transport-807034087062-us-east-1 \
    --key "mq/leaf-web-demo/selection/${CODEBUILD_BUILD_ID}.result.json" \
    --body /tmp/gate-results/gate-result.json \
    --content-type application/json --if-none-match '*' >/dev/null \
    || echo 'WARNING: result_write_failed; reuse evidence absent for this build' >&2
fi
# Reuse donor pointer (plan ci-test-selection-20260925 S1b): only a green, full, main-push shard names itself.
if [[ -n "${CODEBUILD_BUILD_ID:-}" && "${CODEBUILD_WEBHOOK_HEAD_REF:-}" == refs/heads/main \
      && "${CODEBUILD_WEBHOOK_EVENT:-}" == PUSH && "$selection_mode" == full \
      && "$trusted_sha_override" != 1 && "$gate_status" == 0 && "$postgres_status" == 0 \
      && "$HEAD_SHA" =~ ^[0-9a-f]{40}$ ]]; then
  printf '{"build_id":"%s","schema":"leaf.ci.main-shard-pointer.v1","sha":"%s","shard_index":%d,"shard_total":%d}\n' \
    "$CODEBUILD_BUILD_ID" "$HEAD_SHA" "$CI_SHARD_INDEX_N" "$CI_SHARD_TOTAL_N" > "$selection_dir/main-shard-pointer.json"
  aws s3api put-object --bucket leaf-mq-transport-807034087062-us-east-1 \
    --key "mq/leaf-web-demo/selection/main/${HEAD_SHA}/shard-${CI_SHARD_INDEX_N}-of-${CI_SHARD_TOTAL_N}.json" \
    --body "$selection_dir/main-shard-pointer.json" \
    --content-type application/json --if-none-match '*' >/dev/null \
    || echo 'WARNING: pointer_write_failed; this shard is not a reuse donor' >&2
fi
# Sanitized retained-log frames above replace raw result/log tails and note dumps.
if [[ ! -f /tmp/gate-results/gate-result.json ]]; then
  echo "No gate result JSON was written (runner exit $gate_status)"
fi
if [[ "${CI_SHARD_MODE:-all}" == all || "$CI_SHARD_INDEX_N" == 0 ]]; then
echo "LEAF_T start change-impact $(date +%s%3N)"
echo "=== job change-impact ==="
# Advisory in S1: prints the assessment, never changes gate_status. Kill switch honoured.
python scripts/ci/change_impact_job.py --repo . --head "${CODEBUILD_RESOLVED_SOURCE_VERSION:-HEAD}" \
  --base-ref "${CODEBUILD_WEBHOOK_BASE_REF:-}" --head-ref "${CODEBUILD_WEBHOOK_HEAD_REF:-}" \
  --event "${CODEBUILD_WEBHOOK_EVENT:-manual}" --gate-result /tmp/gate-results/gate-result.json \
  --receipt-dir /tmp/impact || echo "change-impact: helper exit $? (advisory)"
echo "LEAF_T end change-impact $(date +%s%3N) rc=0"
else
  echo "change-impact: runs on shard 0"
fi
# Read-set publication is advisory and only runs after a traced gate.
# Compute completeness and the full-run binding before packing any evidence.
manifest_failed=0
if ! python -I -B - "$selection_dir" "$gate_status" "$tracing_ready" "$reporters_ready" "$trusted_sha_override" "$loader_check" \
  "${CODEBUILD_BUILD_ID:-}" "${HEAD_SHA:-}" "${LEAF_READSET_SOURCE_TREE:-}" "${LEAF_READSET_CAPTURE_SHA:-}" "${CODEBUILD_BUILD_IMAGE:-}" <<'LEAF_SELECTION_FINALIZE'
import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from urllib.parse import quote

out = Path(sys.argv[1])
canonical = lambda value: json.dumps(value, sort_keys=True, separators=(",", ":"))
try:
    detail = json.loads((out / "detail.json").read_text(encoding="utf-8"))
except (OSError, ValueError):
    detail = {"schema": "leaf.ci.selection.v1", "execution_mode": "full",
              "fallback_reason": "receipt_prepare_failed"}
# LEAF_TIER_RESULT_BEGIN
try:
    gate_result = json.loads(Path("/tmp/gate-results/gate-result.json").read_text(encoding="utf-8"))
except (OSError, ValueError):
    gate_result = {}
if gate_result.get("mode") == "tier":
    detail.update(execution_mode="selected", selection_mode="selected", selection_source="pr-tier", apply_filter=False,
                  executed_suite_ids=gate_result["suite_ids"], expanded_suite_ids=gate_result["suite_ids"])
# LEAF_TIER_RESULT_END
records = []
raw = b""
expected = set(detail.get("executed_suite_ids", []))
attempt_rows_invalid = 0
attempt_rows_rejected = 0
for path in sorted(Path("/tmp/gate-logs/attempts").glob("*.jsonl")):
    try:
        data = path.read_bytes()
    except OSError:
        attempt_rows_invalid += 1
        continue
    for line in data.splitlines():
        try:
            row = json.loads(line)
        except (ValueError, UnicodeError):
            attempt_rows_invalid += 1
            continue
        if (not isinstance(row, dict) or row.get("run_id") != sys.argv[7] or
                not isinstance(row.get("suite_id"), str) or row["suite_id"] not in expected):
            attempt_rows_rejected += 1
            continue
        raw += line + b"\n"
        if (not isinstance(row.get("test_ids", []), list) or
                not isinstance(row.get("failed_test_ids", []), list) or
                any(not isinstance(tid, str) for tid in row.get("test_ids", []) + row.get("failed_test_ids", []))):
            attempt_rows_invalid += 1
            continue
        records.append(row)
valid = attempt_rows_invalid == 0
attempts_path = Path("/tmp/gate-results/selection-attempts.jsonl")
attempts_path.write_bytes(raw)
first = [row for row in records if row.get("attempt") == 1]
observed = {row.get("suite_id") for row in first}
all_ids = sorted({tid for row in first for tid in row.get("test_ids", [])})
failed = sorted({tid for row in first for tid in row.get("failed_test_ids", [])})
selected = sorted(set(detail.get("selected_test_ids", [])))
reporting = (valid and bool(expected) and observed == expected and len(first) == len(expected)
             and all(row.get("test_report_complete") is True for row in first))
def skipped_by_gate(row):
    return (row.get("status") == "SKIP" and row.get("skipped_by_gate") in ("db_gated", "opt_in_env")
            and row.get("test_report_complete") is True and row.get("test_ids") == []
            and row.get("test_id_granularity") in ("test", "suite"))

suites_skipped_by_gate = sorted({row["suite_id"] for row in first if skipped_by_gate(row)})
complete = reporting and all(row.get("status") in ("PASS", "FAIL") or skipped_by_gate(row) for row in first)
completeness_reasons = []
if not valid:
    completeness_reasons.append(f"attempt_rows_invalid:{attempt_rows_invalid}")
if not expected:
    completeness_reasons.append("expected_suites_empty")
if expected - observed:
    completeness_reasons.append(f"observed_suites_missing:{len(expected - observed)}")
if observed - expected:
    completeness_reasons.append(f"observed_suites_extra:{len(observed - expected)}")
if len(first) != len(expected):
    completeness_reasons.append(f"first_attempt_rows_mismatch:{len(first)}/{len(expected)}")
def suite_reason(reason, suite_ids):
    text = f"{reason}:{len(suite_ids)}:" + ",".join(sorted(suite_ids))
    return text if len(text) <= 400 else text[:396] + ",..."

incomplete_reports = {row["suite_id"] for row in first if row.get("test_report_complete") is not True}
if incomplete_reports:
    completeness_reasons.append(suite_reason("test_report_incomplete", incomplete_reports))
nonfinal = {row["suite_id"] for row in first
            if row.get("status") not in ("PASS", "FAIL") and not skipped_by_gate(row)}
if nonfinal:
    completeness_reasons.append(suite_reason("suite_status_not_final", nonfinal))
if detail.get("execution_mode") != "full":
    completeness_reasons.append("execution_mode:" + str(detail.get("execution_mode")))
rejected_shards = 0
if sys.argv[3] == "1":
    sys.path.insert(0, str(out))
    import full_run_manifest
    if os.environ.get("process_capture_active") == "1":
        full_run_manifest.stage_process_capture("/tmp/process-capture/out", Path("/tmp/gate-logs/readsets"), out)
    packed_catalog = json.loads((out / "catalog.json").read_text(encoding="utf-8"))
    rejected_shards = full_run_manifest.partition_readsets(
        Path("/tmp/gate-logs/readsets"), out / "readsets-rejected", packed_catalog, sys.argv[7])
    (out / "readsets-partitioned").write_text("ready\n", encoding="ascii")
    collection_ids_by_suite = {}
    reports = out / "reports"
    reports.mkdir(exist_ok=True)
    for suite in packed_catalog.get("suites", []):
        sid = suite["id"]
        encoded = quote(sid, safe="").replace(".", "%2E") or "%00"
        directory = Path("/tmp/gate-logs/test-reports") / encoded
        collections = []
        for pattern in ("*/collection-*.json", "*/completion-*.json", "*/tests-*.json"):
            for path in sorted(directory.glob(pattern)):
                if sid in expected:
                    destination = reports / encoded / path.parent.name / path.name
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(path, destination)
                if path.name.startswith("collection-"):
                    doc = json.loads(path.read_text(encoding="utf-8"))
                    ids = doc["test_ids"]
                    if not isinstance(ids, list) or any(not isinstance(tid, str) or not tid for tid in ids):
                        raise ValueError("invalid_collection_ids:" + sid)
                    collections.append(sorted(set(ids)))
        if collections and any(ids != collections[0] for ids in collections):
            completeness_reasons.append("collection_ids_differ:" + sid)
            reporting = complete = False
        elif collections and collections[0]:
            collection_ids_by_suite[sid] = [sid + "::" + tid for tid in collections[0]]
    (out / "collection-ids.json").write_text(canonical(collection_ids_by_suite) + "\n", encoding="utf-8")
detail.update(execution_complete=complete, test_exit_code=int(sys.argv[2]),
              trusted_sha_override=sys.argv[5] == "1", loader_check=sys.argv[6],
              tracing_active=sys.argv[3] == "1", reporters_active=sys.argv[4] == "1",
              build_exit_code=int(sys.argv[2]), collection_complete=reporting,
              collection_ids_sha256=hashlib.sha256(canonical(all_ids).encode("utf-8")).hexdigest(),
              attempts_ref=str(attempts_path), attempts_sha256=hashlib.sha256(raw).hexdigest(),
              test_id_reporting_complete=reporting,
              completeness_reasons=sorted(completeness_reasons),
              suites_skipped_by_gate=suites_skipped_by_gate,
              attempt_rows_rejected=attempt_rows_rejected,
              readsets_rejected_shards=rejected_shards,
              full_run_complete=complete and detail.get("execution_mode") == "full",
              finished_at=datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"))
(out / "detail.json").write_text(canonical(detail) + "\n", encoding="utf-8")
if detail.get("phase") == "shadow":
    shadow = {"id": detail.get("build_id"), "sha": detail.get("head_sha"),
              "trusted_sha_override": detail["trusted_sha_override"],
              "loader_check": detail["loader_check"],
              "selector_sha": detail.get("selector_sha"), "map_sha": detail.get("map_sha"),
              "selected_ids": len(selected), "full_failed_ids": failed,
              "contained": set(failed) <= set(selected), "first_attempt": True,
              "selected_test_ids": selected, "selected_suite_ids": detail.get("expanded_suite_ids", []),
              "catalog_sha256": detail.get("runner_catalog_sha256"),
              "collection_ids_sha256": detail["collection_ids_sha256"],
              "attempts_ref": detail["attempts_ref"], "attempts_sha256": detail["attempts_sha256"],
              "full_run_complete": detail["full_run_complete"],
              "completeness_reasons": detail["completeness_reasons"],
              "suites_skipped_by_gate": detail["suites_skipped_by_gate"],
              "attempt_rows_rejected": detail["attempt_rows_rejected"],
              "tracing_active": detail["tracing_active"], "reporters_active": detail["reporters_active"],
              "test_id_reporting_complete": reporting, "synthetic": False,
              "fallback_reason": detail.get("fallback_reason"),
              "assigned_arm": detail.get("assigned_arm"), "execution_mode": detail.get("execution_mode")}
    (out / "shadow.json").write_text(canonical(shadow) + "\n", encoding="utf-8")
if sys.argv[3] == "1" and detail.get("selection_source") != "pr-tier":
    helper = out / "full_run_manifest.py"
    if not helper.is_file() or not helper.stat().st_size:
        raise ValueError("manifest_helper_unavailable")
    inputs = {"repo": "leaf-web-demo", "run_id": sys.argv[7], "source_sha": sys.argv[8],
              "source_tree": sys.argv[9], "capture_sha": sys.argv[10], "image": sys.argv[11],
              "execution_mode": detail["execution_mode"],
              "full_run_complete": detail["full_run_complete"],
              "test_id_reporting_complete": detail["test_id_reporting_complete"]}
    capture_args = (["--reports", str(out / "reports")]
                    if os.environ.get("process_capture_active") == "1" else [])
    result = subprocess.run([sys.executable, "-I", "-B", str(helper),
                             "--catalog", str(out / "catalog.json"),
                             "--readsets", "/tmp/gate-logs/readsets",
                             "--collection", str(out / "collection-ids.json"),
                             "--output", str(out / "full-run.json")] + capture_args,
                            input=canonical(inputs), text=True)
    if result.returncode == 0 and not (out / "full-run.json").is_file():
        raise ValueError("manifest_output_missing")
    raise SystemExit(result.returncode)
LEAF_SELECTION_FINALIZE
then
  manifest_failed=1
  echo 'WARNING: selection evidence finalization or full-run manifest failed' >&2
fi
readsets_object=""
readsets_sha256=""
readsets_bytes=""
readsets_status=not_traced
readsets_archive_members=""
if [[ "$tracing_ready" == 1 ]]; then
  publish_readsets() {
    local archive="$selection_dir/readsets.tar.gz" entry digest build_uuid key member
    local -a selection_members=()
    readsets_status=empty
    readsets_error="read-set partition failed"
    [[ -f "$selection_dir/readsets-partitioned" ]] || return 1
    [[ -d /tmp/gate-logs/readsets ]] || return 0
    readsets_error="directory scan failed"
    entry="$(find /tmp/gate-logs/readsets -type f -print -quit)" || return 1
    [[ -n "$entry" || "${process_capture_active:-0}" == 1 ]] || return 0
    readsets_error="archive creation failed"
    for member in catalog.json decision.json full-run.json; do
      [[ ! -f "$selection_dir/$member" ]] || selection_members+=("$member")
    done
    # Name each document once, including the outcome streams shards reference.
    local -a attempt_members=() shard_members=() report_members=()
    while IFS= read -r -d '' member; do
      attempt_members+=("${member#/tmp/gate-logs/}")
    done < <(find /tmp/gate-logs/readsets -type f -name 'attempts*.jsonl' -print0)
    while IFS= read -r -d '' member; do
      shard_members+=("${member#/tmp/gate-logs/}")
    done < <(find /tmp/gate-logs/readsets -type f ! -name 'attempts*.jsonl' -print0)
    if [[ -d "$selection_dir/reports" ]]; then
      report_members+=(reports)
      while IFS= read -r -d '' member; do
        report_members+=("${member#"$selection_dir/"}")
      done < <(find "$selection_dir/reports" -type f \( -name 'collection-*.json' -o -name 'completion-*.json' -o -name 'tests-*.json' \) -print0 | sort -z)
    fi
    local -a capture_members=() diagnostic_members=()
    if [[ "${process_capture_active:-0}" == 1 ]]; then
      readsets_error="capture manifest failed"
      [[ "$manifest_failed" == 0 && -f "$selection_dir/full-run.json" ]] || return 1
      selection_members+=(capture-suites.json)
      # Test reports remain available in ordinary archives; capture packs only map evidence.
      report_members=()
      while IFS= read -r -d '' member; do
        report_members+=("${member#"$selection_dir/"}")
      done < <(find "$selection_dir/reports" -type f \( -name 'collection-*.json' -o -name 'completion-*.json' \) -print0 | sort -z)
      while IFS= read -r -d '' member; do
        capture_members+=("${member#"$selection_dir/"}")
      done < <(find "$selection_dir/capture" -type f -name '*.json' -print0 | sort -z)
      while IFS= read -r -d '' member; do
        report_members+=("${member#"$selection_dir/"}")
      done < <(find "$selection_dir/reports" -maxdepth 1 -type f \( -name 'process-tree-*.json' -o -name 'trace-receipt-*.json' \) -print0 | sort -z)
      if [[ -d /tmp/gate-logs/diagnostics ]]; then
        while IFS= read -r -d '' member; do
          diagnostic_members+=("${member#/tmp/gate-logs/}")
        done < <(find /tmp/gate-logs/diagnostics -type f -print0 | sort -z)
      fi
      readsets_error="capture reference validation failed"
      python -I -B - "$selection_dir" /tmp/gate-logs "${selection_members[@]}" "${report_members[@]}" "${capture_members[@]}" -- "${shard_members[@]}" "${attempt_members[@]}" <<'LEAF_CAPTURE_REFERENCES' || return 1
import json
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import full_run_manifest
boundary = sys.argv.index("--", 3)
members = {name: Path(sys.argv[1]) / name for name in sys.argv[3:boundary]}
members.update({name: Path(sys.argv[2]) / name for name in sys.argv[boundary + 1:]})
full_run_manifest.validate_package({name: path for name, path in members.items() if path.is_file()})
inventory = {"readsets": 0}
for name, path in members.items():
    if path.is_file():
        top = name.split("/", 1)[0]
        inventory[top] = inventory.get(top, 0) + path.stat().st_size
print("LEAF_CAPTURE inventory " + json.dumps(inventory, sort_keys=True))
LEAF_CAPTURE_REFERENCES
    fi
    tar -czf "$archive" --no-recursion -C /tmp/gate-logs readsets -C "$selection_dir" "${selection_members[@]}" "${report_members[@]}" "${capture_members[@]}" -C /tmp/gate-logs "${shard_members[@]}" "${attempt_members[@]}" 2>/dev/null || return 1
    readsets_archive_members="readsets ${selection_members[*]} ${report_members[*]} ${attempt_members[*]}"
    if [[ "${process_capture_active:-0}" == 1 ]]; then
      readsets_archive_members+=" ${capture_members[*]}"
    fi
    readsets_error="archive measurement failed"
    readsets_bytes="$(wc -c < "$archive")" || return 1
    readsets_bytes="${readsets_bytes//[[:space:]]/}"
    digest="$(sha256sum "$archive")" || return 1
    readsets_sha256="${digest%% *}"
    if [[ "${process_capture_active:-0}" == 1 && "${CODEBUILD_BUILD_ID:-}" =~ ^device:dv-[0-9a-f]{16}$ ]]; then
      if mkdir -p /tmp/process-capture \
        && cp "$archive" /tmp/process-capture/readsets.tar.gz \
        && python -I -B - /tmp/process-capture/readsets.json "$CODEBUILD_BUILD_ID" "$HEAD_SHA" \
          "$readsets_bytes" "$readsets_sha256" "$readsets_archive_members" <<'LEAF_DEVICE_CAPTURE_ARCHIVE'
import json
from pathlib import Path
import sys

receipt = {"schema": "leaf.ci.device-capture-archive.v1", "build_id": sys.argv[2],
           "head_sha": sys.argv[3], "bytes": int(sys.argv[4]), "sha256": sys.argv[5],
           "members": sys.argv[6].split()}
Path(sys.argv[1]).write_text(json.dumps(receipt, sort_keys=True) + "\n", encoding="utf-8")
LEAF_DEVICE_CAPTURE_ARCHIVE
      then
        echo "LEAF_CAPTURE device_archive $readsets_bytes $readsets_sha256"
      else
        echo 'LEAF_CAPTURE device_archive_copy_failed'
      fi
    fi
    local archive_cap=209715200
    if [[ "${process_capture_active:-0}" == 1 ]]; then
      archive_cap=1073741824
      echo "LEAF_CAPTURE archive_bytes $readsets_bytes"
    fi
    if (( readsets_bytes > archive_cap )); then
      readsets_status=too_large
      if [[ "${process_capture_active:-0}" == 1 ]]; then
        echo "LEAF_CAPTURE package_error archive_too_large"
      fi
      return 0
    fi
    readsets_error="missing build id"
    [[ -n "${CODEBUILD_BUILD_ID:-}" ]] || return 1
    build_uuid="${CODEBUILD_BUILD_ID#*:}"
    key="mq/leaf-web-demo/selection/${build_uuid}.readsets.tar.gz"
    aws s3api put-object --bucket leaf-mq-transport-807034087062-us-east-1 \
      --key "$key" --body "$archive" --if-none-match '*' --checksum-algorithm SHA256 \
      --metadata "build_id=${CODEBUILD_BUILD_ID},head_sha=${HEAD_SHA},trusted_sha=${TRUSTED_SHA},trusted_sha_override=${trusted_sha_override}" \
      >/dev/null 2>&1 || { readsets_error="put-object exit $?"; return 1; }
    readsets_object="s3://leaf-mq-transport-807034087062-us-east-1/$key"
    readsets_status=uploaded
    if [[ "${process_capture_active:-0}" == 1 && ${#diagnostic_members[@]} -gt 0 ]]; then
      local diagnostics_archive="$selection_dir/capture-diagnostics.tar.gz" diagnostics_bytes
      if tar -czf "$diagnostics_archive" --no-recursion -C /tmp/gate-logs "${diagnostic_members[@]}" 2>/dev/null; then
        diagnostics_bytes="$(wc -c < "$diagnostics_archive")"
        diagnostics_bytes="${diagnostics_bytes//[[:space:]]/}"
        if (( diagnostics_bytes > 209715200 )); then
          echo "LEAF_CAPTURE diagnostics_skipped too_large $diagnostics_bytes"
        else
          aws s3api put-object --bucket leaf-mq-transport-807034087062-us-east-1 \
            --key "mq/leaf-web-demo/selection/${build_uuid}.capture-diagnostics.tar.gz" \
            --body "$diagnostics_archive" --if-none-match '*' --checksum-algorithm SHA256 \
            --metadata "build_id=${CODEBUILD_BUILD_ID},head_sha=${HEAD_SHA},trusted_sha=${TRUSTED_SHA},trusted_sha_override=${trusted_sha_override}" \
            >/dev/null 2>&1 || echo 'LEAF_CAPTURE diagnostics_skipped upload_failed'
        fi
      else
        echo 'LEAF_CAPTURE diagnostics_skipped archive_failed'
      fi
    fi
  }
  if ! publish_readsets; then
    readsets_status=upload_failed
    echo "WARNING: readsets upload failed ($readsets_error)" >&2
    if [[ "${process_capture_active:-0}" == 1 ]]; then
      echo "LEAF_CAPTURE package_error $readsets_error"
    fi
  fi
fi
if [[ "$tracing_ready" == 1 && "$manifest_failed" == 1 ]]; then
  readsets_status=manifest_failed
fi
export readsets_object readsets_sha256 readsets_bytes readsets_status readsets_archive_members
python -I -B - "$selection_dir" <<'LEAF_SELECTION_PUBLISHED' || echo 'WARNING: selection evidence finalization failed' >&2
import json
import os
from pathlib import Path
import sys

out = Path(sys.argv[1])
canonical = lambda value: json.dumps(value, sort_keys=True, separators=(",", ":"))
detail = json.loads((out / "detail.json").read_text(encoding="utf-8"))
publication = {"readsets_object": os.environ.get("readsets_object") or None,
               "readsets_sha256": os.environ.get("readsets_sha256") or None,
               "readsets_bytes": int(os.environ["readsets_bytes"]) if os.environ.get("readsets_bytes") else None,
               "readsets_status": os.environ["readsets_status"],
               "readsets_rejected_shards": detail.get("readsets_rejected_shards", 0),
               "readsets_archive_members": os.environ["readsets_archive_members"].split()}
detail.update(publication)
(out / "detail.json").write_text(canonical(detail) + "\n", encoding="utf-8")
print("LEAF_SELECTION_FINAL " + canonical(detail))
if detail.get("phase") == "shadow":
    shadow = json.loads((out / "shadow.json").read_text(encoding="utf-8"))
    shadow.update(publication)
    print("LEAF_SHADOW " + canonical(shadow))
LEAF_SELECTION_PUBLISHED
# LEAF_GATE_PROOF_BEGIN
if [[ "${CI_SHARD_MODE:-all}" == shard ]]; then
  echo 'INFO: gate proof skipped in sharded mode; build verdict unchanged'
else
# Reuse only this run's result. Proof publication is advisory to the CI verdict.
# Eligibility follows what executed (full, unfiltered, no trusted-SHA override), never the selector's phase.
if [[ "$gate_status" == 0 ]] && python -I -B - "$selection_dir/detail.json" <<'LEAF_GATE_PROOF_ELIGIBLE'
import json
import sys
try:
    with open(sys.argv[1], encoding="utf-8") as source:
        receipt = json.load(source)
    eligible = (receipt.get("schema") == "leaf.ci.selection.v1"
                and receipt.get("execution_mode") == "full"
                and receipt.get("apply_filter") is False
                and receipt.get("trusted_sha_override") is False)
except (OSError, ValueError, AttributeError):
    eligible = False
raise SystemExit(0 if eligible else 1)
LEAF_GATE_PROOF_ELIGIBLE
then
  gate_proof="$selection_dir/gate-proof.json"
  if gate_tree="$(git rev-parse 'HEAD^{tree}')" \
    && python scripts/run-all-gates.py --verify-shard-results /tmp/gate-results --emit-proof "$gate_proof" \
    && python scripts/run-all-gates.py --verify-gate-proof "$gate_proof" --expect-tree "$gate_tree"; then
    if [[ -n "${CODEBUILD_BUILD_ID:-}" ]]; then
      if aws s3api put-object --bucket leaf-mq-transport-807034087062-us-east-1 \
        --key "mq/leaf-web-demo/selection/gate-proof/${gate_tree}.json" --body "$gate_proof" \
        --content-type application/json --if-none-match '*' --checksum-algorithm SHA256 \
        --metadata "producer-build-id=${CODEBUILD_BUILD_ID},producer-project=leaf-ci-leaf-web-demo,tree=${gate_tree}" \
        > "$selection_dir/gate-proof-put.log" 2>&1; then
        echo 'INFO: gate proof published'
      elif grep -Eq '412|PreconditionFailed' "$selection_dir/gate-proof-put.log"; then
        echo 'INFO: gate proof already exists; immutable object retained'
      else
        echo 'WARNING: gate proof put failed; build verdict unchanged' >&2
      fi
    else
      echo 'WARNING: gate proof put skipped without build identity; build verdict unchanged' >&2
    fi
  else
    echo 'WARNING: gate proof mint or verification failed; build verdict unchanged' >&2
  fi
fi
fi
# LEAF_PROCESS_CAPTURE_SUMMARY_BEGIN
if [[ "${process_capture_active:-0}" == 1 ]]; then
  if [[ "$manifest_failed" == 1 ]]; then
    echo 'LEAF_CAPTURE package_error manifest_failed'
  fi
  python -I -B - /tmp/process-capture/out "${readsets_bytes:-0}" /tmp/gate-logs/attempts <<'LEAF_CAPTURE_SUMMARY' || true
import json
from pathlib import Path
import sys
def summarize():
    receipts, certificates, errors, wrapped = [], 0, 0, 0
    for path in Path(sys.argv[1]).glob("*/*/reports/trace-receipt-*.json"):
        try:
            receipts.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            errors += 1
    certificates = sum(1 for _ in Path(sys.argv[1]).glob("*/*/reports/process-tree-*.json"))
    for path in Path(sys.argv[3]).glob("*.jsonl"):
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
                if "process_capture_exit_status" in row or row.get("process_capture_errors"):
                    wrapped += 1
                    if not row.get("process_capture_receipt"):
                        errors += len(row.get("process_capture_errors") or [])
            except ValueError:
                errors += 1
    print("LEAF_CAPTURE summary " + json.dumps(dict(mode="process", suites_wrapped=wrapped,
          receipts=len(receipts), certificates=certificates,
          capture_errors=errors + sum(len(receipt.get("capture_errors") or []) for receipt in receipts),
          archive_bytes=int(sys.argv[2])), separators=(",", ":")))
try:
    summarize()
except Exception as exc:
    print("LEAF_CAPTURE package_error summary_failed:" + type(exc).__name__)
LEAF_CAPTURE_SUMMARY
fi
# LEAF_PROCESS_CAPTURE_SUMMARY_END
# LEAF_GATE_PROOF_END
exit "$gate_status"
