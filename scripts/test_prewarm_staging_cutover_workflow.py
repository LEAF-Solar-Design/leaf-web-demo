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

import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import textwrap

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "prewarm-staging-cutover.yml"

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


def workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def workflow_document() -> dict:
    # BaseLoader keeps the literal `on` key instead of YAML 1.1's boolean True.
    return yaml.load(workflow_text(), Loader=yaml.BaseLoader)


def step_body(job: str, name_fragment: str) -> str:
    for step in workflow_document()["jobs"][job]["steps"]:
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
    assert concurrency["cancel-in-progress"] == "${{ github.event.action != 'closed' }}"


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


def test_web_and_app_are_staged_now_that_the_app_deploy_job_is_ephemeral():
    """A cross-repo coupling this repo cannot check mechanically, so it is pinned.

    `app` was withheld from 2026-08-17 to 2026-09-05: deploy-leaf-platform-staging.yml
    (terraform repo) then ran its deploy job on the single self-hosted runner, and
    a run holds the shared staging mutex from the moment it ENTERS the group, so an
    app prewarm queued behind that busy runner blocked every staging deploy (two
    relayed prewarms sat 25 and 44 minutes behind an iOS capture job).

    Since 2026-08-24 that job runs on an ephemeral CodeBuild-backed runner, one per
    run (codebuild-leaf-gha-runner-platform-deploy-<run_id>-<attempt>), and on
    2026-09-05 the deploy job waited 0.4 min for its runner on nine consecutive app
    relays. The condition the old pin named is met, so both colour-surfaced
    services are staged, web first. Removing `app` again, reordering, or adding a
    service the deploy workflow refuses under prewarm (`app` and `web` are the only
    two it admits) is a deliberate edit that has to come back through this test.
    """
    services = workflow_document()["env"]["STAGE_SERVICES"].split()
    assert services == ["web", "app"], (
        "STAGE_SERVICES is %s; the two colour-surfaced services are staged in "
        "dispatch order, web then app" % services
    )


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
