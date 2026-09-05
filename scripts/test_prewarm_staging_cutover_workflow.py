"""Executable contract for the prewarm staging relay (tf design W2).

This relay stages UNMERGED code onto a task that runs with the real staging
task role, so two of its decisions are security controls rather than
optimisations, and both are EXECUTED here against the real step bodies rather
than asserted by reading the workflow text:

* **Eligibility.** Only a PR with a standing approval (or the explicit
  `stage-cutover` label) may be staged. An approval that a later
  CHANGES_REQUESTED overrode must not still buy a stage -- that is the case a
  text assertion would never catch.
* **Migration-surface refusal.** A candidate touching `platform/migrations/`
  or `platform/db.py` is never staged, and the check FAILS CLOSED: an
  unreadable preview refuses rather than proceeds. The same step derives the
  `spec-<tree40>-<preview12>` tag, so executing it also pins the tag shape the
  terraform deploy admits only under `deploy_mode=prewarm`.

The remaining assertions pin the properties that have no executable surface
here: which events fire, what the dispatch says, and that a failed dispatch
degrades instead of reddening a PR.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import textwrap
import zipfile

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "prewarm-staging-cutover.yml"

GROUP_WORKFLOW = WORKFLOW.with_name("prewarm-staging-group.yml")
BUILD_WORKFLOW = WORKFLOW.with_name("build-platform-images.yml")


def _usable_bash() -> str:
    """A bash that can itself run jq and git, which is not the same question as
    whether the HOST has them: on Windows the `bash` on PATH can be a WSL shim
    with its own PATH, and probing shutil.which would let these tests run
    against a shell where every jq call silently fails."""
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


def _has_unzip() -> bool:
    """The readiness step's own tests need unzip, unlike every other step
    exercised in this file: probe it separately so a dev box without it
    skips just these tests instead of failing them (CI always has one)."""
    if not BASH:
        return False
    try:
        subprocess.run(
            [BASH, "-c", "command -v unzip >/dev/null"],
            check=True,
            capture_output=True,
            timeout=30,
        )
    except Exception:
        return False
    return True


needs_unzip = pytest.mark.skipif(
    not _has_unzip(),
    reason="no unzip in this bash (CI always has one)",
)


def workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def workflow_document() -> dict:
    # BaseLoader keeps the literal `on` key instead of YAML 1.1's boolean True.
    return yaml.load(workflow_text(), Loader=yaml.BaseLoader)


def group_workflow_document() -> dict:
    return yaml.load(GROUP_WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def build_workflow_document() -> dict:
    return yaml.load(BUILD_WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def _step_by_id(job_steps: list, step_id: str) -> dict:
    for step in job_steps:
        if step.get("id") == step_id:
            return step
    raise AssertionError("no step id=%r among %d steps" % (step_id, len(job_steps)))


_GH_EXPRESSION = re.compile(r"\$\{\{\s*([^}]+?)\s*\}\}")


def _bind_gh_expressions(script: str, bindings: dict) -> str:
    """Replace `${{ ... }}` GitHub Actions expressions the way the runner
    would, so the producer's own step body can be executed verbatim rather
    than re-typed as a hand-built fixture."""

    def repl(match: "re.Match[str]") -> str:
        key = match.group(1)
        if key not in bindings:
            raise AssertionError("unbound GitHub expression %r in producer step" % key)
        return bindings[key]

    return _GH_EXPRESSION.sub(repl, script)


def workflow_jobs() -> dict:
    return {**workflow_document()["jobs"], **group_workflow_document()["jobs"]}


def step_body(job: str, name_fragment: str) -> str:
    for step in workflow_jobs()[job]["steps"]:
        if name_fragment in step.get("name", "") and "run" in step:
            return step["run"]
    raise AssertionError("no %r step with a run body in job %s" % (name_fragment, job))


def run_step(body: str, workdir: Path, env: dict) -> dict:
    """Run one step body and return its $GITHUB_OUTPUT as a dict."""
    # Everything the shell sees is RELATIVE to workdir, and the environment is
    # INLINED into the script rather than passed through `subprocess(env=...)`.
    # Two portability traps, both real on the author's machine and invisible on
    # CI: a Windows absolute path handed to bash loses its backslashes, and a
    # bash that is a WSL shim drops every inherited variable not named in
    # WSLENV. Inlining makes one harness that runs in both places.
    output = workdir / "step-output.txt"
    output.write_text("", encoding="utf-8")
    summary = workdir / "step-summary.md"
    summary.write_text("", encoding="utf-8")
    exports = {
        "GITHUB_OUTPUT": "step-output.txt",
        "GITHUB_STEP_SUMMARY": "step-summary.md",
        "GITHUB_REPOSITORY": "LEAF-Solar-Design/leaf-web-demo",
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
    assert completed.returncode == 0, completed.stderr + completed.stdout
    parsed = {}
    for line in output.read_text(encoding="utf-8").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            parsed[key] = value
    return parsed


def fake_gh(workdir: Path, pull: dict, reviews: list) -> None:
    """A `gh` that answers the two API reads the eligibility step makes."""
    binary = workdir / "bin"
    binary.mkdir(exist_ok=True)
    (workdir / "pull.json").write_text(json.dumps(pull), encoding="utf-8")
    (workdir / "reviews.json.fixture").write_text(json.dumps(reviews), encoding="utf-8")
    script = binary / "gh"
    script.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            for arg in "$@"; do
              case "$arg" in
                *"/reviews"*) cat "$(dirname "$0")/../reviews.json.fixture"; exit 0 ;;
              esac
            done
            cat "$(dirname "$0")/../pull.json"
            """
        ),
        encoding="utf-8",
        newline="\n",
    )
    script.chmod(0o755)


def review(login: str, state: str, submitted_at: str) -> dict:
    return {"user": {"login": login}, "state": state, "submitted_at": submitted_at}


ELIGIBILITY_ENV = {"PR_NUMBER": "42", "STAGE_LABEL": "stage-cutover", "GH_TOKEN": "unused"}


@needs_shell
@pytest.mark.parametrize(
    "reviews,labels,expected,because",
    [
        ([review("ada", "APPROVED", "2026-08-17T10:00:00Z")], [], "true", "standing approval"),
        ([], [{"name": "stage-cutover"}], "true", "explicit label override"),
        ([], [], "false", "nothing makes it eligible"),
        (
            [
                review("ada", "APPROVED", "2026-08-17T10:00:00Z"),
                review("ada", "CHANGES_REQUESTED", "2026-08-17T11:00:00Z"),
            ],
            [],
            "false",
            "the approval was overridden by the same reviewer",
        ),
        (
            [
                review("ada", "APPROVED", "2026-08-17T10:00:00Z"),
                review("linus", "CHANGES_REQUESTED", "2026-08-17T11:00:00Z"),
            ],
            [],
            "false",
            "another reviewer is still blocking",
        ),
        (
            [
                review("ada", "CHANGES_REQUESTED", "2026-08-17T10:00:00Z"),
                review("ada", "APPROVED", "2026-08-17T11:00:00Z"),
            ],
            [],
            "true",
            "the reviewer's latest verdict approves",
        ),
        (
            [review("ada", "APPROVED", "2026-08-17T10:00:00Z"), review("bot", "COMMENTED", "2026-08-17T12:00:00Z")],
            [],
            "true",
            "a comment carries no verdict",
        ),
        (
            [review("ada", "CHANGES_REQUESTED", "2026-08-17T10:00:00Z")],
            [{"name": "stage-cutover"}],
            "false",
            "the label never overrides a standing changes-requested",
        ),
    ],
)
def test_eligibility_is_decided_by_the_standing_review_verdict(tmp_path, reviews, labels, expected, because):
    fake_gh(tmp_path, {"state": "open", "labels": labels}, reviews)
    result = run_step(step_body("stage", "Decide stage eligibility"), tmp_path, ELIGIBILITY_ENV)
    assert result["eligible"] == expected, "%s: %s" % (because, result.get("reason"))


@needs_shell
def test_a_closed_pull_request_is_never_staged(tmp_path):
    fake_gh(tmp_path, {"state": "closed", "labels": [{"name": "stage-cutover"}]}, [])
    result = run_step(step_body("stage", "Decide stage eligibility"), tmp_path, ELIGIBILITY_ENV)
    assert result["eligible"] == "false"


def _preview_repo(tmp_path: Path, changed: str) -> Path:
    """A two-commit repo standing in for a merge preview and its first parent."""
    repo = tmp_path / "repo"
    repo.mkdir()
    run = lambda *argv: subprocess.run(argv, cwd=repo, check=True, capture_output=True)
    run("git", "init", "-q")
    run("git", "config", "user.email", "t@example.invalid")
    run("git", "config", "user.name", "t")
    (repo / "platform").mkdir()
    (repo / "platform" / "migrations").mkdir()
    (repo / "platform" / "db.py").write_text("baseline\n", encoding="utf-8")
    (repo / "platform" / "migrations" / "0001.sql").write_text("baseline\n", encoding="utf-8")
    (repo / "server.py").write_text("baseline\n", encoding="utf-8")
    run("git", "add", "-A")
    run("git", "commit", "-qm", "main tip")
    target = repo / changed
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("changed\n", encoding="utf-8")
    run("git", "add", "-A")
    run("git", "commit", "-qm", "preview")
    return repo


CANDIDATE_ENV = {"MIGRATION_SURFACE": "platform/migrations/ platform/db.py"}


@needs_shell
@pytest.mark.parametrize(
    "changed,stageable",
    [
        ("server.py", "true"),
        ("platform/db.py", "false"),
        ("platform/migrations/0002.sql", "false"),
    ],
)
def test_a_candidate_touching_the_migration_surface_is_refused(tmp_path, changed, stageable):
    repo = _preview_repo(tmp_path, changed)
    (repo / "bin").mkdir()
    result = run_step(step_body("stage", "Refuse a candidate that touches"), repo, CANDIDATE_ENV)
    assert result["stageable"] == stageable
    if stageable == "true":
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
        ).stdout.strip()
        tree = subprocess.run(
            ["git", "rev-parse", "HEAD^{tree}"], cwd=repo, capture_output=True, text=True, check=True
        ).stdout.strip()
        assert result["image_tag"] == "spec-%s-%s" % (tree, head[:12])
        assert result["migration_refusal"] == "false"
    else:
        assert "image_tag" not in result


@needs_shell
def test_an_unreadable_preview_fails_closed(tmp_path):
    """No first parent means we cannot know what we would stage."""
    repo = tmp_path / "shallow"
    repo.mkdir()
    (repo / "bin").mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "only.txt").write_text("one commit\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "root"], cwd=repo, check=True, capture_output=True)
    result = run_step(step_body("stage", "Refuse a candidate that touches"), repo, CANDIDATE_ENV)
    assert result["stageable"] == "false"


def test_it_fires_on_approval_pushes_labels_and_closes():
    document = workflow_document()
    triggers = document["on"]
    assert triggers["pull_request_review"]["types"] == ["submitted"]
    assert set(triggers["pull_request_target"]["types"]) == {"synchronize", "labeled", "closed"}
    assert triggers["pull_request_target"]["branches"] == ["main"]


def test_no_trigger_runs_pr_authored_workflow_text_next_to_the_token():
    """The trigger set is a secret-handling decision.

    A `pull_request` run executes the PR's OWN copy of this file while
    TERRAFORM_REPO_TOKEN is in scope, so any same-repo branch could rewrite the
    workflow and take the token. `pull_request_target` and `pull_request_review`
    both run the DEFAULT BRANCH's copy. This is the boundary
    speculate-platform-images holds by dispatching on the main ref.
    """
    triggers = workflow_document()["on"]
    assert "pull_request" not in triggers, (
        "this workflow holds a cross-repository token; use pull_request_target "
        "so the text that runs is always reviewed"
    )


def test_the_preview_checkout_is_never_executed():
    """The other half of the boundary: read the preview, never run it.

    With pull_request_target the token is in scope, so a step that executed
    anything out of the merge preview would hand it to the PR author. Only git
    plumbing may touch that checkout.
    """
    body = step_body("stage", "Refuse a candidate that touches")
    for command in ("git rev-parse", "git diff"):
        assert command in body
    for forbidden in ("python", "bash ", "sh ", "npm", "make", "./"):
        assert forbidden not in body, (
            "the merge preview is read, never executed: %r appears in the step" % forbidden
        )


def test_a_close_is_never_cancelled_by_a_newer_stage():
    concurrency = workflow_document()["concurrency"]
    assert "descale" in concurrency["group"] and "stage" in concurrency["group"]
    # Extended for the group path (a merge group always cancels in progress,
    # since the queue is serial and there is no close event to protect), but
    # a PR close (action == 'closed') must still never be cancelled.
    assert concurrency["cancel-in-progress"] == (
        "${{ github.event_name == 'merge_group' || github.event.action != 'closed' }}"
    )


def test_only_same_repo_non_fork_non_draft_pull_requests_stage():
    condition = workflow_document()["jobs"]["stage"]["if"]
    assert "github.event.pull_request.head.repo.fork == false" in condition
    assert "head.repo.full_name == github.repository" in condition
    assert "draft == false" in condition
    assert "base.ref == 'main'" in condition
    assert "github.event.review.state == 'approved'" in condition


def test_the_dispatch_stages_both_colours_on_the_prewarm_rail():
    body = step_body("stage", "Dispatch the prewarm")
    assert "deploy_mode=prewarm" in body
    assert "expected_task_definition=auto-live" in body
    assert "deploy_strategy=bluegreen" in body
    assert "for SERVICE in $STAGE_SERVICES" in body
    # Staging is an optimisation: a refused or failed dispatch degrades to the
    # measured fallback warm and must never redden the PR.
    assert "::warning::Prewarm dispatch for $SERVICE failed" in body
    # Identity, never inference. First choice is the run id GitHub itself
    # reports, and it is verified against the workflow path and run-name before
    # it is trusted; the mark scan is the fallback for a gh that reports
    # nothing, and it too requires exactly one match.
    assert "actions/runs/[0-9]+" in body
    assert "did not verify as this dispatch" in body
    assert "if length == 1 then .[0].databaseId else empty end" in body


def test_web_and_app_are_staged_again_because_the_merge_group_makes_the_stage_fresh():
    """The freshness gap that paused `app` (#1067, #1068) is closed by the group path.

    `app` was staged (#1055), paused when the posture gate refused a prewarm
    over an open drawing-write lane (#1058), restored once terraform #1474 admitted an
    idle weight-0 prewarm (#1060), then paused again because the PR-mode stage
    is taken on the merge PREVIEW and a busy main could move under it before
    the real merge (#1067, #1068): the merge's actual tree then had no
    speculative supply set, and the build rebuilt.

    The merge-group job stages the group's own head, whose first parent is
    checked equal to its base at stage time (`stage-group`'s "Require the
    group head's first parent to equal its base" step), so it is fresh at stage
    time. Later invalidation or replay remains possible. Both colours are
    back in STAGE_SERVICES for that path; the PR path also re-covers app,
    which is accepted only because the PR triggers are provisional until the
    queue retires them.
    """
    services = workflow_document()["env"]["STAGE_SERVICES"].split()
    assert services == ["web", "app"], (
        "STAGE_SERVICES is %s; the merge-group path stages the group's exact "
        "head, so both colours should be back" % services
    )


def test_merge_group_trigger_fires_only_on_checks_requested():
    triggers = workflow_document()["on"]
    assert triggers["merge_group"]["types"] == ["checks_requested"], (
        "checks_requested is the only type GitHub sends for a queued group; "
        "there is no group-destroyed event for this workflow to subscribe to"
    )


def test_the_group_concurrency_keys_use_the_head_sha():
    concurrency = workflow_document()["concurrency"]
    group = concurrency["group"]
    assert "github.event_name == 'merge_group'" in group
    assert "github.event.merge_group.head_sha" in group
    assert "inputs.group_head_sha" not in group
    assert "prewarm-staging-cutover-mg-dispatch-{0}" in group
    stage_concurrency = group_workflow_document()["concurrency"]
    assert "inputs.group_head_sha" in stage_concurrency["group"]
    assert "prewarm-staging-cutover-mg-stage-{0}" in stage_concurrency["group"]
    assert stage_concurrency["cancel-in-progress"] == "true"
    # PR events must keep their own per-PR, per-action key untouched.
    assert "github.event.pull_request.number" in group
    assert "github.event.action == 'closed' && 'descale' || 'stage'" in group
    cancel = concurrency["cancel-in-progress"]
    assert "github.event_name == 'merge_group'" in cancel, (
        "the newest queued group's stage must cancel an older one in flight"
    )


def test_the_group_job_runs_only_for_main_dispatch_with_a_head():
    condition = group_workflow_document()["jobs"]["stage-group"]["if"]
    assert condition == "github.event_name == 'workflow_dispatch' && inputs.group_head_sha != '' && github.ref == 'refs/heads/main'"
    assert group_workflow_document()["jobs"]["stage-group"]["needs"] == "guard-ref"


GROUP_ELIGIBILITY_ENV = {
    "HEAD_SHA": "a" * 40,
}


@needs_shell
@pytest.mark.parametrize("queued", [True, False])
def test_the_group_eligibility_requires_a_live_queue_entry(tmp_path, queued):
    body = step_body("stage-group", "Validate and record the live merge-group")
    (tmp_path / "bin").mkdir()
    entries = [
        {"position": 1, "headCommit": {"oid": "c" * 40},
         "baseCommit": {"oid": "d" * 40}, "pullRequest": {"number": 41}},
        {"position": 2, "headCommit": {"oid": "a" * 40 if queued else "e" * 40},
         "baseCommit": {"oid": "b" * 40}, "pullRequest": {"number": 42}},
        {"position": 3, "headCommit": {"oid": "f" * 40},
         "baseCommit": {"oid": "b" * 40}, "pullRequest": {"number": 43}},
    ]
    pages = [
        {"data": {"repository": {"mergeQueue": {"entries": {"nodes": nodes}}}}}
        for nodes in (entries[:1], entries[1:])
    ]
    (tmp_path / "queue-fixture.json").write_text(
        "\n".join(json.dumps(page) for page in pages), encoding="utf-8"
    )
    gh = tmp_path / "bin" / "gh"
    gh.write_text("#!/bin/sh\ncat queue-fixture.json\n", encoding="utf-8", newline="\n")
    gh.chmod(0o755)
    result = run_step(body, tmp_path, GROUP_ELIGIBILITY_ENV)
    assert result["superseded"] == ("false" if queued else "true")
    if not queued:
        assert "eligible" not in result
        assert "base_sha" not in result
        return
    assert result["eligible"] == "true"
    assert result["reason"] == "merge group"
    assert result["head_sha"] == "a" * 40
    assert result["base_sha"] == "b" * 40
    assert result["sha12"] == "a" * 12
    assert json.loads(result["members"]) == [41, 42]


def test_the_group_dispatcher_has_only_two_permissions_and_no_secrets():
    job = workflow_document()["jobs"]["dispatch-group"]
    assert job["if"] == "github.event_name == 'merge_group'"
    assert job["permissions"] == {"actions": "write", "contents": "read"}
    assert len(job["steps"]) == 1
    assert "secrets." not in str(job)
    assert "uses" not in job["steps"][0]
    body = job["steps"][0]["run"]
    assert "gh workflow run prewarm-staging-group.yml" in body
    assert '--ref main -f "group_head_sha=$HEAD_SHA"' in body
    assert "PR_NUMBER" not in str(job)
    assert "::warning::" in body


def test_the_dispatch_input_and_main_ref_guard():
    document = group_workflow_document()
    field = document["on"]["workflow_dispatch"]["inputs"]["group_head_sha"]
    assert field["type"] == "string" and field["default"] == ""
    guard = document["jobs"]["guard-ref"]
    assert guard["if"] == "github.event_name == 'workflow_dispatch'"
    body = step_body("guard-ref", "Require the main workflow ref")
    assert '"$GITHUB_REF" != "refs/heads/main"' in body
    assert "exit 1" in body


def test_the_group_workflow_has_only_the_dispatch_trigger():
    document = group_workflow_document()
    assert set(document["on"]) == {"workflow_dispatch"}
    assert set(document["on"]["workflow_dispatch"]["inputs"]) == {"group_head_sha"}
    assert document["permissions"] == workflow_document()["permissions"]
    for key, value in document["env"].items():
        assert workflow_document()["env"][key] == value
    assert "stage-group" not in workflow_document()["jobs"]
    assert "group_head_sha" not in str(workflow_document()["on"]["workflow_dispatch"])


def test_live_queue_validation_and_superseded_steps_are_pinned():
    body = step_body("stage-group", "Validate and record the live merge-group")
    for required in ('gh api graphql --paginate', 'mergeQueue(branch: "main")',
                     'entries(first: 50, after: $endCursor)',
                     'pageInfo { hasNextPage endCursor }',
                     'headCommit { oid } baseCommit { oid } pullRequest { number }',
                     'select(.headCommit.oid == $head)', 'superseded=true',
                     '^[0-9a-fA-F]{40}$'):
        assert required in body
    assert "exit 0" in body
    job = group_workflow_document()["jobs"]["stage-group"]
    assert "github.event.merge_group" not in str(job)
    for step in job["steps"]:
        if step.get("uses", "").startswith("actions/checkout") or step.get("id") in {"parentage", "receipt"}:
            assert "steps.group.outputs.eligible == 'true'" in step["if"]
    parentage = next(s for s in job["steps"] if s.get("id") == "parentage")
    assert parentage["env"]["BASE_SHA"] == "${{ steps.group.outputs.base_sha }}"


def test_the_group_checkout_targets_the_exact_head_sha():
    steps = group_workflow_document()["jobs"]["stage-group"]["steps"]
    checkout = next(s for s in steps if s.get("uses", "").startswith("actions/checkout"))
    assert checkout["with"]["ref"] == "${{ steps.group.outputs.head_sha }}"
    assert checkout["with"]["fetch-depth"] == "2"
    assert checkout["with"]["persist-credentials"] == "false"


def _repo_with_parent(tmp_path: Path, base_matches: bool) -> tuple[Path, str]:
    """A two-commit repo; returns (repo, the base sha to assert against)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    run = lambda *argv: subprocess.run(argv, cwd=repo, check=True, capture_output=True)
    run("git", "init", "-q")
    run("git", "config", "user.email", "t@example.invalid")
    run("git", "config", "user.name", "t")
    (repo / "a.txt").write_text("base\n", encoding="utf-8")
    run("git", "add", "-A")
    run("git", "commit", "-qm", "base")
    base_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    (repo / "a.txt").write_text("head\n", encoding="utf-8")
    run("git", "add", "-A")
    run("git", "commit", "-qm", "head")
    asserted_base = base_sha if base_matches else "f" * 40
    return repo, asserted_base


@needs_shell
@pytest.mark.parametrize(
    "base_matches,expected_ok",
    [(True, "true"), (False, "false")],
)
def test_the_group_first_parent_must_equal_its_base(tmp_path, base_matches, expected_ok):
    repo, asserted_base = _repo_with_parent(tmp_path, base_matches)
    (repo / "bin").mkdir()
    result = run_step(
        step_body("stage-group", "Require the group head's first parent"),
        repo,
        {"BASE_SHA": asserted_base},
    )
    assert result["ok"] == expected_ok


def test_the_migration_surface_refusal_is_identical_on_the_group_path():
    pr_body = step_body("stage", "Refuse a candidate that touches")
    group_body = step_body("stage-group", "Refuse a candidate that touches")
    assert group_body == pr_body, (
        "the group path must run the exact same fail-closed migration-surface "
        "check and tag derivation as the PR path, over the group head instead "
        "of a merge preview"
    )


def test_the_supply_set_step_is_identical_on_the_group_path():
    pr_body = step_body("stage", "Wait for the speculative supply set")
    group_body = step_body("stage-group", "Wait for the speculative supply set")
    assert group_body == pr_body, (
        "the supply-set artifact name (spec-v3-supply-set-<tree>) and the "
        "poll bounds must match the PR path exactly"
    )


def test_the_dispatch_step_is_identical_on_the_group_path():
    pr_body = step_body("stage", "Dispatch the prewarm")
    group_body = step_body("stage-group", "Dispatch the prewarm")
    assert group_body == pr_body


def test_the_group_receipt_carries_a_group_object_not_a_pr_number():
    body = step_body("stage-group", "Emit the relay receipt")
    assert "group: {head_sha: $head_sha, base_sha: $base_sha, members: $members}" in body
    assert "pr: $pr" not in body
    assert '--arg schema "leaf.staging-prewarm-relay.v1"' in body


def _readiness_gh(workdir: Path, listing: dict, run_record: dict | None, zip_bytes: bytes | None) -> None:
    """A `gh` that answers the readiness step's three API reads."""
    binary = workdir / "bin"
    binary.mkdir(exist_ok=True)
    (workdir / "listing.json").write_text(json.dumps(listing), encoding="utf-8")
    if run_record is not None:
        (workdir / "run.json").write_text(json.dumps(run_record), encoding="utf-8")
    if zip_bytes is not None:
        (workdir / "artifact.zip").write_bytes(zip_bytes)
    script = binary / "gh"
    script.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            for arg in "$@"; do
              case "$arg" in
                *"/actions/artifacts/"*"/zip") cat "$(dirname "$0")/../artifact.zip"; exit 0 ;;
                *"/actions/artifacts?name="*) cat "$(dirname "$0")/../listing.json"; exit 0 ;;
                *"/actions/runs/"*) cat "$(dirname "$0")/../run.json"; exit 0 ;;
              esac
            done
            echo "{}"
            """
        ),
        encoding="utf-8",
        newline="\n",
    )
    script.chmod(0o755)


def _readiness_record(tree: str, source_sha: str, *, image_tag: str, run_id: int, run_attempt: int, repository_id: int) -> dict:
    return {
        "schema": "leaf.speculative-tag-readiness.v1",
        "source_sha": source_sha,
        "source_tree": tree,
        "image_tag": image_tag,
        "digests": {
            "app": "sha256:" + "a" * 64,
            "broker": "sha256:" + "b" * 64,
            "canonical_worker": "sha256:" + "c" * 64,
            "harness": "sha256:" + "d" * 64,
            "web": "sha256:" + "e" * 64,
        },
        "producer_workflow_path": ".github/workflows/build-platform-images.yml",
        "producer_run_id": run_id,
        "producer_run_attempt": run_attempt,
        "repository_id": repository_id,
    }


def _readiness_zip(tmp_path: Path, record: dict) -> tuple[bytes, str]:
    """Zip the record the same way upload-artifact would, return (bytes, its sha256:<hex>)."""
    zip_path = tmp_path / "fixture-source.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("spec-tag-readiness.json", json.dumps(record))
    data = zip_path.read_bytes()
    digest = "sha256:%s" % hashlib.sha256(data).hexdigest()
    return data, digest


READINESS_TREE = "1" * 40
READINESS_SOURCE_SHA = "2" * 40
READINESS_IMAGE_TAG = "spec-%s-%s" % (READINESS_TREE, READINESS_SOURCE_SHA[:12])
READINESS_REPO_ID = 555
READINESS_RUN_ID = 99
READINESS_RUN_ATTEMPT = 2

READINESS_ENV = {
    "GH_TOKEN": "unused",
    "TREE": READINESS_TREE,
    "SOURCE_SHA": READINESS_SOURCE_SHA,
    "IMAGE_TAG": READINESS_IMAGE_TAG,
    "REPO_ID": str(READINESS_REPO_ID),
    "SUPPLY_SET_POLLS": "1",
    "SUPPLY_SET_INTERVAL": "0",
}

READINESS_LISTING_TEMPLATE = {
    "id": 1,
    "expired": False,
    "workflow_run": {"id": READINESS_RUN_ID, "head_repository_id": READINESS_REPO_ID},
}

READINESS_RUN_RECORD = {
    "path": ".github/workflows/build-platform-images.yml@refs/heads/main",
    "event": "workflow_dispatch",
    "head_branch": "main",
    "run_attempt": READINESS_RUN_ATTEMPT,
}


@needs_shell
def test_the_readiness_step_is_false_on_an_empty_listing(tmp_path):
    _readiness_gh(tmp_path, {"artifacts": []}, None, None)
    result = run_step(
        step_body("stage-group", "Wait for the exact speculative tag-readiness"),
        tmp_path,
        READINESS_ENV,
    )
    assert result["ready"] == "false"


@needs_shell
@needs_unzip
def test_the_readiness_step_is_true_for_the_exact_matching_record(tmp_path):
    """Proves adoption WITHOUT consulting the older v3 supply-set artifact:
    this test runs the readiness step body alone, over a fixture bound to
    this run's own tree, source and tag."""
    record = _readiness_record(
        READINESS_TREE, READINESS_SOURCE_SHA, image_tag=READINESS_IMAGE_TAG,
        run_id=READINESS_RUN_ID, run_attempt=READINESS_RUN_ATTEMPT,
        repository_id=READINESS_REPO_ID,
    )
    zip_bytes, digest = _readiness_zip(tmp_path, record)
    listing = {"artifacts": [{**READINESS_LISTING_TEMPLATE, "digest": digest}]}
    _readiness_gh(tmp_path, listing, READINESS_RUN_RECORD, zip_bytes)
    result = run_step(
        step_body("stage-group", "Wait for the exact speculative tag-readiness"),
        tmp_path,
        READINESS_ENV,
    )
    assert result["ready"] == "true"


@needs_shell
@needs_unzip
def test_the_readiness_step_rejects_an_earlier_preview_cohorts_tag_for_the_same_tree(tmp_path):
    """Same tree, a real matching digest, but the record names an EARLIER
    preview cohort's own source and tag -- exactly the artifact that used to
    unblock the relay early. Content validation, not just presence, must
    refuse it."""
    earlier_source_sha = "3" * 40
    earlier_tag = "spec-%s-%s" % (READINESS_TREE, earlier_source_sha[:12])
    record = _readiness_record(
        READINESS_TREE, earlier_source_sha, image_tag=earlier_tag,
        run_id=READINESS_RUN_ID, run_attempt=READINESS_RUN_ATTEMPT,
        repository_id=READINESS_REPO_ID,
    )
    zip_bytes, digest = _readiness_zip(tmp_path, record)
    listing = {"artifacts": [{**READINESS_LISTING_TEMPLATE, "digest": digest}]}
    _readiness_gh(tmp_path, listing, READINESS_RUN_RECORD, zip_bytes)
    result = run_step(
        step_body("stage-group", "Wait for the exact speculative tag-readiness"),
        tmp_path,
        READINESS_ENV,
    )
    assert result["ready"] == "false"


@needs_shell
@needs_unzip
def test_the_relay_accepts_the_producers_own_materialized_readiness_record(tmp_path):
    """A hand-built fixture only proves the relay's OWN opinion of the
    schema. Run build-platform-images.yml's actual materialize step body,
    with faked digest/identity inputs and the v3 dedup guard never
    consulted, and feed the relay the producer's REAL byte output -- the
    only proof the two sides still agree on the wire format."""
    producer_source_sha = "6" * 40
    producer_tree = "7" * 40
    producer_tag = "spec-%s-%s" % (producer_tree, producer_source_sha[:12])
    producer_run_id = 4242
    producer_run_attempt = 3
    producer_repo_id = 909

    materialize_step = _step_by_id(
        build_workflow_document()["jobs"]["speculate-manifest"]["steps"], "readiness"
    )
    bound_script = _bind_gh_expressions(materialize_step["run"], {
        "needs.prepare.outputs.source_sha": producer_source_sha,
        "steps.digests.outputs.app": "sha256:" + "a" * 64,
        "steps.digests.outputs.broker": "sha256:" + "b" * 64,
        "steps.digests.outputs.canonical_worker": "sha256:" + "c" * 64,
        "steps.digests.outputs.harness": "sha256:" + "d" * 64,
        "steps.digests.outputs.web": "sha256:" + "e" * 64,
        "github.repository_id": str(producer_repo_id),
    })
    run_step(bound_script, tmp_path, {
        "SOURCE_TREE": producer_tree,
        "SPEC_TAG": producer_tag,
        "GITHUB_RUN_ID": str(producer_run_id),
        "GITHUB_RUN_ATTEMPT": str(producer_run_attempt),
        "RUNNER_TEMP": ".",
    })
    raw_record = (tmp_path / "spec-tag-readiness.json").read_bytes()
    record = json.loads(raw_record)
    assert record["schema"] == "leaf.speculative-tag-readiness.v1"
    assert record["source_sha"] == producer_source_sha
    assert record["image_tag"] == producer_tag
    assert record["producer_workflow_path"] == ".github/workflows/build-platform-images.yml"

    zip_path = tmp_path / "fixture-source.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("spec-tag-readiness.json", raw_record)
    digest = "sha256:%s" % hashlib.sha256(zip_path.read_bytes()).hexdigest()

    listing = {"artifacts": [{
        "id": 1, "expired": False,
        "workflow_run": {"id": producer_run_id, "head_repository_id": producer_repo_id},
        "digest": digest,
    }]}
    run_record = {
        "path": ".github/workflows/build-platform-images.yml@refs/heads/main",
        "event": "workflow_dispatch",
        "head_branch": "main",
        "run_attempt": producer_run_attempt,
    }
    _readiness_gh(tmp_path, listing, run_record, zip_path.read_bytes())
    result = run_step(
        step_body("stage-group", "Wait for the exact speculative tag-readiness"),
        tmp_path,
        {
            "GH_TOKEN": "unused",
            "TREE": producer_tree,
            "SOURCE_SHA": producer_source_sha,
            "IMAGE_TAG": producer_tag,
            "REPO_ID": str(producer_repo_id),
            "SUPPLY_SET_POLLS": "1",
            "SUPPLY_SET_INTERVAL": "0",
        },
    )
    assert result["ready"] == "true"


def test_the_dispatch_step_now_gates_on_readiness_not_supply_presence():
    dispatch = next(
        s for s in group_workflow_document()["jobs"]["stage-group"]["steps"]
        if s.get("id") == "dispatch"
    )
    assert dispatch["if"] == "steps.readiness.outputs.ready == 'true'"


def test_the_receipt_carries_an_honest_tag_ready_boolean():
    body = step_body("stage-group", "Emit the relay receipt")
    assert 'tag_ready: ($tag_ready == "true")' in body
    assert '--arg tag_ready "${TAG_READY:-false}"' in body
    assert '--arg schema "leaf.staging-prewarm-relay.v1"' in body


def test_the_group_receipt_artifact_is_named_by_short_sha():
    steps = group_workflow_document()["jobs"]["stage-group"]["steps"]
    upload = next(s for s in steps if s.get("uses", "").startswith("actions/upload-artifact"))
    assert upload["with"]["name"] == "prewarm-relay-receipt-mg-${{ steps.group.outputs.sha12 }}"
    assert upload["with"]["retention-days"] == "30"


def test_the_descale_job_is_explicit_pr_only_with_the_reaper_comment():
    document = workflow_document()
    condition = document["jobs"]["descale"]["if"]
    assert "github.event_name == 'pull_request_target'" in condition, (
        "the descale job must be explicit that it is PR-only"
    )
    text = workflow_text()
    assert 'GitHub Actions delivers no "merge group destroyed" event' in text
    assert "left entirely to the terraform TTL reaper" in text


def test_the_relay_never_deploys_normally_or_flips():
    text = workflow_text()
    assert "deploy_mode=normal" not in text
    for forbidden in ("modify-rule", "aws ecs", "desired-count"):
        assert forbidden not in text, "the relay holds no AWS authority (%s)" % forbidden


def test_only_an_unmerged_close_descales():
    condition = workflow_document()["jobs"]["descale"]["if"]
    assert "github.event.pull_request.merged == false" in condition
    body = step_body("descale", "Ask the reaper")
    assert "witness_tag=$WITNESS" in body
    assert "mode=reap" in body
    # The TTL sweep is the backstop, so a failed targeted descale is a warning.
    assert "::warning::Targeted descale dispatch failed" in body


def _uncommented_workflow() -> str:
    """The workflow with comment lines dropped.

    A comment is allowed to DISCUSS a trap -- the note explaining why `--arg
    label` breaks jq 1.6 must not itself trip the scan that enforces it.
    """
    return "".join(
        line + chr(10)
        for line in workflow_text().splitlines()
        if not line.lstrip().startswith("#")
    )


JQ_KEYWORDS = {
    "def", "as", "label", "import", "include", "if", "then", "else", "elif",
    "end", "and", "or", "reduce", "foreach", "try", "catch", "__loc__",
}


def test_no_jq_binding_shadows_a_jq_keyword():
    """`--arg label` is a syntax error on jq 1.6, and fine on 1.7+.

    That difference is invisible on a modern workstation and fatal on the CI
    image, which is exactly how it shipped once: `any(.labels[]?.name; . ==
    $label)` passed locally on jq 1.8 and failed the gate with "unexpected
    label". Scan for the class, not that one instance.
    """
    import re

    offenders = [
        name
        for name in re.findall(r"--arg(?:json)?[ \t]+([A-Za-z_][A-Za-z0-9_]*)", _uncommented_workflow())
        if name in JQ_KEYWORDS
    ]
    assert offenders == [], (
        "these jq bindings shadow a jq keyword and break on jq 1.6: %s" % offenders
    )


def test_no_jq_filter_suffixes_a_field_onto_an_optional_iterator():
    """`.a[]?.b` is rejected by older jq; `.a[]? | .b` is portable."""
    import re

    assert not re.search(r"\[\]\?\.", _uncommented_workflow()), (
        "use `.x[]? | .y` rather than `.x[]?.y`; the suffixed form does not "
        "parse on the jq the CI image carries"
    )


def test_the_cross_repo_token_is_scoped_to_the_dispatch_steps():
    document = workflow_document()
    for job_name, job in document["jobs"].items():
        for step in job["steps"]:
            env = step.get("env", {})
            if "TERRAFORM_REPO_TOKEN" in str(env.get("GH_TOKEN", "")):
                assert "gh workflow run" in step.get("run", ""), (
                    "%s: the terraform token is only for dispatching, not for reads" % job_name
                )


def test_run_url_extraction_cannot_abort_the_service_loop():
    """Under pipefail, a no-match grep makes the run-URL pipeline fail.

    Errexit then aborts the step before the mark scan and before the next service.
    Guard the extraction so an empty run URL reaches the fallback.
    """
    text = workflow_text()
    assert "RUN_ID=$(printf '%s' \"$DISPATCH_OUTPUT\" | grep -oE 'actions/runs/[0-9]+' | head -1 | cut -d/ -f3 || true)" in text
    assert "RUN_ID=$(printf '%s' \"$DISPATCH_OUTPUT\" | grep -oE 'actions/runs/[0-9]+' | head -1 | cut -d/ -f3)" not in text
