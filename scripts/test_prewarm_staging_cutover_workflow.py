"""Executable contract for the queue-mode prewarm staging relay.

PR events only report retirement. Mutation fixtures prove that a dispatch,
checkout or token cannot return unnoticed. The group relay retains executable
migration, queue-membership and tag-readiness checks, and unmerged PR closes
retain targeted descaling of old stages.
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


def _check_ecr_role_environment(document: dict) -> None:
    for job_name, job in document["jobs"].items():
        if "secrets.AWS_ECR_PUSH_ROLE" in json.dumps(job.get("steps", [])):
            assert job.get("environment") == "ecr-release", (
                "%s: AWS_ECR_PUSH_ROLE requires environment: ecr-release" % job_name
            )


def test_ecr_role_jobs_declare_the_trusted_environment():
    for document in (workflow_document(), group_workflow_document()):
        _check_ecr_role_environment(document)


def test_ecr_role_environment_pin_rejects_a_missing_job_environment():
    checked = []
    for document in (workflow_document(), group_workflow_document()):
        _check_ecr_role_environment(document)
        for job_name, job in document["jobs"].items():
            if "secrets.AWS_ECR_PUSH_ROLE" in json.dumps(job.get("steps", [])):
                mutated = json.loads(json.dumps(document))
                mutated["jobs"][job_name].pop("environment")
                with pytest.raises(AssertionError, match=re.escape(job_name)):
                    _check_ecr_role_environment(mutated)
                checked.append(job_name)
    assert "stage-group" in checked


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
        "GITHUB_RUN_ATTEMPT": "1",
        "MQ_TRANSPORT_BUCKET": "leaf-mq-transport-807034087062-us-east-1",
        "MQ_TRANSPORT_PREFIX": "mq/leaf-web-demo/",
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


def _check_pr_notice(document: dict) -> None:
    job = document["jobs"]["stage"]
    assert set(job) == {"name", "if", "runs-on", "permissions", "timeout-minutes", "steps"}
    assert job["if"] == (
        "(github.event_name == 'pull_request_review' || "
        "github.event_name == 'pull_request_target') && github.event.action != 'closed'"
    )
    assert job["permissions"] == {}
    assert "secrets." not in str(job)
    assert len(job["steps"]) == 1
    step = job["steps"][0]
    assert set(step) == {"name", "run"}
    assert step["run"] == (
        'echo "::notice::PR-mode staging retired 2026-09-05: '
        'the merge queue builds the group commit; see merge-queue.yml"\n'
        'exit 0\n'
    )


@pytest.mark.parametrize(
    "mutation",
    ["dispatch", "secret", "checkout", "job-env", "step-env",
     "reusable-job", "write-permission", "closed-event"],
)
def test_pr_notice_rejects_staging_regressions(mutation):
    document = workflow_document()
    _check_pr_notice(document)
    job = document["jobs"]["stage"]
    if mutation == "dispatch":
        job["steps"][0]["run"] = (
            'gh workflow run "$DEPLOY_WORKFLOW" --ref main\n'
            + job["steps"][0]["run"]
        )
    elif mutation == "secret":
        job["steps"][0]["env"] = {"GH_TOKEN": "${{ secrets.TERRAFORM_REPO_TOKEN }}"}
    elif mutation == "checkout":
        job["steps"].insert(0, {"uses": "actions/checkout@v4"})
    elif mutation == "job-env":
        job["env"] = {"GH_TOKEN": "${{ github.token }}"}
    elif mutation == "step-env":
        job["steps"][0]["env"] = {"GH_TOKEN": "${{ github.token }}"}
    elif mutation == "reusable-job":
        job["uses"] = "./.github/workflows/prewarm-staging-group.yml"
    elif mutation == "write-permission":
        job["permissions"] = {"actions": "write"}
    else:
        job["if"] = "github.event_name == 'pull_request_target'"
    with pytest.raises(AssertionError):
        _check_pr_notice(document)


def test_a_closed_pull_request_is_never_staged():
    _check_pr_notice(workflow_document())
    assert "github.event.action != 'closed'" in workflow_document()["jobs"]["stage"]["if"]


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
    result = run_step(step_body("stage-group", "Refuse a candidate that touches"), repo, CANDIDATE_ENV)
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
    result = run_step(step_body("stage-group", "Refuse a candidate that touches"), repo, CANDIDATE_ENV)
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
    body = step_body("stage-group", "Refuse a candidate that touches")
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


def test_pr_events_only_report_retirement():
    _check_pr_notice(workflow_document())


def test_the_dispatch_stages_both_colours_on_the_prewarm_rail():
    body = step_body("stage-group", "Dispatch the prewarm")
    assert "timeout 60 aws codebuild start-build" in body
    assert "--project-name leaf-deploy-terraform-staging" in body
    assert "for SERVICE in $STAGE_SERVICES" in body
    assert "::warning::Prewarm dispatch for $SERVICE failed" in body
    assert "--source-version" not in body
    assert set(re.findall(r"name=([A-Z_]+),value=", body)) == {
        "STEP", "LEAF_DEPLOY_APPROVED_BY", "LEAF_DEPLOY_SERVICE",
        "LEAF_DEPLOY_IMAGE_TAG", "LEAF_DEPLOY_EXPECTED_TD", "LEAF_DEPLOY_REQUEST_ID",
    }
    job = str(group_workflow_document()["jobs"]["stage-group"])
    assert "gh workflow run" not in job
    assert "INFRA_REPO" not in job


def test_web_and_app_are_staged_again_because_the_merge_group_makes_the_stage_fresh():
    """Group freshness does not supply app's missing staging migration.

    Keep web alone until the native deploy runs migrations, then restore app
    in that same change. The historical row now pins this migration contract.
    """
    services = group_workflow_document()["env"]["STAGE_SERVICES"].split()
    assert services == ["web"]
    source = GROUP_WORKFLOW.read_text(encoding="utf-8")
    preceding = source.split('  STAGE_SERVICES: "web"', 1)[0].splitlines()
    comment_lines = []
    for line in reversed(preceding):
        if not line.lstrip().startswith("#"):
            break
        comment_lines.append(line.strip())
    comment = " ".join(reversed(comment_lines))
    assert "0058_campaign_host_enrollment" in comment
    assert "campaign_host_enrollments_machine_unique" in comment
    assert "platform_link.validate_postgres_startup" in comment
    assert "leaf-deploy-terraform-staging" in comment
    assert 'Restore "web app" in the change that lands' in comment
    assert "the native deploy's migrate step" in comment


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
        if key == "STAGE_SERVICES":
            # Only the group relay excludes app until native migrations land.
            assert value == "web"
            assert workflow_document()["env"][key] == "web app"
        else:
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


def test_the_group_candidate_retains_fail_closed_migration_checks():
    body = step_body("stage-group", "Refuse a candidate that touches")
    assert "git diff --no-renames --name-only 'HEAD^1' HEAD" in body
    assert 'echo "stageable=false"' in body
    assert 'echo "migration_refusal=true"' in body


def test_the_group_supply_set_step_retains_tree_identity_and_poll_bounds():
    body = step_body("stage-group", "Wait for the speculative supply set")
    assert "spec-v3-supply-set-$TREE" in body
    assert "SUPPLY_SET_POLLS" in body
    assert "SUPPLY_SET_INTERVAL" in body


def test_the_group_dispatch_uses_only_the_oidc_role():
    job = group_workflow_document()["jobs"]["stage-group"]
    assert job["permissions"] == {
        "contents": "read", "actions": "read", "pull-requests": "read", "id-token": "write",
    }
    credentials = next(step for step in job["steps"] if
                       step.get("uses", "").startswith("aws-actions/configure-aws-credentials"))
    assert credentials["uses"] == "aws-actions/configure-aws-credentials@v6.1.0"
    assert credentials["with"] == {
        "role-to-assume": "${{ secrets.AWS_ECR_PUSH_ROLE }}", "aws-region": "us-east-1",
    }
    assert credentials["if"] == "steps.group.outputs.eligible == 'true'"
    steps = job["steps"]
    assert job["needs"] == "guard-ref"
    assert steps.index(next(step for step in steps if step.get("id") == "group")) < steps.index(credentials)
    assert steps.index(credentials) < steps.index(next(step for step in steps if step.get("id") == "supply"))
    assert "refs/heads/main" in group_workflow_document()["jobs"]["guard-ref"]["steps"][0]["run"]
    assert set(re.findall(r"secrets\.([A-Z_]+)", str(job))) == {"AWS_ECR_PUSH_ROLE"}
    _check_pr_notice(workflow_document())


def test_the_group_receipt_carries_a_group_object_not_a_pr_number():
    body = step_body("stage-group", "Emit the relay receipt")
    assert "group: {head_sha: $head_sha, base_sha: $base_sha, members: $members}" in body
    assert "pr: $pr" not in body
    assert '--arg schema "leaf.staging-prewarm-relay.v2"' in body


def _readiness_gh(workdir: Path, listing: dict, run_record: dict | None, zip_bytes: bytes | None) -> None:
    """Adapt retained fixtures to S3 metadata and a GitHub producer run record."""
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


    # Preserve the behavioral fixtures while replacing their wire transport.
    import io
    objects = {}
    if zip_bytes is not None:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
            body = archive.read("spec-tag-readiness.json")
        record = json.loads(body)
        for entry in listing.get("artifacts", []):
            run_id = entry["workflow_run"]["id"]
            attempt = run_record["run_attempt"]
            key = "mq/leaf-web-demo/readiness/%s/%s/%s-%s.json" % (
                record["source_tree"], READINESS_SOURCE_SHA if record["source_sha"] == "3" * 40 else record["source_sha"],
                run_id, attempt,
            )
            objects[key] = _s3_object(
                body, run_id, attempt, entry["workflow_run"]["head_repository_id"],
                READINESS_SOURCE_SHA if record["source_sha"] == "3" * 40 else record["source_sha"],
            )
        provider = {**run_record, "repository": {"id": record["repository_id"]},
                    "head_repository": {"id": record["repository_id"]}}
        (workdir / "run.json").write_text(json.dumps(provider), encoding="utf-8")
    _s3_transport_fixture(workdir, objects)


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
    assert '--arg schema "leaf.staging-prewarm-relay.v2"' in body


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
        for line in (workflow_text() + "\n" + GROUP_WORKFLOW.read_text(encoding="utf-8")).splitlines()
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
    for job_name, job in workflow_jobs().items():
        for step in job["steps"]:
            env = step.get("env", {})
            if "TERRAFORM_REPO_TOKEN" in str(env.get("GH_TOKEN", "")):
                assert "gh workflow run" in step.get("run", ""), (
                    "%s: the terraform token is only for dispatching, not for reads" % job_name
                )


def test_build_id_extraction_refuses_unresolved_identity():
    body = step_body("stage-group", "Dispatch the prewarm")
    assert '^leaf-deploy-terraform-staging:[0-9a-f-]{36}$' in body
    assert 'build_id: null, disposition: "unresolved"' in body
    assert 'build_id: $build_id, disposition: "dispatched"' in body
    assert 'producer: "codebuild"' in step_body("stage-group", "Emit the relay receipt")


def test_pr_stage_is_notice_only():
    _check_pr_notice(workflow_document())


@needs_shell
@pytest.mark.parametrize("response,disposition", [
    ({"build": {"id": "leaf-deploy-terraform-staging:" + "a" * 36}}, "dispatched"),
    ({"build": {}}, "unresolved"),
    ({"build": {"id": "other-project:" + "a" * 36}}, "unresolved"),
    ("malformed JSON", "unresolved"),
    ("refused", "dispatch-failed"),
])
@pytest.mark.parametrize("service,expected_present", [("web", True), ("app", False)])
def test_codebuild_dispatch_and_relay_receipt_executed(tmp_path, response, disposition, service, expected_present):
    binary = tmp_path / "bin"
    binary.mkdir()
    (tmp_path / "response.txt").write_text(
        json.dumps(response) if isinstance(response, dict) else response, encoding="utf-8",
    )
    fake = binary / "aws"
    fake.write_text(textwrap.dedent('''\
        #!/usr/bin/env bash
        set -euo pipefail
        printf '%s\\n' "$*" >> aws-calls.txt
        if [[ "$*" == *"name=LEAF_DEPLOY_SERVICE,value=web"* ]]; then
          cat response.txt
          if [ "$(cat response.txt)" = refused ]; then exit 1; fi
        else
          echo '{"build":{"id":"leaf-deploy-terraform-staging:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"}}'
        fi
        '''), encoding="utf-8", newline="\n")
    fake.chmod(0o755)
    image_tag = "spec-" + "b" * 40 + "-" + "a" * 12
    run_step(step_body("stage-group", "Dispatch the prewarm"), tmp_path, {
        "STAGE_SERVICES": group_workflow_document()["env"]["STAGE_SERVICES"],
        "IMAGE_TAG": image_tag, "SHA12": "a" * 12, "GITHUB_RUN_ID": "77",
    })
    entries = json.loads((tmp_path / "dispatched.json").read_text())
    assert entries == [
        {"service": "web", "build_id": response["build"]["id"] if disposition == "dispatched" else None,
         "disposition": disposition},
    ]
    # App remains an executed absence expectation until native migrations land.
    assert any(entry["service"] == service for entry in entries) == expected_present
    calls = (tmp_path / "aws-calls.txt").read_text().splitlines()
    assert len(calls) == 1
    assert any("name=LEAF_DEPLOY_SERVICE,value=" + service in shlex.split(call)
               for call in calls) == expected_present
    for service, call in zip(("web",), calls):
        assert shlex.split(call) == [
            "codebuild", "start-build", "--project-name", "leaf-deploy-terraform-staging",
            "--environment-variables-override", "name=STEP,value=prewarm",
            "name=LEAF_DEPLOY_APPROVED_BY,value=merge-queue-" + "a" * 12,
            "name=LEAF_DEPLOY_SERVICE,value=" + service,
            "name=LEAF_DEPLOY_IMAGE_TAG,value=" + image_tag,
            "name=LEAF_DEPLOY_EXPECTED_TD,value=auto-live",
            "name=LEAF_DEPLOY_REQUEST_ID,value=" + "a" * 12 + "-77", "--output", "json",
        ]
    run_step(step_body("stage-group", "Emit the relay receipt"), tmp_path, {
        "HEAD_SHA": "a" * 40, "BASE_SHA": "b" * 40, "MEMBERS": "[41]",
        "ELIGIBILITY": "merge group", "IMAGE_TAG": image_tag, "PREVIEW_SHA": "a" * 40,
        "TREE": "b" * 40, "GITHUB_RUN_ID": "77", "TAG_READY": "true",
        "CONFIGURED_SERVICES": group_workflow_document()["env"]["STAGE_SERVICES"],
    })
    receipt = json.loads((tmp_path / "prewarm-relay-receipt.json").read_text())
    assert receipt["producer"] == "codebuild"
    assert receipt["dispatched"] == entries
    assert receipt["configured_services"] == ["web"]
    assert receipt["configured_services"] == sorted(set(group_workflow_document()["env"]["STAGE_SERVICES"].split()))
    assert receipt["relay_run_id"] == "77"


@needs_shell
def test_relay_receipt_configured_services_are_sorted_and_unique(tmp_path):
    step = next(s for s in group_workflow_document()["jobs"]["stage-group"]["steps"]
                if s.get("name") == "Emit the relay receipt")
    assert step["env"]["CONFIGURED_SERVICES"] == "${{ env.STAGE_SERVICES }}"
    run_step(step["run"], tmp_path, {
        "HEAD_SHA": "a" * 40, "BASE_SHA": "b" * 40, "MEMBERS": "[41]",
        "ELIGIBILITY": "merge group", "GITHUB_RUN_ID": "77",
        "STAGE_SERVICES": "web app web", "CONFIGURED_SERVICES": "web app web",
    })
    receipt = json.loads((tmp_path / "prewarm-relay-receipt.json").read_text())
    assert receipt["configured_services"] == ["app", "web"]
    assert receipt["configured_services"] == sorted(set("web app web".split()))


def _s3_object(body, run_id, attempt, repo_id, sha, workflow="build-platform-images.yml", modified="2026-09-07T00:00:00Z"):
    import base64
    if not isinstance(body, bytes):
        body = json.dumps(body).encode()
    return {
        "body": base64.b64encode(body).decode(),
        "ChecksumSHA256": base64.b64encode(hashlib.sha256(body).digest()).decode(),
        "LastModified": modified,
        "Metadata": {
            "run-id": str(run_id), "run-attempt": str(attempt),
            "repository-id": str(repo_id), "head-sha": sha, "event": "workflow_dispatch",
            "workflow-ref": "LEAF-Solar-Design/leaf-web-demo/.github/workflows/" + workflow + "@refs/heads/main",
        },
    }


def _s3_transport_fixture(workdir, objects, error=None):
    """A provider-shaped S3 stub; retain any CodeBuild stub for the unchanged rail."""
    binary = workdir / "bin"
    binary.mkdir(exist_ok=True)
    existing = binary / "aws"
    if existing.exists() and not (binary / "aws-original").exists():
        existing.rename(binary / "aws-original")
    (workdir / "s3-objects.json").write_text(json.dumps(objects), encoding="utf-8")
    (workdir / "s3-error.json").write_text(json.dumps(error), encoding="utf-8")
    existing.write_text('#!/usr/bin/env bash\nif [ "$1" != s3api ]; then exec ./bin/aws-original "$@"; fi\nexec python3 s3-provider.py "$@"\n',
                        encoding="utf-8", newline="\n")
    existing.chmod(0o755)
    (workdir / "s3-provider.py").write_text(textwrap.dedent('''\
        import base64
        import json
        import sys
        from pathlib import Path

        args = sys.argv[1:]
        with Path("s3-calls.jsonl").open("a") as stream:
            stream.write(json.dumps(args) + "\\n")
        error = json.loads(Path("s3-error.json").read_text())
        if isinstance(error, dict):
            error = error.get(args[1])
        if error:
            print(error, file=sys.stderr)
            sys.exit(254)
        objects = json.loads(Path("s3-objects.json").read_text())
        def option(name):
            return args[args.index(name) + 1]
        operation = args[1]
        if operation == "list-objects-v2":
            prefix = option("--prefix")
            contents = [{"Key": key, "LastModified": item["LastModified"]}
                        for key, item in sorted(objects.items()) if key.startswith(prefix)]
            if "--max-keys" in args:
                contents = contents[:int(option("--max-keys"))]
            listed = Path("s3-listed.json")
            keys = json.loads(listed.read_text()) if listed.exists() else []
            listed.write_text(json.dumps(keys + [item["Key"] for item in contents]))
            print(json.dumps({"Contents": contents}))
        elif operation == "put-object":
            assert option("--if-none-match") == "*"
            assert option("--checksum-algorithm") == "SHA256"
            assert "run-id=" in option("--metadata") and "run-attempt=" in option("--metadata")
            assert Path(option("--body")).is_file()
            print("{}")
        else:
            key = option("--key")
            if key not in objects:
                print("AccessDenied (403)", file=sys.stderr)
                sys.exit(254)
            assert key in json.loads(Path("s3-listed.json").read_text()), "read before listing exact key"
            item = objects[key]
            if operation == "get-object":
                assert option("--checksum-mode") == "ENABLED"
                Path(args[-1]).write_bytes(base64.b64decode(item["body"]))
            else:
                assert operation == "head-object"
            print(json.dumps({name: item[name] for name in ("Metadata", "ChecksumSHA256")}))
        '''), encoding="utf-8", newline="\n")


@needs_shell
def test_s3_readiness_absence_never_heads_an_unlisted_key_executed(tmp_path):
    _s3_transport_fixture(tmp_path, {}, {"head-object": "AccessDenied"})
    result = run_step(step_body("stage-group", "Wait for the exact speculative"), tmp_path, READINESS_ENV)
    assert result["ready"] == "false"
    calls = [json.loads(line) for line in (tmp_path / "s3-calls.jsonl").read_text().splitlines()]
    assert [call[1] for call in calls] == ["list-objects-v2"]
    assert calls[0][calls[0].index("--prefix") + 1] == (
        "mq/leaf-web-demo/readiness/" + READINESS_TREE + "/" + READINESS_SOURCE_SHA + "/"
    )


def test_s3_transport_literals_and_credentials_order():
    document = group_workflow_document()
    assert document["jobs"]["stage-group"]["env"] == {
        "MQ_TRANSPORT_BUCKET": "leaf-mq-transport-807034087062-us-east-1",
        "MQ_TRANSPORT_PREFIX": "mq/leaf-web-demo/",
    }
    steps = document["jobs"]["stage-group"]["steps"]
    credentials = next(step for step in steps if step.get("uses", "").startswith("aws-actions/configure"))
    assert credentials["if"] == "steps.group.outputs.eligible == 'true'"
    assert steps.index(next(step for step in steps if step.get("id") == "group")) < steps.index(credentials)
    assert steps.index(credentials) < steps.index(next(step for step in steps if step.get("id") == "supply"))
    assert document["jobs"]["stage-group"]["needs"] == "guard-ref"
    for fragment in ("Wait for the speculative supply", "Wait for the exact speculative"):
        assert "actions/artifacts" not in step_body("stage-group", fragment)


def test_s3_readiness_checksums_are_compared_before_acceptance():
    body = step_body("stage-group", "Wait for the exact speculative")
    assert body.count("aws s3api get-object") == 1
    assert '--key "$KEY" --checksum-mode ENABLED tag-readiness.json' in body
    assert "openssl dgst -sha256 -binary tag-readiness.json | openssl base64 -A" in body
    compare = '[ -n "$EXPECTED_CHECKSUM" ] && [ "$ACTUAL_CHECKSUM" = "$EXPECTED_CHECKSUM" ] || continue'
    assert body.index(compare) < body.index('echo "ready=true"')
    assert 'sort_by(.LastModified, .Key) | reverse' in body


def _s3_ready_fixture(tmp_path):
    record = _readiness_record(
        READINESS_TREE, READINESS_SOURCE_SHA, image_tag=READINESS_IMAGE_TAG,
        run_id=READINESS_RUN_ID, run_attempt=READINESS_RUN_ATTEMPT,
        repository_id=READINESS_REPO_ID,
    )
    zipped, digest = _readiness_zip(tmp_path, record)
    _readiness_gh(tmp_path, {"artifacts": [{**READINESS_LISTING_TEMPLATE, "digest": digest}]},
                  READINESS_RUN_RECORD, zipped)
    return json.loads((tmp_path / "s3-objects.json").read_text())


@needs_shell
def test_s3_readiness_first_valid_wins_and_tie_break_executed(tmp_path):
    for scenario in ("tie", "invalid-newest", "bad-checksum"):
        work = tmp_path / scenario
        work.mkdir()
        objects = _s3_ready_fixture(work)
        key, good = next(iter(objects.items()))
        other_key = key.rsplit("/", 1)[0] + "/98-2.json"
        other = json.loads(json.dumps(good))
        other["Metadata"]["run-id"] = "98"
        import base64
        record = json.loads(base64.b64decode(other["body"]))
        record["producer_run_id"] = 98
        other = _s3_object(record, 98, 2, READINESS_REPO_ID, READINESS_SOURCE_SHA)
        if scenario == "invalid-newest":
            other["LastModified"] = "2026-09-08T00:00:00Z"
            other["Metadata"]["workflow-ref"] = "foreign"
        if scenario == "bad-checksum":
            other["LastModified"] = "2026-09-08T00:00:00Z"
            other["ChecksumSHA256"] = "wrong"
        # Reverse insertion order makes a listing-order tie breaker fail.
        _s3_transport_fixture(work, {key: good, other_key: other})
        result = run_step(step_body("stage-group", "Wait for the exact speculative"), work, READINESS_ENV)
        assert result["ready"] == "true"
        calls = [json.loads(line) for line in (work / "s3-calls.jsonl").read_text().splitlines()]
        listings = [call for call in calls if call[1] == "list-objects-v2"]
        assert len(listings) == 1
        assert listings[0][listings[0].index("--prefix") + 1] == key.rsplit("/", 1)[0] + "/"
        heads = [call[call.index("--key") + 1] for call in calls if call[1] == "head-object"]
        assert heads == ([key] if scenario == "tie" else [other_key, key])
        assert "first-valid-wins" in step_body("stage-group", "Wait for the exact speculative")


@needs_shell
def test_s3_readiness_metadata_and_run_attempt_executed(tmp_path):
    for scenario in ("metadata-run", "metadata-attempt", "run-attempt", "repository", "checksum"):
        work = tmp_path / scenario
        work.mkdir()
        objects = _s3_ready_fixture(work)
        item = next(iter(objects.values()))
        if scenario == "metadata-run":
            item["Metadata"]["run-id"] = "100"
        elif scenario == "metadata-attempt":
            item["Metadata"]["run-attempt"] = "1"
        elif scenario == "repository":
            item["Metadata"]["repository-id"] = "999"
        elif scenario == "checksum":
            item["ChecksumSHA256"] = "wrong"
        else:
            provider = json.loads((work / "run.json").read_text())
            provider["run_attempt"] = 3
            (work / "run.json").write_text(json.dumps(provider))
        _s3_transport_fixture(work, objects)
        result = run_step(step_body("stage-group", "Wait for the exact speculative"), work, READINESS_ENV)
        assert result["ready"] == "false"


@needs_shell
def test_s3_poll_access_errors_fail_from_own_code(tmp_path):
    for operation in ("list-objects-v2", "head-object", "get-object"):
        for error in ("AccessDenied", "NoSuchBucket"):
            work = tmp_path / (operation + error)
            work.mkdir()
            objects = _s3_ready_fixture(work)
            _s3_transport_fixture(work, objects, {operation: error})
            with pytest.raises(AssertionError, match="::error::S3 poll failed"):
                run_step(step_body("stage-group", "Wait for the exact speculative"), work, READINESS_ENV)
    for error in ("AccessDenied", "NoSuchBucket"):
        work = tmp_path / ("supply" + error)
        work.mkdir()
        _s3_transport_fixture(work, {}, error)
        with pytest.raises(AssertionError, match="bucket .*transport policy"):
            run_step(step_body("stage-group", "Wait for the speculative supply"), work, READINESS_ENV)


@needs_shell
def test_s3_receipt_put_metadata_and_best_effort_upload_executed(tmp_path):
    steps = group_workflow_document()["jobs"]["stage-group"]["steps"]
    put = next(step for step in steps if step.get("name") == "Put the relay receipt in S3")
    assert put["if"] == "always() && steps.receipt.outcome == 'success' && steps.group.outputs.eligible == 'true'"
    upload = next(step for step in steps if step.get("uses", "").startswith("actions/upload-artifact"))
    assert upload["continue-on-error"] == "true"
    _s3_transport_fixture(tmp_path, {})
    run_step(step_body("stage-group", "Emit the relay receipt"), tmp_path, {
        "HEAD_SHA": READINESS_SOURCE_SHA, "BASE_SHA": READINESS_TREE, "MEMBERS": "[41]",
        "ELIGIBILITY": "merge group", "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "2",
        "CONFIGURED_SERVICES": group_workflow_document()["env"]["STAGE_SERVICES"],
    })
    receipt = json.loads((tmp_path / "prewarm-relay-receipt.json").read_text())
    assert receipt["dispatched"] == []
    assert receipt["configured_services"] == ["web"]
    assert receipt["configured_services"] == sorted(set(group_workflow_document()["env"]["STAGE_SERVICES"].split()))
    assert receipt["relay_run_id"] == "123" and receipt["relay_run_attempt"] == "2"
    run_step(put["run"], tmp_path, {
        "HEAD_SHA": READINESS_SOURCE_SHA, "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "2",
        "GITHUB_REPOSITORY_ID": "555", "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_WORKFLOW_REF": "LEAF-Solar-Design/leaf-web-demo/.github/workflows/prewarm-staging-group.yml@refs/heads/main",
    })
    call = json.loads((tmp_path / "s3-calls.jsonl").read_text())
    assert call[call.index("--key") + 1] == "mq/leaf-web-demo/relay/" + READINESS_SOURCE_SHA[:12] + "/123-2.json"
    metadata = dict(pair.split("=", 1) for pair in call[call.index("--metadata") + 1].split(","))
    assert metadata == {
        "run-id": "123", "run-attempt": "2", "repository-id": "555",
        "head-sha": READINESS_SOURCE_SHA, "event": "workflow_dispatch",
        "workflow-ref": "LEAF-Solar-Design/leaf-web-demo/.github/workflows/prewarm-staging-group.yml@refs/heads/main",
    }
    assert call[call.index("--if-none-match") + 1] == "*"
    assert call[call.index("--checksum-algorithm") + 1] == "SHA256"


@needs_shell
def test_s3_supply_exact_listing_executed(tmp_path):
    key = "mq/leaf-web-demo/supply-set/" + READINESS_TREE + ".json"
    _s3_transport_fixture(tmp_path, {key: _s3_object({}, 99, 2, 555, READINESS_SOURCE_SHA)})
    result = run_step(step_body("stage-group", "Wait for the speculative supply"), tmp_path, READINESS_ENV)
    assert result["present"] == "true"
    call = json.loads((tmp_path / "s3-calls.jsonl").read_text())
    assert call[1] == "list-objects-v2"
    assert call[call.index("--prefix") + 1] == key
    assert call[call.index("--max-keys") + 1] == "1"


@needs_shell
def test_s3_supply_absence_requires_exact_equality_without_head_executed(tmp_path):
    key = "mq/leaf-web-demo/supply-set/" + READINESS_TREE + ".json"
    for suffix in (None, ".foreign"):
        work = tmp_path / ("missing" if suffix is None else "neighbor")
        work.mkdir()
        objects = {} if suffix is None else {key + suffix: _s3_object({}, 99, 2, 555, READINESS_SOURCE_SHA)}
        _s3_transport_fixture(work, objects, {"head-object": "AccessDenied"})
        result = run_step(step_body("stage-group", "Wait for the speculative supply"), work, READINESS_ENV)
        assert result["present"] == "false"
        calls = [json.loads(line) for line in (work / "s3-calls.jsonl").read_text().splitlines()]
        assert [call[1] for call in calls] == ["list-objects-v2"]
        assert calls[0][calls[0].index("--prefix") + 1] == key
        assert calls[0][calls[0].index("--max-keys") + 1] == "1"


@needs_shell
def test_s3_readiness_retains_every_body_field_check_executed(tmp_path):
    import base64
    fields = ("schema", "source_tree", "source_sha", "image_tag", "producer_workflow_path",
              "producer_run_id", "producer_run_attempt", "repository_id")
    fields += tuple("digests." + name for name in ("app", "broker", "canonical_worker", "harness", "web"))
    for field in fields:
        work = tmp_path / field
        work.mkdir()
        objects = _s3_ready_fixture(work)
        key, item = next(iter(objects.items()))
        record = json.loads(base64.b64decode(item["body"]))
        if field.startswith("digests."):
            record["digests"][field.split(".")[1]] = "sha256:invalid"
        else:
            record[field] = 123 if isinstance(record[field], int) else "wrong"
        objects[key] = _s3_object(record, READINESS_RUN_ID, READINESS_RUN_ATTEMPT,
                                  READINESS_REPO_ID, READINESS_SOURCE_SHA)
        _s3_transport_fixture(work, objects)
        result = run_step(step_body("stage-group", "Wait for the exact speculative"), work, READINESS_ENV)
        assert result["ready"] == "false", field
        calls = [json.loads(line) for line in (work / "s3-calls.jsonl").read_text().splitlines()]
        assert any(call[1] == "get-object" for call in calls), field


@needs_shell
def test_s3_relay_put_refuses_collisions_and_transport_errors_executed(tmp_path):
    for error in ("PreconditionFailed (412)", "AccessDenied", "NoSuchBucket"):
        work = tmp_path / error.split()[0]
        work.mkdir()
        _s3_transport_fixture(work, {}, {"put-object": error})
        (work / "prewarm-relay-receipt.json").write_text('{"dispatched": []}')
        with pytest.raises(AssertionError, match="::error::Relay receipt write failed"):
            run_step(step_body("stage-group", "Put the relay receipt"), work, {
                "HEAD_SHA": READINESS_SOURCE_SHA, "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "2",
                "GITHUB_REPOSITORY_ID": "555", "GITHUB_EVENT_NAME": "workflow_dispatch",
                "GITHUB_WORKFLOW_REF": "LEAF-Solar-Design/leaf-web-demo/.github/workflows/prewarm-staging-group.yml@refs/heads/main",
            })
