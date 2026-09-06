"""Executable contract for the merge-queue group controller (slice C).

mq-review's pagination and per-member status gate are EXECUTED here against
the real step bodies with a fake `gh`, because a two-member fixture where one
lacks a passing `kimi-critic-review` status is exactly the case a text
assertion would never catch, and neither is a queue read that only resolves
correctly once two GraphQL pages are combined. mq-supply's docs-only recompute
is likewise executed against a real git repository, the same pattern
scripts/test_build_platform_images_workflow.py and
scripts/test_prewarm_staging_cutover_workflow.py use for their own
git-derived decisions.

The remaining assertions pin properties with no local executable surface:
GraphQL/REST shapes that only resolve against live GitHub state, the
cross-repository terraform receipt checks, the concurrency key, the
permission and secret boundary, and the fail-closed structure of mq-prewarm.
"""

from __future__ import annotations

import json
import base64
import os
from pathlib import Path
import re
import shlex
import subprocess
import textwrap
import zipfile

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "merge-queue.yml"


def _usable_bash() -> str:
    """A bash that can itself run jq, unzip and git.

    Probing shutil.which alone would let these tests run against a Windows
    WSL bash shim whose own PATH cannot see jq or git.
    """
    candidates = ["bash"]
    if os.name == "nt":
        candidates.append("C:/Program Files/Git/bin/bash.exe")
    for candidate in candidates:
        try:
            subprocess.run(
                [candidate, "-c", "command -v jq >/dev/null && command -v git >/dev/null"],
                check=True,
                capture_output=True,
                timeout=30,
            )
        except Exception:
            continue
        return candidate
    return ""


BASH = _usable_bash()

needs_shell = pytest.mark.skipif(
    not BASH,
    reason="no bash that can run jq and git (CI always has one)",
)


def workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def workflow_document() -> dict:
    # BaseLoader keeps the literal `on` key instead of YAML 1.1's boolean True,
    # and keeps every scalar (including booleans) as a string, which is why
    # the tests below compare against "true"/"false" strings, not Python bools.
    return yaml.load(workflow_text(), Loader=yaml.BaseLoader)


def job_steps(job: str) -> list:
    return workflow_document()["jobs"][job]["steps"]


def step_body(job: str, name_fragment: str) -> str:
    for step in job_steps(job):
        if name_fragment in step.get("name", "") and "run" in step:
            return step["run"]
    raise AssertionError("no %r step with a run body in job %s" % (name_fragment, job))


def step_by_name(job: str, name_fragment: str) -> dict:
    for step in job_steps(job):
        if name_fragment in step.get("name", ""):
            return step
    raise AssertionError("no %r step in job %s" % (name_fragment, job))


def run_step(body: str, workdir: Path, env: dict) -> dict:
    """Run one step body and return its $GITHUB_OUTPUT as a dict."""
    output = workdir / "step-output.txt"
    output.write_text("", encoding="utf-8")
    exports = {
        "GITHUB_OUTPUT": "step-output.txt",
        "GITHUB_REPOSITORY": "LEAF-Solar-Design/leaf-web-demo",
        "SUPPLY_PROVIDER_WORKFLOW_PATH": ".github/workflows/build-platform-images.yml",
        "SUPPLY_SET_POLLS": "1",
        "SUPPLY_SET_INTERVAL": "0",
        "HEAD_SHA": "a" * 40,
        "RELAY_RECEIPT_POLLS": "1",
        "RELAY_RECEIPT_INTERVAL": "0",
        "TERRAFORM_RECEIPT_POLLS": "1",
        "TERRAFORM_RECEIPT_INTERVAL": "0",
        "INFRA_REPO": "LEAF-Solar-Design/leaf-automation-aws-terraform",
    }
    exports.update(env)
    newline = chr(10)
    preamble = 'PATH="./bin:$PATH"' + newline + "".join(
        "export %s=%s%s" % (key, shlex.quote(value), newline) for key, value in exports.items()
    )
    script = workdir / "step.sh"
    script.write_text(preamble + body, encoding="utf-8", newline=newline)
    completed = subprocess.run(
        [BASH, "step.sh"],
        cwd=workdir,
        capture_output=True,
        text=True,
        timeout=120,
    )
    parsed = {}
    for line in output.read_text(encoding="utf-8").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            parsed[key] = value
    parsed["__returncode__"] = completed.returncode
    parsed["__stderr__"] = completed.stderr
    parsed["__stdout__"] = completed.stdout
    return parsed


def _install_fake_gh(workdir: Path) -> Path:
    binary = workdir / "bin"
    binary.mkdir(exist_ok=True)
    script = binary / "gh"
    # Sequential GraphQL calls are served from graphql-response-<n>.json (the
    # counter file persists across separate run_step() invocations sharing
    # this workdir, so a "read" step followed by a "re-read" step can be
    # driven with two different queue snapshots). Statuses calls are served
    # from statuses-<sha>.json, keyed by the sha embedded in the URL.
    script.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            set -euo pipefail
            DIR="$(cd "$(dirname "$0")/.." && pwd)"
            ARGS="$*"
            if [[ "$ARGS" == *"graphql"* ]]; then
              COUNTER_FILE="$DIR/graphql-call-count"
              N=0
              [ -f "$COUNTER_FILE" ] && N=$(cat "$COUNTER_FILE")
              N=$((N + 1))
              echo "$N" > "$COUNTER_FILE"
              RESP="$DIR/graphql-response-$N.json"
              [ -f "$RESP" ] || RESP="$DIR/graphql-response-last.json"
              cat "$RESP"
              exit 0
            fi
            for arg in "$@"; do
              case "$arg" in
                */actions/runs\\?*)
                  cat "$DIR/dispatcher-runs.json"
                  exit 0
                  ;;
                */actions/runs/*/jobs\\?*)
                  cat "$DIR/dispatcher-jobs.json"
                  exit 0
                  ;;
                */commits/*/statuses*)
                  SHA=$(printf '%s' "$arg" | sed -E 's#.*/commits/([0-9a-f]+)/statuses.*#\\1#')
                  RESP="$DIR/statuses-$SHA.json"
                  if [ -f "$RESP" ]; then cat "$RESP"; else echo "[]"; fi
                  exit 0
                  ;;
              esac
            done
            echo "{}"
            """
        ),
        encoding="utf-8",
        newline="\n",
    )
    script.chmod(0o755)
    return binary


def graphql_page(nodes: list, has_next: bool = False, end_cursor: str = "") -> dict:
    return {
        "data": {
            "repository": {
                "mergeQueue": {
                    "entries": {
                        "pageInfo": {"hasNextPage": has_next, "endCursor": end_cursor},
                        "nodes": nodes,
                    }
                }
            }
        }
    }


def member_node(position: int, number: int, head_sha: str) -> dict:
    return {
        "position": position,
        "baseCommit": {"oid": "0" * 40},
        "headCommit": {"oid": head_sha},
        "pullRequest": {"number": number, "headRefOid": head_sha},
    }


def status(state: str, created_at: str, context: str = "kimi-critic-review") -> dict:
    return {"context": context, "state": state, "created_at": created_at}


# --------------------------------------------------------------------------- #
# mq-review: executed against the real step bodies
# --------------------------------------------------------------------------- #

@needs_shell
def test_mq_review_paginates_the_graphql_query_to_find_the_group_head(tmp_path):
    """The target entry only exists on page 2; a broken pagination loop can
    never find it and the step must fail closed instead of silently stopping
    at page 1."""
    _install_fake_gh(tmp_path)
    head_sha = "a" * 40
    page1 = graphql_page([member_node(1, 10, "b" * 40)], has_next=True, end_cursor="cursor-1")
    page2 = graphql_page([member_node(2, 11, head_sha)], has_next=False)
    (tmp_path / "graphql-response-1.json").write_text(json.dumps(page1), encoding="utf-8")
    (tmp_path / "graphql-response-2.json").write_text(json.dumps(page2), encoding="utf-8")
    result = run_step(
        step_body("mq-review", "Read the live merge queue"),
        tmp_path,
        {"HEAD_SHA": head_sha},
    )
    assert result["__returncode__"] == 0, result["__stderr__"]
    assert result["head_sha"] == head_sha
    members = json.loads((tmp_path / "members.json").read_text(encoding="utf-8"))
    assert [m["pullRequest"]["number"] for m in members] == [10, 11]


@needs_shell
def test_mq_review_fails_closed_when_group_head_is_not_in_the_queue(tmp_path):
    _install_fake_gh(tmp_path)
    page1 = graphql_page([member_node(1, 10, "b" * 40)], has_next=False)
    (tmp_path / "graphql-response-1.json").write_text(json.dumps(page1), encoding="utf-8")
    result = run_step(
        step_body("mq-review", "Read the live merge queue"),
        tmp_path,
        {"HEAD_SHA": "c" * 40},
    )
    assert result["__returncode__"] != 0


@needs_shell
@pytest.mark.parametrize(
    "member_statuses,expect_pass,because",
    [
        (
            {"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa": [status("success", "2026-09-01T00:00:00Z")],
             "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb": [status("success", "2026-09-01T00:01:00Z")]},
            True,
            "both members carry a success status",
        ),
        (
            {"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa": [status("success", "2026-09-01T00:00:00Z")],
             "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb": []},
            False,
            "one of the two members has no kimi-critic-review status at all",
        ),
        (
            {"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa": [status("success", "2026-09-01T00:00:00Z")],
             "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb": [status("failure", "2026-09-01T00:00:00Z")]},
            False,
            "the newest status on the second member is a failure",
        ),
        (
            {"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa": [status("success", "2026-09-01T00:00:00Z")],
             "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb": [
                 status("success", "2026-09-01T00:00:00Z"),
                 status("failure", "2026-09-01T00:05:00Z"),
             ]},
            False,
            "a later failure supersedes an earlier success on the same member",
        ),
    ],
)
def test_every_member_of_a_two_member_group_needs_a_success_status(
    tmp_path, member_statuses, expect_pass, because
):
    _install_fake_gh(tmp_path)
    members = [
        member_node(1, 10, "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"),
        member_node(2, 11, "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"),
    ]
    (tmp_path / "members.json").write_text(json.dumps(members), encoding="utf-8")
    for sha, statuses in member_statuses.items():
        (tmp_path / f"statuses-{sha}.json").write_text(json.dumps(statuses), encoding="utf-8")
    result = run_step(
        step_body("mq-review", "Require the newest kimi-critic-review status"),
        tmp_path,
        {},
    )
    ok = result["__returncode__"] == 0
    assert ok == expect_pass, "%s: %s" % (because, result["__stderr__"])


@needs_shell
def test_post_check_reread_fails_on_membership_drift(tmp_path):
    _install_fake_gh(tmp_path)
    head_sha = "a" * 40
    checked = [member_node(1, 10, head_sha)]
    (tmp_path / "members-check.json").write_text(json.dumps(checked), encoding="utf-8")
    drifted = graphql_page([member_node(1, 10, "c" * 40)], has_next=False)
    (tmp_path / "graphql-response-1.json").write_text(json.dumps(drifted), encoding="utf-8")
    result = run_step(
        step_body("mq-review", "Re-read the queue and require the same membership"),
        tmp_path,
        {"HEAD_SHA": head_sha},
    )
    assert result["__returncode__"] != 0


@needs_shell
def test_post_check_reread_passes_on_unchanged_membership(tmp_path):
    _install_fake_gh(tmp_path)
    head_sha = "a" * 40
    checked = [member_node(1, 10, head_sha)]
    (tmp_path / "members-check.json").write_text(json.dumps(checked), encoding="utf-8")
    same = graphql_page([member_node(1, 10, head_sha)], has_next=False)
    (tmp_path / "graphql-response-1.json").write_text(json.dumps(same), encoding="utf-8")
    result = run_step(
        step_body("mq-review", "Re-read the queue and require the same membership"),
        tmp_path,
        {"HEAD_SHA": head_sha},
    )
    assert result["__returncode__"] == 0, result["__stderr__"]


@needs_shell
def test_post_check_reread_rejects_changed_pr_head_with_same_group_head(tmp_path):
    _install_fake_gh(tmp_path)
    head = "a" * 40
    checked = [member_node(1, 10, head)]
    (tmp_path / "members-check.json").write_text(json.dumps(checked), encoding="utf-8")
    checked[0]["pullRequest"]["headRefOid"] = "b" * 40
    (tmp_path / "graphql-response-1.json").write_text(
        json.dumps(graphql_page(checked)), encoding="utf-8",
    )
    result = run_step(step_body("mq-review", "Re-read the queue"), tmp_path, {"HEAD_SHA": head})
    assert result["__returncode__"] != 0
    assert "membership drifted" in result["__stdout__"]


# --------------------------------------------------------------------------- #
# mq-supply: the docs-only recompute, executed against a real git repo
# --------------------------------------------------------------------------- #

def _group_repo(tmp_path, changed: str | None) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    run = lambda *argv: subprocess.run(argv, cwd=repo, check=True, capture_output=True)
    run("git", "init", "-q")
    run("git", "config", "user.email", "t@example.invalid")
    run("git", "config", "user.name", "t")
    (repo / "scripts").mkdir()
    (repo / "server.py").write_text("baseline\n", encoding="utf-8")
    filter_src = (ROOT / "scripts" / "docs_noop_filter.py").read_text(encoding="utf-8")
    (repo / "scripts" / "docs_noop_filter.py").write_text(filter_src, encoding="utf-8")
    run("git", "add", "-A")
    run("git", "commit", "-qm", "main tip")
    if changed is not None:
        target = repo / changed
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("changed\n", encoding="utf-8")
        run("git", "add", "-A")
        run("git", "commit", "-qm", "group head")
    return repo


@needs_shell
@pytest.mark.parametrize(
    "changed,expect_supply_none",
    [
        ("docs/whatever.md", True),
        ("server.py", False),
    ],
)
def test_docs_only_group_recomputed_from_the_real_diff(tmp_path, changed, expect_supply_none):
    repo = _group_repo(tmp_path, changed)
    _dispatcher_evidence(repo)
    result = run_step(step_body("mq-supply", "Recompute the docs-noop verdict"), repo, {})
    assert result["__returncode__"] == 0, result["__stderr__"]
    if expect_supply_none:
        assert result.get("supply") == "none"
    else:
        assert result.get("supply") in ("", None)


@needs_shell
def test_docs_only_recompute_fails_open_with_no_first_parent(tmp_path):
    repo = _group_repo(tmp_path, None)
    result = run_step(step_body("mq-supply", "Recompute the docs-noop verdict"), repo, {})
    assert result["__returncode__"] == 0, result["__stderr__"]
    assert result.get("supply") in ("", None)


def _dispatcher_evidence(repo, missing=None, conclusion="skipped"):
    _install_fake_gh(repo)
    runs = [{
        "id": 12, "event": "merge_group", "head_sha": "a" * 40,
        "path": ".github/workflows/speculate-platform-images.yml",
        "created_at": "2026-09-05T00:00:00Z",
    }]
    steps = [{"name": "Dispatch the merge-group build on the main ref",
              "conclusion": conclusion}]
    jobs = [{"name": "dispatch-group", "conclusion": "success",
             "steps": [] if missing == "step" else steps}]
    (repo / "dispatcher-runs.json").write_text(
        json.dumps({"workflow_runs": [] if missing == "run" else runs}), encoding="utf-8",
    )
    (repo / "dispatcher-jobs.json").write_text(
        json.dumps({"jobs": [] if missing == "job" else jobs}), encoding="utf-8",
    )


@needs_shell
@pytest.mark.parametrize("missing,conclusion", [
    (None, "success"), ("run", "skipped"), ("job", "skipped"), ("step", "skipped"),
])
def test_local_docs_verdict_requires_dispatcher_skip_evidence(tmp_path, missing, conclusion):
    repo = _group_repo(tmp_path, "docs/whatever.md")
    _dispatcher_evidence(repo, missing, conclusion)
    result = run_step(step_body("mq-supply", "Recompute the docs-noop verdict"), repo, {})
    assert result["__returncode__"] == 0, result["__stderr__"]
    assert result.get("supply") in ("", None)


@needs_shell
@pytest.mark.parametrize("receipt,accepted", [({"weights_touched": False}, True), ({}, False)])
def test_receipt_weights_presence_uses_the_workflow_jq_expression(tmp_path, receipt, accepted):
    body = step_body("mq-prewarm", "Wait for every dispatched terraform staging receipt")
    expression = re.search(r"REC_WEIGHTS=\$\(jq -[rj] '([^']+)'", body).group(1)
    (tmp_path / "receipt.json").write_text(json.dumps(receipt), encoding="utf-8")
    result = run_step(
        "REC_WEIGHTS=$(jq -r %s receipt.json)\n[ \"$REC_WEIGHTS\" = \"false\" ]\n"
        % shlex.quote(expression), tmp_path, {},
    )
    assert (result["__returncode__"] == 0) == accepted


def test_boolean_receipt_fields_never_use_jq_alternative_operator():
    assert not re.search(r"\.(?:weights_touched|migration_refusal)\s*//", workflow_text())


def test_every_network_command_has_a_timeout():
    for job in workflow_document()["jobs"].values():
        for step in job["steps"]:
            body = step.get("run", "").replace("\\\n", " ")
            for line in body.splitlines():
                if line.lstrip().startswith("#"):
                    continue
                for command in re.finditer(r"\b(gh|curl)\s+", line):
                    prefix = line[:command.start()]
                    wrapped = re.search(r"\btimeout\s+[1-9][0-9]*\s+$", prefix)
                    assert wrapped or (
                        command.group(1) == "curl"
                        and re.search(r"--max-time\s+[1-9][0-9]*", line[command.end():])
                    ), line


# --------------------------------------------------------------------------- #
# Structural / falsifying pins with no local executable surface
# --------------------------------------------------------------------------- #

def test_it_fires_on_merge_group_checks_requested_and_pull_request_to_main():
    triggers = workflow_document()["on"]
    assert triggers["merge_group"]["types"] == ["checks_requested"]
    assert set(triggers["pull_request"]["types"]) == {
        "opened", "synchronize", "reopened", "ready_for_review",
    }
    assert triggers["pull_request"]["branches"] == ["main"]


def test_both_required_contexts_are_named_exactly():
    document = workflow_document()
    assert document["jobs"]["mq-review"]["name"] == "mq-review"
    assert document["jobs"]["mq-prewarm"]["name"] == "mq-prewarm"


def test_mq_supply_is_not_a_required_context_and_only_runs_for_the_group():
    assert workflow_document()["jobs"]["mq-supply"]["if"] == "github.event_name == 'merge_group'"


def test_every_step_in_the_required_jobs_is_conditioned_on_the_event():
    for job in ("mq-review", "mq-prewarm"):
        for step in job_steps(job):
            assert "if" in step, "%s: unconditioned step %r would run on every event" % (
                job, step.get("name"),
            )


def test_pull_request_arm_publishes_a_deferred_success_and_calls_nothing():
    for job in ("mq-review", "mq-prewarm"):
        step = step_by_name(job, "Publish the deferred queue-preparation success")
        assert step["if"] == "github.event_name == 'pull_request'"
        body = step["run"]
        for forbidden in ("gh ", "curl", "git "):
            assert forbidden not in body, "%s: pull_request arm must do nothing but notice" % job


def test_mq_prewarm_always_runs_and_fails_explicitly_on_a_dependency_failure():
    document = workflow_document()
    assert document["jobs"]["mq-prewarm"]["if"] == "always()"
    assert document["jobs"]["mq-prewarm"]["needs"] == ["mq-review", "mq-supply"]
    step = step_by_name("mq-prewarm", "Fail explicitly on a failed or cancelled dependency")
    condition = step["if"]
    for needed in (
        "needs.mq-review.result == 'failure'",
        "needs.mq-review.result == 'cancelled'",
        "needs.mq-supply.result == 'failure'",
        "needs.mq-supply.result == 'cancelled'",
    ):
        assert needed in condition
    assert "exit 1" in step["run"]


def test_mq_supply_provider_checks_match_adopt_decides_list():
    """Read-only repeat of build-platform-images.yml's adopt job: repository
    id, path, event, branch, status, conclusion, then the immutable archive
    digest verified against a real download."""
    body = step_body("mq-supply", "Wait for the provider-bound speculative supply set")
    assert ".workflow_run.head_repository_id == $repo_id" in body
    assert '.event == "workflow_dispatch"' in body
    assert '.head_branch == "main"' in body
    assert '.status == "completed"' in body
    assert '.conclusion == "success"' in body
    assert "sha256:$(sha256sum spec-candidate.zip" in body
    assert 'ACTUAL_DIGEST" = "$CAND_DIGEST"' in body
    assert "spec-v3-supply-set-$TREE" in body


def test_mq_supply_bounded_poll_matches_spec_40x30s():
    body = step_body("mq-supply", "Wait for the provider-bound speculative supply set")
    assert '"$ATTEMPT" -lt "$SUPPLY_SET_POLLS"' in body
    assert workflow_document()["env"]["SUPPLY_SET_POLLS"] == "40"
    assert workflow_document()["env"]["SUPPLY_SET_INTERVAL"] == "30"


def test_relay_receipt_named_by_the_group_head_sha():
    body = step_body("mq-prewarm", "Wait for the relay's prewarm receipt")
    assert 'SHA12="${GROUP_HEAD_SHA:0:12}"' in body
    assert 'NAME="prewarm-relay-receipt-mg-$SHA12"' in body
    assert '"$ATTEMPT" -lt "$RELAY_RECEIPT_POLLS"' in body
    assert workflow_document()["env"]["RELAY_RECEIPT_POLLS"] == "30"
    assert workflow_document()["env"]["RELAY_RECEIPT_INTERVAL"] == "60"


def test_relay_receipt_requires_every_stage_service_dispatched():
    body = step_body("mq-prewarm", "Wait for the relay's prewarm receipt")
    assert 'entry.get("disposition") != "dispatched"' in body
    assert "relay dispatched nothing for this group" in body
    assert ".group.head_sha" in body


def test_migration_refused_group_succeeds_with_the_named_reason():
    body = step_body("mq-prewarm", "Wait for the relay's prewarm receipt")
    assert "MIGRATION_REFUSAL" in body
    assert "reason=migration candidate: normal deploy path" in body
    assert 'if [ "$MIGRATION_REFUSAL" = "true" ]; then' in body


def test_docs_only_group_succeeds_with_the_named_reason():
    supply_body = step_body("mq-supply", "Recompute the docs-noop verdict")
    assert "reason=docs-only" in supply_body
    assert 'echo "supply=none"' in supply_body
    prewarm_step = step_by_name("mq-prewarm", "Recognize a docs-only group")
    assert prewarm_step["if"] == "github.event_name == 'merge_group' && needs.mq-supply.outputs.supply == 'none'"
    assert "reason=docs-only group: nothing to stage" in prewarm_step["run"]


def test_per_service_terraform_receipt_checks_include_weights_touched_false():
    body = step_body("mq-prewarm", "Wait for every dispatched terraform staging receipt")
    assert 'NAME="staging-prewarm-receipt-$SERVICE-run-$RUN_ID-attempt-$ATTEMPT_NUM"' in body
    assert 'staged-prewarm-receipt.json' in body
    assert '[ "$REC_SERVICE" = "$SERVICE" ]' in body
    assert 'EXPECTED_TAG="spec-$TREE-$SHA12"' in body
    assert '[ "$REC_TAG" = "$EXPECTED_TAG" ]' in body
    assert '[ "$REC_WEIGHTS" = "false" ]' in body
    assert '"$ATTEMPT" -lt "$TERRAFORM_RECEIPT_POLLS"' in body
    assert workflow_document()["env"]["TERRAFORM_RECEIPT_POLLS"] == "40"
    assert workflow_document()["env"]["TERRAFORM_RECEIPT_INTERVAL"] == "60"


def test_terraform_receipt_step_gates_on_supply_present_and_no_migration_refusal():
    step = step_by_name("mq-prewarm", "Wait for every dispatched terraform staging receipt")
    condition = step["if"]
    assert "needs.mq-supply.outputs.supply == 'present'" in condition
    assert "steps.relay.outputs.present == 'true'" in condition
    assert "steps.relay.outputs.migration_refusal != 'true'" in condition


def test_concurrency_key_is_the_group_head_sha_without_cancellation():
    concurrency = workflow_document()["concurrency"]
    assert concurrency["group"] == (
        "merge-queue-${{ github.event.merge_group.head_sha || "
        "github.event.pull_request.head.sha }}"
    )
    assert concurrency["cancel-in-progress"] == "false"


def test_permissions_are_least():
    assert workflow_document()["permissions"] == {
        "contents": "read",
        "pull-requests": "read",
        "actions": "read",
        "statuses": "read",
        "checks": "read",
    }


def test_secrets_used_are_exactly_github_token_and_terraform_repo_token():
    names = set(re.findall(r"secrets\.([A-Za-z_][A-Za-z0-9_]*)", workflow_text()))
    assert names == {"TERRAFORM_REPO_TOKEN"}
    assert "github.token" in workflow_text()


def test_terraform_token_is_scoped_to_the_terraform_receipt_step_only():
    for job in ("mq-review", "mq-supply", "mq-prewarm"):
        for step in job_steps(job):
            env = step.get("env", {})
            if "TERRAFORM_REPO_TOKEN" in str(env.get("GH_TOKEN", "")):
                assert step["name"] == "Wait for every dispatched terraform staging receipt"


def test_every_job_carries_a_timeout():
    document = workflow_document()
    for job in ("mq-review", "mq-supply", "mq-prewarm"):
        assert int(document["jobs"][job]["timeout-minutes"]) > 0


def test_every_poll_loop_is_bounded_by_a_named_env_attempt_count():
    text = workflow_text()
    for var in ("SUPPLY_SET_POLLS", "RELAY_RECEIPT_POLLS", "TERRAFORM_RECEIPT_POLLS"):
        assert text.count('"$ATTEMPT" -lt "$%s"' % var) >= 1, "no bounded loop guards on %s" % var
    # Never sleep on the last iteration: every bounded loop pays its interval
    # only when another attempt will actually follow.
    assert text.count("if [ \"$ATTEMPT\" -lt ") == text.count("sleep \"$")


def test_lf_endings():
    raw = WORKFLOW.read_bytes()
    assert b"\r" not in raw


def _assert_no_service_mirror(text):
    assert "STAGE_SERVICES" not in text


def test_no_service_mirror():
    _assert_no_service_mirror(workflow_text())


def test_no_service_mirror_falsification():
    with pytest.raises(AssertionError):
        _assert_no_service_mirror(workflow_text().replace("env:\n", 'env:\n  STAGE_SERVICES: "web"\n', 1))


def _prewarm_evidence(tmp_path, entries, relay_source='env:\n  STAGE_SERVICES: "web app"\n', empty_arn=False, receipt_change=None):
    binary = tmp_path / "bin"
    binary.mkdir(exist_ok=True)
    fake = binary / "gh"
    fake.write_text(textwrap.dedent('''\
        #!/usr/bin/env bash
        set -euo pipefail
        for arg in "$@"; do
          case "$arg" in
            */contents/*)
              echo "$arg" >> contents-calls.txt
              cat relay-source.json; exit 0 ;;
            */actions/artifacts/1/zip) cat relay.zip; exit 0 ;;
            */actions/artifacts/2/zip) cat web.zip; exit 0 ;;
            */actions/artifacts/3/zip) cat app.zip; exit 0 ;;
            */actions/artifacts\?*)
              echo '{"artifacts":[{"id":1,"expired":false,"created_at":"2026-09-05"}]}'; exit 0 ;;
            */actions/runs/101/artifacts\?*)
              echo '{"artifacts":[{"id":2,"expired":false,"name":"staging-prewarm-receipt-web-run-101-attempt-1"}]}'; exit 0 ;;
            */actions/runs/102/artifacts\?*)
              echo '{"artifacts":[{"id":3,"expired":false,"name":"staging-prewarm-receipt-app-run-102-attempt-1"}]}'; exit 0 ;;
            */actions/runs/*)
              echo "$arg" >> terraform-calls.txt
              echo '{"status":"completed","conclusion":"success","run_attempt":1}'; exit 0 ;;
          esac
        done
        exit 1
        '''), encoding="utf-8", newline="\n")
    fake.chmod(0o755)
    response = {} if relay_source is None else {"content": base64.b64encode(relay_source.encode()).decode()}
    (tmp_path / "relay-source.json").write_text(json.dumps(response), encoding="utf-8")
    with zipfile.ZipFile(tmp_path / "relay.zip", "w") as archive:
        archive.writestr("prewarm-relay-receipt.json", json.dumps({
            "group": {"head_sha": "a" * 40}, "dispatched": entries,
        }))
    for service in ("web", "app"):
        receipt = {
            "schema": "leaf.staging-prewarm-receipt.v1",
            "service": service,
            "ecs_service": "leaf-platform-" + service + "-alt",
            "color": "alt",
            "staged_td_arn": "" if empty_arn else _staged_arn(service),
            "image_tag": "spec-" + "b" * 40 + "-" + "a" * 12,
            "image_digest": "sha256:" + "c" * 64,
            "witness": "synthetic",
            "run_id": "101" if service == "web" else "102",
            "run_attempt": "1",
            "warm_started_at": "2026-09-05T00:00:00Z",
            "staged_at": "2026-09-05T00:01:00Z",
            "migration_legs_skipped": True,
            "mutations_transaction_skipped": True,
            "weights_touched": False,
        }
        if receipt_change == "old-key-only":
            receipt["task_definition_arn"] = receipt.pop("staged_td_arn")
        elif receipt_change:
            receipt.update(receipt_change)
        with zipfile.ZipFile(tmp_path / f"{service}.zip", "w") as archive:
            archive.writestr("staged-prewarm-receipt.json", json.dumps(receipt))


def _staged_arn(service):
    return "arn:aws:ecs:us-east-1:123456789012:task-definition/leaf-platform-" + service + "-alt:254"


def _dispatch(service, run_id, disposition="dispatched"):
    return {"service": service, "run_id": run_id, "disposition": disposition}


@needs_shell
@pytest.mark.parametrize("entries,source,error", [
    ([], 'STAGE_SERVICES: "web app"', "relay dispatched nothing"),
    ([_dispatch("web", 101, "dispatch-failed")], 'STAGE_SERVICES: "web"', "web"),
    ([_dispatch("app", 102, "unresolved")], 'STAGE_SERVICES: "app"', "app"),
    ([_dispatch("app", None)], 'STAGE_SERVICES: "app"', "app"),
    ([_dispatch("web", 101)], 'STAGE_SERVICES: "web app"', "differ"),
    ([_dispatch("web", 101), _dispatch("app", 102)], 'STAGE_SERVICES: "web app"', None),
    ([_dispatch("web", 101)], 'env: {}', "absent or unparsable"),
    ([_dispatch("web", 101)], 'STAGE_SERVICES: web', "absent or unparsable"),
    ([_dispatch("web", 101)], None, "absent or unparsable"),
])
def test_relay_dispatched_set_executed(tmp_path, entries, source, error):
    _prewarm_evidence(tmp_path, entries, source)
    result = run_step(step_body("mq-prewarm", "Wait for the relay's"), tmp_path,
                      {"GROUP_HEAD_SHA": "a" * 40})
    if error:
        assert result["__returncode__"] != 0
        assert error in result["__stdout__"] + result["__stderr__"]
    else:
        assert result["__returncode__"] == 0, result
        assert json.loads(result["dispatched_json"]) == {"web": 101, "app": 102}
        assert "configured services: app web" in result["__stdout__"]
        assert "dispatched services: app web" in result["__stdout__"]
        assert (tmp_path / "contents-calls.txt").read_text().strip().endswith(
            "prewarm-staging-group.yml?ref=" + "a" * 40)
        waited = run_step(step_body("mq-prewarm", "Wait for every dispatched"), tmp_path,
                          {"GROUP_HEAD_SHA": "a" * 40, "TREE": "b" * 40,
                           "DISPATCHED_JSON": result["dispatched_json"]})
        assert waited["__returncode__"] == 0, waited
        assert _staged_arn("app") + " " + _staged_arn("web") in waited["__stdout__"]


@needs_shell
@pytest.mark.parametrize("dispatched,empty_arn,accepted", [
    ({"web": 101, "app": 102}, False, True),
    ({"web": 101, "app": 102}, True, False),
    ({}, False, False),
])
def test_terraform_wait_uses_dispatched_set_and_requires_arns(tmp_path, dispatched, empty_arn, accepted):
    _prewarm_evidence(tmp_path, [], empty_arn=empty_arn)
    result = run_step(step_body("mq-prewarm", "Wait for every dispatched"), tmp_path,
                      {"GROUP_HEAD_SHA": "a" * 40, "TREE": "b" * 40,
                       "DISPATCHED_JSON": json.dumps(dispatched)})
    assert (result["__returncode__"] == 0) == accepted, result
    if accepted:
        calls = (tmp_path / "terraform-calls.txt").read_text()
        assert "/runs/101" in calls and "/runs/102" in calls
        assert "staged task definitions: " + _staged_arn("app") + " " + _staged_arn("web") in result["__stdout__"]
    else:
        assert "empty" in result["__stdout__"] + result["__stderr__"]


@needs_shell
@pytest.mark.parametrize("receipt_change,error", [
    (None, None),
    ("old-key-only", "empty or invalid staged task definition ARN for app"),
    ({"schema": "wrong-schema"}, "schema mismatch"),
    ({"run_id": "999"}, "run_id mismatch"),
    ({"staged_td_arn": "arn:wrong:app"}, "empty or invalid staged task definition ARN for app"),
    ({"staged_td_arn": 254}, "empty or invalid staged task definition ARN for app"),
])
def test_terraform_wait_reads_real_receipt_schema(tmp_path, receipt_change, error):
    _prewarm_evidence(tmp_path, [], receipt_change=receipt_change)
    result = run_step(step_body("mq-prewarm", "Wait for every dispatched"), tmp_path,
                      {"GROUP_HEAD_SHA": "a" * 40, "TREE": "b" * 40,
                       "DISPATCHED_JSON": json.dumps({"app": 102})})
    if error:
        assert result["__returncode__"] != 0, result
        assert error in result["__stdout__"] + result["__stderr__"]
    else:
        assert result["__returncode__"] == 0, result
        assert "staged task definitions: " + _staged_arn("app") in result["__stdout__"]
