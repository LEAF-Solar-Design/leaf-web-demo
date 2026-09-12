"""Executable contract for the merge-queue group controller (slice C).

mq-review's pagination and per-member status gate are EXECUTED here against
the real step bodies with a fake `gh`, because a two-member fixture where one
lacks a passing `critic-review` status is exactly the case a text
assertion would never catch, and neither is a queue read that only resolves
correctly once two GraphQL pages are combined. mq-supply's docs-only recompute
is likewise executed against a real git repository, the same pattern
scripts/test_build_platform_images_workflow.py and
scripts/test_prewarm_staging_cutover_workflow.py use for their own
git-derived decisions.

The remaining assertions pin properties with no local executable surface:
GraphQL/REST shapes that only resolve against live GitHub state, the
concurrency key, the
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
RERUN_WORKFLOW = ROOT / ".github" / "workflows" / "mq-admission-rerun.yml"


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


def _check_prewarm_read_role(document: dict) -> None:
    job = document["jobs"]["mq-prewarm"]
    assert "environment" not in job, "mq-prewarm must not declare an environment"
    # Scan the whole mapping, including reusable-workflow secrets and inputs.
    serialized = json.dumps(job)
    assert "AWS_ECR_PUSH_ROLE" not in serialized, "mq-prewarm must not use the release role"
    assert "secrets.AWS_MQ_PREWARM_READ_ROLE" in serialized, "mq-prewarm needs its read role"
    credentials = [step for step in job.get("steps", [])
                   if step.get("uses", "").startswith("aws-actions/configure-aws-credentials@")]
    assert credentials, "mq-prewarm needs AWS credentials"
    assert all(step.get("with", {}).get("role-to-assume") ==
               "${{ secrets.AWS_MQ_PREWARM_READ_ROLE }}" for step in credentials), (
        "mq-prewarm credentials must assume the read role"
    )


def test_prewarm_uses_its_read_role_without_an_environment():
    _check_prewarm_read_role(workflow_document())


@pytest.mark.parametrize("mutation", [
    "release-role", "environment-scalar", "environment-mapping", "reusable-secret",
])
def test_prewarm_read_role_pin_rejects_release_authority(mutation):
    document = workflow_document()
    _check_prewarm_read_role(document)
    mutated = json.loads(json.dumps(document))
    job = mutated["jobs"]["mq-prewarm"]
    if mutation == "release-role":
        mutated = json.loads(json.dumps(mutated).replace(
            "AWS_MQ_PREWARM_READ_ROLE", "AWS_ECR_PUSH_ROLE"))
    elif mutation == "environment-scalar":
        job["environment"] = "ecr-release"
    elif mutation == "environment-mapping":
        job["environment"] = {"name": "ecr-release"}
    else:
        job["secrets"] = {"role": "${{ secrets.AWS_ECR_PUSH_ROLE }}"}
    with pytest.raises(AssertionError, match="mq-prewarm"):
        _check_prewarm_read_role(mutated)


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
        "MQ_TRANSPORT_BUCKET": "leaf-mq-transport-807034087062-us-east-1",
        "MQ_TRANSPORT_PREFIX": "mq/leaf-web-demo/",
        "GITHUB_REPOSITORY": "LEAF-Solar-Design/leaf-web-demo",
        "SUPPLY_PROVIDER_WORKFLOW_PATH": ".github/workflows/build-platform-images.yml",
        "SUPPLY_SET_POLLS": "1",
        "SUPPLY_SET_INTERVAL": "0",
        "HEAD_SHA": "a" * 40,
        "RELAY_RECEIPT_POLLS": "1",
        "RELAY_RECEIPT_INTERVAL": "0",
        "TERRAFORM_RECEIPT_POLLS": "1",
        "TERRAFORM_RECEIPT_INTERVAL": "0",
        "RELAY_RUN_ID": "77",
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
    # from statuses-<sha>.json, keyed by the sha embedded in the URL. Pulls
    # lookups (a status event resolving its PR) are served from
    # pulls-<sha>.json, same keying, defaulting to no open pull requests.
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
                */commits/*/pulls*)
                  [ -f "$DIR/fail-pulls" ] && exit 1
                  SHA=$(printf '%s' "$arg" | sed -E 's#.*/commits/([0-9a-f]+)/pulls.*#\\1#')
                  RESP="$DIR/pulls-$SHA.json"
                  if [ -f "$RESP" ]; then cat "$RESP"; else echo "[]"; fi
                  exit 0
                  ;;
                */commits/*/check-runs*)
                  [ -f "$DIR/fail-check-runs" ] && exit 1
                  SHA=$(printf '%s' "$arg" | sed -E 's#.*/commits/([0-9a-f]+)/check-runs.*#\\1#')
                  COUNTER_FILE="$DIR/check-runs-call-count"
                  N=0
                  [ -f "$COUNTER_FILE" ] && N=$(cat "$COUNTER_FILE")
                  N=$((N + 1))
                  echo "$N" > "$COUNTER_FILE"
                  RESP="$DIR/check-runs-$SHA-$N.json"
                  [ -f "$RESP" ] || RESP="$DIR/check-runs-$SHA.json"
                  if [ -f "$RESP" ]; then cat "$RESP"; else echo '{"check_runs":[]}'; fi
                  exit 0
                  ;;
                */actions/jobs/*/rerun)
                  [[ "$ARGS" == *"POST"* ]] || exit 1
                  ID=$(printf '%s' "$arg" | sed -E 's#.*/actions/jobs/([0-9]+)/rerun#\\1#')
                  echo "$ID" >> "$DIR/rerun-calls.txt"
                  [ -f "$DIR/fail-rerun" ] && exit 1
                  echo "{}"
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


def rerun_document() -> dict:
    return yaml.load(RERUN_WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def rerun_step_body() -> str:
    return rerun_document()["jobs"]["rerun"]["steps"][0]["run"]


def test_admission_rerun_structure():
    document = rerun_document()
    assert document["on"] == {"status": {}}
    assert set(document["jobs"]) == {"rerun"}
    job = document["jobs"]["rerun"]
    assert job["if"] == "github.event.context == 'critic-review' && github.event.state == 'success'"
    assert document["permissions"] == {
        "actions": "write", "checks": "read", "pull-requests": "read", "statuses": "read",
    }
    assert len(job["steps"]) == 1
    assert all("uses" not in step for step in job["steps"])
    assert document["concurrency"] == {
        "group": "mq-admission-rerun-${{ github.event.sha }}", "cancel-in-progress": "false",
    }
    assert int(job["timeout-minutes"]) > 0
    body = rerun_step_body()
    assert "gh api" in body
    assert all(body[max(0, match.start() - 11):match.start()] == "timeout 60 "
               for match in re.finditer(r"gh api", body))


def _rerun_check(job_id=2, conclusion="failure", status="completed",
                 started_at="2026-09-12T00:05:00Z", app="github-actions"):
    return {"id": job_id, "name": "mq-review", "status": status,
            "conclusion": conclusion, "started_at": started_at, "app": {"slug": app}}


def _rerun_fixture(tmp_path, checks):
    _install_fake_gh(tmp_path)
    (tmp_path / f"pulls-{PR_HEAD_SHA}.json").write_text(json.dumps([
        {"number": 34, "state": "open", "base": {"ref": "main"},
         "head": {"sha": PR_HEAD_SHA}},
    ]), encoding="utf-8")
    (tmp_path / f"check-runs-{PR_HEAD_SHA}.json").write_text(
        json.dumps({"check_runs": checks}), encoding="utf-8")


def _run_rerun(tmp_path, expected_calls="", expected_code=0):
    result = run_step(rerun_step_body(), tmp_path, {
        "HEAD_SHA": PR_HEAD_SHA, "CHECK_POLLS": "3", "CHECK_INTERVAL": "0",
    })
    calls = tmp_path / "rerun-calls.txt"
    assert (calls.read_text(encoding="utf-8") if calls.exists() else "") == expected_calls
    assert result["__returncode__"] == expected_code, result
    return result


@needs_shell
def test_admission_rerun_completed_failure(tmp_path):
    _rerun_fixture(tmp_path, [_rerun_check()])
    _run_rerun(tmp_path, "2\n")


@needs_shell
@pytest.mark.parametrize("case", ["empty", "closed", "other-base"])
def test_admission_rerun_no_open_pr(tmp_path, case):
    _rerun_fixture(tmp_path, [_rerun_check()])
    pulls = [] if case == "empty" else [{
        "number": 34, "state": "closed" if case == "closed" else "open",
        "base": {"ref": "other" if case == "other-base" else "main"},
        "head": {"sha": PR_HEAD_SHA},
    }]
    (tmp_path / f"pulls-{PR_HEAD_SHA}.json").write_text(json.dumps(pulls), encoding="utf-8")
    _run_rerun(tmp_path)


@needs_shell
def test_admission_rerun_completed_success(tmp_path):
    _rerun_fixture(tmp_path, [_rerun_check(conclusion="success")])
    _run_rerun(tmp_path)


@needs_shell
def test_admission_rerun_waits_for_completion(tmp_path):
    _rerun_fixture(tmp_path, [_rerun_check()])
    (tmp_path / f"check-runs-{PR_HEAD_SHA}-1.json").write_text(
        json.dumps({"check_runs": [_rerun_check(status="in_progress", conclusion=None)]}),
        encoding="utf-8")
    _run_rerun(tmp_path, "2\n")
    assert (tmp_path / "check-runs-call-count").read_text().strip() == "2"


@needs_shell
def test_admission_rerun_still_running(tmp_path):
    _rerun_fixture(tmp_path, [_rerun_check(status="in_progress", conclusion=None)])
    _run_rerun(tmp_path)
    assert (tmp_path / "check-runs-call-count").read_text().strip() == "3"


@needs_shell
def test_admission_rerun_no_check(tmp_path):
    _rerun_fixture(tmp_path, [])
    _run_rerun(tmp_path)


@needs_shell
@pytest.mark.parametrize("newest_conclusion,expected_calls", [
    ("failure", "2\n"), ("success", ""),
])
def test_admission_rerun_newest_wins(tmp_path, newest_conclusion, expected_calls):
    older = "success" if newest_conclusion == "failure" else "failure"
    _rerun_fixture(tmp_path, [
        _rerun_check(conclusion=newest_conclusion),
        _rerun_check(job_id=1, conclusion=older, started_at="2026-09-12T00:00:00Z"),
    ])
    _run_rerun(tmp_path, expected_calls)


@needs_shell
def test_admission_rerun_ignores_other_app(tmp_path):
    _rerun_fixture(tmp_path, [_rerun_check(app="other-app")])
    _run_rerun(tmp_path)


@needs_shell
@pytest.mark.parametrize("marker,expected_calls", [
    ("fail-pulls", ""), ("fail-check-runs", ""), ("fail-rerun", "2\n"),
])
def test_admission_rerun_api_failure(tmp_path, marker, expected_calls):
    _rerun_fixture(tmp_path, [_rerun_check()])
    (tmp_path / marker).touch()
    result = _run_rerun(tmp_path, expected_calls, expected_code=1)
    assert "::error::" in result["__stdout__"]


def test_task_34_relay_receipt_wait_covers_group_readiness():
    # The controller's relay-receipt wait starts after mq-supply passed, so it
    # races only the group stage's readiness leg plus dispatch, never the whole
    # stage job. The 2026-09-10 "30 vs 42" reading compared the wrong pair.
    controller = workflow_document()["env"]
    stage = yaml.load(
        (ROOT / ".github" / "workflows" / "prewarm-staging-group.yml").read_text(
            encoding="utf-8"), Loader=yaml.BaseLoader,
    )["env"]
    assert (int(controller["RELAY_RECEIPT_POLLS"]) *
            int(controller["RELAY_RECEIPT_INTERVAL"])) >= (
                int(stage["SUPPLY_SET_POLLS"]) * int(stage["SUPPLY_SET_INTERVAL"]) + 300)


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


def status(state: str, created_at: str, context: str = "critic-review") -> dict:
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
            "one of the two members has no critic-review status at all",
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
        step_body("mq-review", "Require the newest critic-review status"),
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
# mq-review: pull_request admission and its status-triggered re-decision,
# executed against the real step bodies with a fake `gh`. This is the same
# newest-by-created_at critic-review rule the merge_group path proves
# above, so a PR and its group can never disagree about admission.
# --------------------------------------------------------------------------- #

PR_HEAD_SHA = "d" * 40


def _write_statuses(tmp_path: Path, sha: str, statuses: list) -> None:
    (tmp_path / f"statuses-{sha}.json").write_text(json.dumps(statuses), encoding="utf-8")


@needs_shell
@pytest.mark.parametrize(
    "state,expect_pass,because",
    [
        ("success", True, "the newest critic-review status is success"),
        ("failure", False, "the newest critic-review status is failure"),
        ("pending", False, "the newest critic-review status is pending"),
        (None, False, "the PR carries no critic-review status at all"),
    ],
)
def test_pull_request_admission_requires_a_kimi_success(tmp_path, state, expect_pass, because):
    _install_fake_gh(tmp_path)
    statuses = [] if state is None else [status(state, "2026-09-01T00:00:00Z")]
    _write_statuses(tmp_path, PR_HEAD_SHA, statuses)
    result = run_step(
        step_body("mq-review", "Decide admission from the newest critic-review status (pull_request)"),
        tmp_path,
        {"HEAD_SHA": PR_HEAD_SHA},
    )
    assert (result["__returncode__"] == 0) == expect_pass, "%s: %s" % (because, result["__stderr__"])
    if not expect_pass:
        assert PR_HEAD_SHA in (result["__stdout__"] + result["__stderr__"])


@needs_shell
def test_pull_request_admission_uses_the_newest_status_by_created_at(tmp_path):
    _install_fake_gh(tmp_path)
    _write_statuses(tmp_path, PR_HEAD_SHA, [
        status("failure", "2026-09-01T00:05:00Z"),
        status("success", "2026-09-01T00:00:00Z"),
    ])
    result = run_step(
        step_body("mq-review", "Decide admission from the newest critic-review status (pull_request)"),
        tmp_path,
        {"HEAD_SHA": PR_HEAD_SHA},
    )
    assert result["__returncode__"] != 0, "an older success must not beat a newer failure"


@needs_shell
@pytest.mark.parametrize("failure_mode", ["gh-exit-nonzero", "empty-body"])
def test_pull_request_admission_fails_closed_on_an_unreadable_gate(tmp_path, failure_mode):
    binary = tmp_path / "bin"
    binary.mkdir(exist_ok=True)
    fake = binary / "gh"
    fake.write_text(
        "#!/usr/bin/env bash\nexit %s\n" % ("1" if failure_mode == "gh-exit-nonzero" else "0"),
        encoding="utf-8", newline="\n",
    )
    fake.chmod(0o755)
    result = run_step(
        step_body("mq-review", "Decide admission from the newest critic-review status (pull_request)"),
        tmp_path,
        {"HEAD_SHA": PR_HEAD_SHA},
    )
    message = result["__stdout__"] + result["__stderr__"]
    assert result["__returncode__"] != 0, message
    assert "unreadable gate" in message
    assert "post a critic-review success on this head and re-run this check" not in message


@needs_shell
def test_pull_request_admission_same_second_tie_resolves_to_the_newer_success(tmp_path):
    """The statuses API returns newest first and jq's sort_by is stable, so a
    plain sort_by(.created_at) | last would pick the OLDER entry when two
    statuses share a created_at second. A failure corrected by a success in
    the same second must still admit."""
    _install_fake_gh(tmp_path)
    _write_statuses(tmp_path, PR_HEAD_SHA, [
        status("success", "2026-09-01T00:00:00Z"),
        status("failure", "2026-09-01T00:00:00Z"),
    ])
    result = run_step(
        step_body("mq-review", "Decide admission from the newest critic-review status (pull_request)"),
        tmp_path,
        {"HEAD_SHA": PR_HEAD_SHA},
    )
    assert result["__returncode__"] == 0, (
        "a same-second success listed after a same-second failure must still admit: %s"
        % (result["__stdout__"] + result["__stderr__"])
    )


def test_both_arms_resolve_a_same_second_tie_identically():
    """R1 from the read of head 58688301: the pull_request arm and the
    merge_group arm are ONE rule. While only the pull_request arm reversed, a
    failure corrected by a success inside one second admitted the PR and then
    ejected the group, which is the exact failure this change exists to end.
    Both arms must pick the newer of a tie, so the selection expression must
    be byte-identical in both."""
    text = workflow_text()
    marker = 'select(.context == "critic-review")'
    selections = []
    cursor = 0
    while True:
        found = text.find(marker, cursor)
        if found < 0:
            break
        tail = text.find(".state", found)
        assert tail > found, "a critic-review selection has no .state read"
        selections.append(" ".join(text[found:tail].split()))
        cursor = tail
    assert len(selections) == 2, (
        "expected exactly two critic-review selections, found %d" % len(selections)
    )
    assert selections[0] == selections[1], (
        "the two arms must be one rule; they differ: %s vs %s"
        % (selections[0], selections[1])
    )
    assert "reverse" in selections[0], (
        "both arms must reverse before sort_by so the newer of a tie wins: %s" % selections[0]
    )


def test_the_merge_group_path_is_unchanged():
    # Embeds main's list so a future edit to a merge_group step must update
    # this pin deliberately rather than drift underneath it.
    expected = [
        ("Read the live merge queue and resolve this group's members", "github.event_name == 'merge_group'"),
        ("Require the newest critic-review status on every member", "github.event_name == 'merge_group'"),
        ("Re-read the queue and require the same membership", "github.event_name == 'merge_group'"),
    ]
    merge_group_steps = [
        (step["name"], step["if"])
        for step in job_steps("mq-review")
        if step["if"] == "github.event_name == 'merge_group'"
    ]
    assert merge_group_steps == expected


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
    body = step_body("mq-prewarm", "Wait for every dispatched CodeBuild staging receipt")
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
                for command in re.finditer(r"\b(gh|curl|aws)\s+", line):
                    prefix = line[:command.start()]
                    wrapped = re.search(r"\btimeout\s+[1-9][0-9]*\s+$", prefix)
                    assert wrapped or (
                        command.group(1) == "curl"
                        and re.search(r"--max-time\s+[1-9][0-9]*", line[command.end():])
                    ), line


# --------------------------------------------------------------------------- #
# Structural / falsifying pins with no local executable surface
# --------------------------------------------------------------------------- #

def test_it_fires_on_merge_group_and_pull_request_only():
    # pull_request_target was tried for R2 (a pull_request run executes the
    # workflow FILE from the PR's own head, so a PR could weaken its own
    # admission decision) and reverted: GitHub resolves a pull_request_target
    # trigger from the DEFAULT BRANCH's copy of the workflow, so introducing
    # it in the same PR that removes `pull_request:` left mq-review and
    # mq-prewarm never firing on that PR at all. Trigger stays pull_request;
    # R2 is acknowledged, not fixed, in the header comment.
    triggers = workflow_document()["on"]
    assert triggers["merge_group"]["types"] == ["checks_requested"]
    assert set(triggers["pull_request"]["types"]) == {
        "opened", "synchronize", "reopened", "ready_for_review",
    }
    assert triggers["pull_request"]["branches"] == ["main"]
    assert "status" not in triggers, "the refuted status trigger must never come back"


def test_no_status_trigger_and_supply_prewarm_match_mains_conditions():
    # Pins the shape a future edit could quietly regress into: reintroducing
    # `status:` in `on:`, or re-adding `github.event_name != 'status'` to
    # mq-supply's or mq-prewarm's `if:`.
    document = workflow_document()
    assert "status" not in document["on"]
    assert document["jobs"]["mq-supply"]["if"] == "github.event_name == 'merge_group'"
    assert document["jobs"]["mq-prewarm"]["if"] == "always()"
    for job in ("mq-supply", "mq-prewarm"):
        assert "status" not in document["jobs"][job]["if"]


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


def test_prewarm_pull_request_arm_publishes_a_deferred_success_and_calls_nothing():
    # mq-prewarm still defers unconditionally: it gates staging, not review.
    # mq-review's pull_request arm now decides admission instead; that
    # behavior is executed in the admission tests below, not pinned here.
    step = step_by_name("mq-prewarm", "Publish the deferred queue-preparation success")
    assert step["if"] == "github.event_name == 'pull_request'"
    body = step["run"]
    for forbidden in ("gh ", "curl", "git "):
        assert forbidden not in body, "mq-prewarm: pull_request arm must do nothing but notice"


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
    """S3 metadata supplies the producer ids; its run record and body must agree."""
    body = step_body("mq-supply", "Wait for the provider-bound speculative supply set")
    assert '.Metadata["repository-id"] == $repo' in body
    assert '.Metadata["run-id"] // empty' in body
    assert '.Metadata["run-attempt"] // empty' in body
    assert 'actions/runs/$CAND_RUN' in body
    assert '"$ATTEMPT_NUM" = "$CAND_ATTEMPT"' in body
    assert '.repository.id == $repo and .head_repository.id == $repo' in body
    assert '.build_run_id == $run and .build_run_attempt == $attempt' in body
    assert '.event == "workflow_dispatch"' in body
    assert '.head_branch == "main"' in body
    assert '.status == "completed"' in body
    assert '.conclusion == "success"' in body
    assert "--checksum-mode ENABLED spec-candidate.json" in body
    assert "openssl dgst -sha256 -binary spec-candidate.json" in body
    assert 'ACTUAL_CHECKSUM" = "$EXPECTED_CHECKSUM"' in body
    assert '${MQ_TRANSPORT_PREFIX}supply-set/$TREE.json' in body


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
    body = step_body("mq-prewarm", "Wait for every dispatched CodeBuild staging receipt")
    assert "timeout 60 aws codebuild batch-get-builds" in body
    assert "actions/artifacts" not in body
    assert "gh api" not in body
    assert 'staged-prewarm-receipt.json' in body
    assert '[ "$REC_SERVICE" = "$SERVICE" ]' in body
    assert 'EXPECTED_TAG="spec-$TREE-$SHA12"' in body
    assert '[ "$REC_TAG" = "$EXPECTED_TAG" ]' in body
    assert '[ "$REC_WEIGHTS" = "false" ]' in body
    assert '"$ATTEMPT" -lt "$TERRAFORM_RECEIPT_POLLS"' in body
    assert workflow_document()["env"]["TERRAFORM_RECEIPT_POLLS"] == "40"
    assert workflow_document()["env"]["TERRAFORM_RECEIPT_INTERVAL"] == "60"


def test_terraform_receipt_step_gates_on_supply_present_and_no_migration_refusal():
    step = step_by_name("mq-prewarm", "Wait for every dispatched CodeBuild staging receipt")
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


def test_secrets_used_are_exactly_github_token_and_oidc_role():
    names = set(re.findall(r"secrets\.([A-Za-z_][A-Za-z0-9_]*)", workflow_text()))
    assert names == {"AWS_MQ_PREWARM_READ_ROLE"}
    assert "github.token" in workflow_text()


def test_codebuild_credentials_are_scoped_to_prewarm():
    job = workflow_document()["jobs"]["mq-prewarm"]
    assert job["permissions"] == {"contents": "read", "actions": "read", "id-token": "write"}
    step = step_by_name("mq-prewarm", "Configure AWS credentials")
    assert step["uses"] == "aws-actions/configure-aws-credentials@v6.1.0"
    assert step["with"] == {
        "role-to-assume": "${{ secrets.AWS_MQ_PREWARM_READ_ROLE }}", "aws-region": "us-east-1",
    }
    assert step["if"] == step_by_name("mq-prewarm", "Wait for every dispatched")["if"]
    assert "TERRAFORM_REPO_TOKEN" not in workflow_text()


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


def _prewarm_evidence(tmp_path, entries, relay_source='env:\n  STAGE_SERVICES: "web"\n',
                      empty_arn=False, receipt_change=None, status="SUCCEEDED", log_case=None,
                      configured_services=("web",)):
    # Group-env fixtures previously configured app and web; now they configure web.
    binary = tmp_path / "bin"
    binary.mkdir(exist_ok=True)
    fake = binary / "gh"
    fake.write_text(textwrap.dedent('''\
        #!/usr/bin/env bash
        set -euo pipefail
        printf '%s\\n' "$*" >> gh-calls.txt
        for arg in "$@"; do
          case "$arg" in
            */actions/runs/77) cat relay-run.json; exit 0 ;;
            repos/LEAF-Solar-Design/leaf-web-demo) echo 555; exit 0 ;;
            */contents/*)
              echo "$arg" >> contents-calls.txt
              cat relay-source.json; exit 0 ;;
            */actions/artifacts/1/zip) cat relay.zip; exit 0 ;;
            */actions/artifacts\\?*)
              echo '{"artifacts":[{"id":1,"expired":false,"created_at":"2026-09-05"}]}'; exit 0 ;;
          esac
        done
        exit 1
        '''), encoding="utf-8", newline="\n")
    fake.chmod(0o755)
    aws = binary / "aws"
    aws.write_text(textwrap.dedent('''\
        #!/usr/bin/env bash
        set -euo pipefail
        printf '%s\\n' "$*" >> aws-calls.txt
        if [ "$1 $2" = "codebuild batch-get-builds" ]; then
          ID="$4"
          cat "build-${ID#*:}.json"
        elif [ "$1 $2" = "logs get-log-events" ]; then
          SERVICE="$6"
          TOKEN=""
          while [ "$#" -gt 0 ]; do
            if [ "$1" = --next-token ]; then TOKEN="$2"; fi
            shift
          done
          if [ -f endless ]; then
            N=$(cat log-count.txt 2>/dev/null || echo 0)
            N=$((N + 1))
            echo "$N" > log-count.txt
            printf '{"events":[],"nextForwardToken":"%s"}' "$N"
          elif [ -z "$TOKEN" ]; then
            cat "logs-$SERVICE-first.json"
          elif [ "$TOKEN" = first ]; then
            cat "logs-$SERVICE-last.json"
          elif [ "$TOKEN" = last ]; then
            echo '{"events":[],"nextForwardToken":"last"}'
          else
            exit 1
          fi
        else
          exit 1
        fi
        '''), encoding="utf-8", newline="\n")
    aws.chmod(0o755)
    response = {} if relay_source is None else {"content": base64.b64encode(relay_source.encode()).decode()}
    (tmp_path / "relay-source.json").write_text(json.dumps(response), encoding="utf-8")
    with zipfile.ZipFile(tmp_path / "relay.zip", "w") as archive:
        archive.writestr("prewarm-relay-receipt.json", json.dumps({
            "group": {"head_sha": "a" * 40}, "dispatched": entries,
            "producer": "codebuild", "relay_run_id": "77",
        }))
    from test_prewarm_staging_cutover_workflow import _s3_object, _s3_transport_fixture
    relay = {
        "group": {"head_sha": "a" * 40}, "dispatched": entries,
        "producer": "codebuild", "relay_run_id": "77", "relay_run_attempt": "1",
    }
    if configured_services != "absent":
        relay["configured_services"] = (list(configured_services)
                                        if isinstance(configured_services, tuple) else configured_services)
    _s3_transport_fixture(tmp_path, {
        "mq/leaf-web-demo/relay/" + "a" * 12 + "/77-1.json":
            _s3_object(relay, 77, 1, 555, "a" * 40, "prewarm-staging-group.yml"),
    })
    (tmp_path / "relay-run.json").write_text(json.dumps({
        "path": ".github/workflows/prewarm-staging-group.yml", "event": "workflow_dispatch",
        "head_branch": "main", "run_attempt": 1,
        "repository": {"id": 555}, "head_repository": {"id": 555},
    }), encoding="utf-8")
    for service, number in (("web", 101), ("app", 102)):
        build_id = _build_id(number)
        # Filenames use just the UUID: Windows does not allow ':' in a filename.
        record_name = "build-" + build_id.split(":")[1] + ".json"
        (tmp_path / record_name).write_text(json.dumps({"builds": [{
            "id": build_id, "buildStatus": status,
            "logs": {"groupName": "/aws/codebuild/leaf-deploy-terraform-staging", "streamName": service},
        }]}), encoding="utf-8")
        receipt = {
            "schema": "leaf.staging-prewarm-receipt.v1",
            "service": service,
            "staged_td_arn": "" if empty_arn else _staged_arn(service),
            "image_tag": "spec-" + "b" * 40 + "-" + "a" * 12,
            "run_id": "a" * 12 + "-77",
            "weights_touched": False,
            "producer": {"kind": "codebuild", "build_id": build_id},
        }
        if receipt_change == "old-key-only":
            receipt["task_definition_arn"] = receipt.pop("staged_td_arn")
        elif receipt_change == "missing-weights":
            receipt.pop("weights_touched")
        elif receipt_change:
            receipt.update(receipt_change)
        first = "LEAF_DEPLOY_RECEIPT " + json.dumps({"refused": True})
        last = "LEAF_DEPLOY_RECEIPT " + json.dumps(receipt)
        if log_case == "missing":
            first = last = "ordinary log line"
        elif log_case == "malformed":
            last = "LEAF_DEPLOY_RECEIPT {"
        elif log_case == "last-refusal":
            first, last = last, first
        elif log_case == "multiple-json":
            last += " {}"
        for page, message, token in (("first", first, "first"), ("last", last, "last")):
            (tmp_path / f"logs-{service}-{page}.json").write_text(json.dumps({
                "events": [{"message": "ordinary log line"}, {"message": message}],
                "nextForwardToken": token,
            }), encoding="utf-8")
    if log_case == "endless":
        (tmp_path / "endless").write_text("yes", encoding="utf-8")


def _build_id(number):
    return "leaf-deploy-terraform-staging:00000000-0000-0000-0000-" + f"{number:012x}"


def _staged_arn(service):
    return "arn:aws:ecs:us-east-1:123456789012:task-definition/leaf-platform-" + service + "-alt:254"


def _dispatch(service, run_id, disposition="dispatched"):
    return {"service": service, "build_id": _build_id(run_id) if type(run_id) is int else run_id,
            "disposition": disposition}


@needs_shell
@pytest.mark.parametrize("entries,configured,error", [
    ([], ["web"], "relay dispatched nothing"),
    ([_dispatch("web", 101, "dispatch-failed")], ["web"], "web"),
    ([_dispatch("app", 102, "unresolved")], ["app"], "app"),
    ([_dispatch("app", None)], ["app"], "app"),
    ([_dispatch("web", 101)], ["app", "web"], "relay dispatched services differ from configured services"),
    ([_dispatch("web", 101)], ["web"], None),
    ([_dispatch("web", 101)], [], "relay configured service list absent or unparsable"),
    ([_dispatch("web", 101)], "web", "relay configured service list absent or unparsable"),
    ([_dispatch("web", 101)], "absent", "relay configured service list absent or unparsable"),
    ([_dispatch("web", 101)], ["web", "web"], "relay configured service list absent or unparsable"),
    ([_dispatch("web", 101)], [1], "relay configured service list absent or unparsable"),
    ([_dispatch("web", 101)], ["bad_service"], "relay configured service list absent or unparsable"),
])
def test_relay_dispatched_set_executed(tmp_path, entries, configured, error):
    _prewarm_evidence(tmp_path, entries, configured_services=configured)
    result = run_step(step_body("mq-prewarm", "Wait for the relay's"), tmp_path,
                      {"GROUP_HEAD_SHA": "a" * 40})
    if error:
        assert result["__returncode__"] != 0
        assert error in result["__stdout__"] + result["__stderr__"]
    else:
        assert result["__returncode__"] == 0, result
        assert json.loads(result["dispatched_json"]) == {"web": _build_id(101)}
        assert result["relay_run_id"] == "77"
        assert "configured services: web" in result["__stdout__"]
        assert "dispatched services: web" in result["__stdout__"]
        assert "contents/" not in (tmp_path / "gh-calls.txt").read_text()
        waited = run_step(step_body("mq-prewarm", "Wait for every dispatched"), tmp_path,
                          {"GROUP_HEAD_SHA": "a" * 40, "TREE": "b" * 40,
                           "DISPATCHED_JSON": result["dispatched_json"],
                           "RELAY_RUN_ID": result["relay_run_id"]})
        assert waited["__returncode__"] == 0, waited
        assert "staged task definitions: " + _staged_arn("web") in waited["__stdout__"]


@needs_shell
def test_relay_configuration_change_takes_effect_on_the_next_group(tmp_path):
    # The relay runs main's text. A group changing the list must still merge;
    # its new configuration takes effect when the next group relay runs.
    _prewarm_evidence(tmp_path, [_dispatch("web", 101), _dispatch("app", 102)],
                      relay_source='env:\n  STAGE_SERVICES: "web"\n',
                      configured_services=["app", "web"])
    result = run_step(step_body("mq-prewarm", "Wait for the relay's"), tmp_path,
                      {"GROUP_HEAD_SHA": "a" * 40})
    assert result["__returncode__"] == 0, result
    assert json.loads(result["dispatched_json"]) == {"web": _build_id(101), "app": _build_id(102)}
    assert "configured services: app web" in result["__stdout__"]
    assert "contents/" not in (tmp_path / "gh-calls.txt").read_text()

    # Previously restoration added app on the next group; this pause removes it.
    # The landing group's prior two-service receipt remains the transition case.
    next_group = tmp_path / "next-group"
    next_group.mkdir()
    _prewarm_evidence(next_group, [_dispatch("web", 101)],
                      configured_services=["web"])
    result = run_step(step_body("mq-prewarm", "Wait for the relay's"), next_group,
                      {"GROUP_HEAD_SHA": "a" * 40})
    assert result["__returncode__"] == 0, result
    assert json.loads(result["dispatched_json"]) == {
        "web": _build_id(101),
    }
    assert "configured services: web" in result["__stdout__"]
    assert "contents/" not in (next_group / "gh-calls.txt").read_text()


@needs_shell
@pytest.mark.parametrize("dispatched,empty_arn,accepted", [
    ({"web": _build_id(101), "app": _build_id(102)}, False, True),
    ({"web": _build_id(101), "app": _build_id(102)}, True, False),
    ({}, False, False),
])
def test_terraform_wait_uses_dispatched_set_and_requires_arns(tmp_path, dispatched, empty_arn, accepted):
    _prewarm_evidence(tmp_path, [], empty_arn=empty_arn)
    result = run_step(step_body("mq-prewarm", "Wait for every dispatched"), tmp_path,
                      {"GROUP_HEAD_SHA": "a" * 40, "TREE": "b" * 40,
                       "DISPATCHED_JSON": json.dumps(dispatched)})
    assert (result["__returncode__"] == 0) == accepted, result
    if accepted:
        calls = (tmp_path / "aws-calls.txt").read_text()
        assert _build_id(101) in calls and _build_id(102) in calls
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
    ({"service": "web"}, "service mismatch"),
    ({"image_tag": "wrong"}, "image_tag mismatch"),
    ({"weights_touched": True}, "weights_touched"),
    ({"weights_touched": "false"}, "weights_touched"),
    ("missing-weights", "weights_touched"),
    ({"producer": {"kind": "actions", "build_id": _build_id(102)}}, "producer mismatch"),
    ({"producer": {"kind": "codebuild", "build_id": _build_id(101)}}, "producer mismatch"),
])
def test_terraform_wait_reads_real_receipt_schema(tmp_path, receipt_change, error):
    _prewarm_evidence(tmp_path, [], receipt_change=receipt_change)
    result = run_step(step_body("mq-prewarm", "Wait for every dispatched"), tmp_path,
                      {"GROUP_HEAD_SHA": "a" * 40, "TREE": "b" * 40,
                       "DISPATCHED_JSON": json.dumps({"app": _build_id(102)})})
    if error:
        assert result["__returncode__"] != 0, result
        assert error in result["__stdout__"] + result["__stderr__"]
    else:
        assert result["__returncode__"] == 0, result
        assert "staged task definitions: " + _staged_arn("app") in result["__stdout__"]


@needs_shell
@pytest.mark.parametrize("status", ["FAILED", "FAULT", "STOPPED", "TIMED_OUT", "IN_PROGRESS"])
def test_codebuild_terminal_failures_name_the_status(tmp_path, status):
    _prewarm_evidence(tmp_path, [], status=status)
    result = run_step(step_body("mq-prewarm", "Wait for every dispatched"), tmp_path, {
        "GROUP_HEAD_SHA": "a" * 40, "TREE": "b" * 40,
        "DISPATCHED_JSON": json.dumps({"app": _build_id(102)}),
    })
    assert result["__returncode__"] != 0, result
    message = result["__stdout__"] + result["__stderr__"]
    assert ("never completed" if status == "IN_PROGRESS" else status) in message
    assert "logs get-log-events" not in (tmp_path / "aws-calls.txt").read_text()


@needs_shell
@pytest.mark.parametrize("log_case,error", [
    (None, None),
    ("missing", "no LEAF_DEPLOY_RECEIPT"),
    ("malformed", "invalid receipt JSON"),
    ("last-refusal", "schema mismatch"),
    ("multiple-json", "invalid receipt JSON"),
    ("endless", "exceeded 200 pages"),
])
def test_codebuild_logs_paginate_and_validate_only_the_last_receipt(tmp_path, log_case, error):
    _prewarm_evidence(tmp_path, [], log_case=log_case)
    result = run_step(step_body("mq-prewarm", "Wait for every dispatched"), tmp_path, {
        "GROUP_HEAD_SHA": "a" * 40, "TREE": "b" * 40,
        "DISPATCHED_JSON": json.dumps({"app": _build_id(102)}),
    })
    if error:
        assert result["__returncode__"] != 0, result
        assert error in result["__stdout__"] + result["__stderr__"], result
    else:
        assert result["__returncode__"] == 0, result
        assert _staged_arn("app") in result["__stdout__"]
    calls = (tmp_path / "aws-calls.txt").read_text().splitlines()
    logs = [shlex.split(call) for call in calls if call.startswith("logs ")]
    assert len(logs) == (200 if log_case == "endless" else 3)
    assert all("--start-from-head" in call for call in logs)
    assert all(call[call.index("--log-group-name") + 1] ==
               "/aws/codebuild/leaf-deploy-terraform-staging" for call in logs)
    assert all(call[call.index("--log-stream-name") + 1] == "app" for call in logs)
    if log_case != "endless":
        assert "--next-token" not in logs[0]
        assert logs[1][logs[1].index("--next-token") + 1] == "first"
        assert logs[2][logs[2].index("--next-token") + 1] == "last"


def test_mq_supply_s3_permissions_role_and_exact_key():
    document = workflow_document()
    assert document["env"]["MQ_TRANSPORT_BUCKET"] == "leaf-mq-transport-807034087062-us-east-1"
    assert document["env"]["MQ_TRANSPORT_PREFIX"] == "mq/leaf-web-demo/"
    job = document["jobs"]["mq-supply"]
    assert "environment" not in job
    assert job["permissions"] == {"id-token": "write", "contents": "read", "actions": "read"}
    credentials = next(step for step in job["steps"] if step.get("uses", "").startswith("aws-actions/configure"))
    assert credentials["with"] == {
        "role-to-assume": "${{ secrets.AWS_MQ_PREWARM_READ_ROLE }}", "aws-region": "us-east-1",
    }
    assert job["steps"].index(credentials) < job["steps"].index(step_by_name("mq-supply", "Wait for the provider"))
    body = step_body("mq-supply", "Wait for the provider")
    assert '${MQ_TRANSPORT_PREFIX}supply-set/$TREE.json' in body
    assert 'list-objects-v2 --bucket "$MQ_TRANSPORT_BUCKET" --prefix "$KEY" --max-keys 1' in body
    assert 'any(.Contents[]?; .Key == $key)' in body
    assert "actions/artifacts" not in body
    relay_steps = document["jobs"]["mq-prewarm"]["steps"]
    relay_credentials = next(step for step in relay_steps if step.get("name") == "Assume the relay transport read role")
    assert relay_credentials["if"] == step_by_name("mq-prewarm", "Wait for the relay's")["if"]
    assert relay_steps.index(relay_credentials) < relay_steps.index(step_by_name("mq-prewarm", "Wait for the relay's"))


@needs_shell
def test_mq_supply_absence_requires_exact_equality_without_head_executed(tmp_path):
    from test_prewarm_staging_cutover_workflow import _s3_transport_fixture
    for scenario in ("missing", "neighbor"):
        work = tmp_path / scenario
        work.mkdir()
        objects, _, _ = _s3_supply_fixture(work)
        key, item = next(iter(objects.items()))
        objects = {} if scenario == "missing" else {key + ".foreign": item}
        _s3_transport_fixture(work, objects, {"head-object": "AccessDenied"})
        result = run_step(step_body("mq-supply", "Wait for the provider"), work, {"TREE": "b" * 40})
        assert result["__returncode__"] == 1
        assert "no exact provider-bound" in result["__stdout__"]
        calls = [json.loads(line) for line in (work / "s3-calls.jsonl").read_text().splitlines()]
        assert [call[1] for call in calls] == ["list-objects-v2"]
        assert calls[0][calls[0].index("--prefix") + 1] == key
        assert calls[0][calls[0].index("--max-keys") + 1] == "1"


def _s3_supply_fixture(work):
    from test_prewarm_staging_cutover_workflow import _s3_object, _s3_transport_fixture
    tree, sha = "b" * 40, "a" * 40
    body = {
        "release_source_tree": tree, "release_source_revision": sha,
        "build_run_id": 99, "build_run_attempt": 2,
        "services": {name: {
            "producer_run_id": 99, "producer_run_attempt": 2,
            "producer_source_revision": sha, "producer_source_tree": tree,
        } for name in ("app", "broker", "canonical_worker", "harness", "web")},
    }
    key = "mq/leaf-web-demo/supply-set/" + tree + ".json"
    objects = {key: _s3_object(body, 99, 2, 555, sha)}
    _s3_transport_fixture(work, objects)
    provider = {
        "path": ".github/workflows/build-platform-images.yml@refs/heads/main",
        "event": "workflow_dispatch", "head_branch": "main", "run_attempt": 2,
        "status": "completed", "conclusion": "success",
        "repository": {"id": 555}, "head_repository": {"id": 555},
    }
    (work / "run.json").write_text(json.dumps(provider))
    fake = work / "bin" / "gh"
    fake.write_text('#!/usr/bin/env bash\ncase "$*" in *"/actions/runs/"*) cat run.json ;; *) echo 555 ;; esac\n',
                    encoding="utf-8", newline="\n")
    fake.chmod(0o755)
    return objects, provider, body


@needs_shell
def test_mq_supply_s3_metadata_checksum_and_run_crosscheck_executed(tmp_path):
    from test_prewarm_staging_cutover_workflow import _s3_object, _s3_transport_fixture
    for scenario in ("valid", "checksum", "body-run", "body-attempt", "body-source", "metadata-run",
                     "metadata-repo", "run-attempt", "run-path", "run-event", "run-branch",
                     "run-status", "run-conclusion", "run-repository"):
        work = tmp_path / scenario
        work.mkdir()
        objects, provider, body = _s3_supply_fixture(work)
        key, item = next(iter(objects.items()))
        if scenario == "checksum":
            item["ChecksumSHA256"] = "wrong"
        elif scenario.startswith("body-"):
            field = {"body-run": "build_run_id", "body-attempt": "build_run_attempt",
                     "body-source": "release_source_revision"}[scenario]
            body[field] = "c" * 40 if scenario == "body-source" else 123
            objects[key] = _s3_object(body, 99, 2, 555, "a" * 40)
        elif scenario == "metadata-run":
            item["Metadata"]["run-id"] = "123"
        elif scenario == "metadata-repo":
            item["Metadata"]["repository-id"] = "123"
        elif scenario.startswith("run-"):
            field = {"run-attempt": "run_attempt", "run-path": "path", "run-event": "event",
                     "run-branch": "head_branch", "run-status": "status",
                     "run-conclusion": "conclusion", "run-repository": "head_repository"}[scenario]
            provider[field] = 3 if scenario == "run-attempt" else {"id": 123} if scenario == "run-repository" else "wrong"
        (work / "run.json").write_text(json.dumps(provider))
        _s3_transport_fixture(work, objects)
        result = run_step(step_body("mq-supply", "Wait for the provider"), work, {"TREE": "b" * 40})
        assert (result["__returncode__"] == 0) == (scenario == "valid"), result
        if scenario == "valid":
            assert result["producer_run_id"] == "99" and result["producer_run_attempt"] == "2"
            calls = [json.loads(line) for line in (work / "s3-calls.jsonl").read_text().splitlines()]
            assert [call[1] for call in calls] == ["list-objects-v2", "head-object", "get-object"]
            assert calls[0][calls[0].index("--prefix") + 1] == key
            assert calls[0][calls[0].index("--max-keys") + 1] == "1"
            assert all(call[call.index("--key") + 1] == key for call in calls[1:])
            assert calls[2][calls[2].index("--checksum-mode") + 1] == "ENABLED"


@needs_shell
def test_mq_relay_s3_newest_wins_with_key_tie_break_executed(tmp_path):
    from test_prewarm_staging_cutover_workflow import _s3_transport_fixture
    for scenario in ("timestamp", "tie"):
        work = tmp_path / scenario
        work.mkdir()
        _prewarm_evidence(work, [_dispatch("web", 101)])
        objects = json.loads((work / "s3-objects.json").read_text())
        key, item = next(iter(objects.items()))
        old = json.loads(json.dumps(item))
        old["ChecksumSHA256"] = "bad"
        if scenario == "timestamp":
            old["LastModified"] = "2026-09-06T00:00:00Z"
            old_key = key.replace("/77-1.json", "/99-1.json")
        else:
            old_key = key.replace("/77-1.json", "/76-1.json")
        _s3_transport_fixture(work, {key: item, old_key: old})
        result = run_step(step_body("mq-prewarm", "Wait for the relay's"), work, {"GROUP_HEAD_SHA": "a" * 40})
        assert result["__returncode__"] == 0, result
        calls = [json.loads(line) for line in (work / "s3-calls.jsonl").read_text().splitlines()]
        listings = [call for call in calls if call[1] == "list-objects-v2"]
        assert len(listings) == 1
        assert listings[0][listings[0].index("--prefix") + 1] == "mq/leaf-web-demo/relay/" + "a" * 12 + "/"
        reads = [call for call in calls if call[1] in ("head-object", "get-object")]
        assert all(call[call.index("--key") + 1] == key for call in reads)
        assert reads[-1][reads[-1].index("--checksum-mode") + 1] == "ENABLED"


@needs_shell
def test_mq_s3_access_errors_fail_from_own_code(tmp_path):
    from test_prewarm_staging_cutover_workflow import _s3_transport_fixture
    for job, operations in (("mq-supply", ("list-objects-v2", "head-object", "get-object")),
                            ("mq-prewarm", ("list-objects-v2", "head-object", "get-object"))):
        for operation in operations:
            for error in ("AccessDenied", "NoSuchBucket"):
                work = tmp_path / (job + operation + error)
                work.mkdir()
                if job == "mq-supply":
                    objects, _, _ = _s3_supply_fixture(work)
                    fragment = "Wait for the provider"
                else:
                    _prewarm_evidence(work, [_dispatch("web", 101)])
                    objects = json.loads((work / "s3-objects.json").read_text())
                    fragment = "Wait for the relay's"
                _s3_transport_fixture(work, objects, {operation: error})
                result = run_step(step_body(job, fragment), work, {"TREE": "b" * 40, "GROUP_HEAD_SHA": "a" * 40})
                assert result["__returncode__"] == 1, result
                assert "::error::" in result["__stdout__"]
                assert "bucket leaf-mq-transport-807034087062-us-east-1" in result["__stdout__"]
                assert "transport policy" in result["__stdout__"]


@needs_shell
def test_mq_relay_s3_checksum_and_metadata_refuse_executed(tmp_path):
    from test_prewarm_staging_cutover_workflow import _s3_transport_fixture
    for scenario in ("checksum", "metadata", "attempt"):
        work = tmp_path / scenario
        work.mkdir()
        _prewarm_evidence(work, [_dispatch("web", 101)])
        objects = json.loads((work / "s3-objects.json").read_text())
        item = next(iter(objects.values()))
        if scenario == "checksum":
            item["ChecksumSHA256"] = "bad"
        elif scenario == "metadata":
            item["Metadata"]["run-id"] = "999"
        else:
            provider = json.loads((work / "relay-run.json").read_text())
            provider["run_attempt"] = 2
            (work / "relay-run.json").write_text(json.dumps(provider))
        _s3_transport_fixture(work, objects)
        result = run_step(step_body("mq-prewarm", "Wait for the relay's"), work, {"GROUP_HEAD_SHA": "a" * 40})
        assert result["__returncode__"] == 1, result
        assert "::error::" in result["__stdout__"]


@needs_shell
def test_mq_relay_s3_invalid_newest_never_falls_back_executed(tmp_path):
    from test_prewarm_staging_cutover_workflow import _s3_transport_fixture
    _prewarm_evidence(tmp_path, [_dispatch("web", 101)])
    objects = json.loads((tmp_path / "s3-objects.json").read_text())
    old_key, old = next(iter(objects.items()))
    newest = json.loads(json.dumps(old))
    newest["LastModified"] = "2026-09-08T00:00:00Z"
    newest["ChecksumSHA256"] = "bad"
    newest_key = old_key.replace("/77-1.json", "/78-1.json")
    _s3_transport_fixture(tmp_path, {old_key: old, newest_key: newest})
    result = run_step(step_body("mq-prewarm", "Wait for the relay's"), tmp_path, {"GROUP_HEAD_SHA": "a" * 40})
    assert result["__returncode__"] == 1, result
    assert "::error::mq-prewarm: S3 checksum mismatch." in result["__stdout__"]
    calls = [json.loads(line) for line in (tmp_path / "s3-calls.jsonl").read_text().splitlines()]
    assert [call[1] for call in calls] == ["list-objects-v2", "head-object", "get-object"]
    assert all(call[call.index("--key") + 1] == newest_key for call in calls[1:])


@needs_shell
def test_mq_relay_absence_never_heads_an_unlisted_key_executed(tmp_path):
    from test_prewarm_staging_cutover_workflow import _s3_transport_fixture
    _s3_transport_fixture(tmp_path, {}, {"head-object": "AccessDenied", "get-object": "AccessDenied"})
    result = run_step(step_body("mq-prewarm", "Wait for the relay's"), tmp_path, {"GROUP_HEAD_SHA": "a" * 40})
    assert result["__returncode__"] == 1
    assert "no matching prewarm-relay-receipt" in result["__stdout__"]
    calls = [json.loads(line) for line in (tmp_path / "s3-calls.jsonl").read_text().splitlines()]
    assert [call[1] for call in calls] == ["list-objects-v2"]
    assert calls[0][calls[0].index("--prefix") + 1] == "mq/leaf-web-demo/relay/" + "a" * 12 + "/"
