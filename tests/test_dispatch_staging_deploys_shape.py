#!/usr/bin/env python3
"""Exact-shape contract for .github/workflows/dispatch-staging-deploys.yml.

Runs from .github/workflows/contract.yml on every pull_request, next to
test_contract_workflow_shape.py. The relay's step SCRIPTS are frozen by
scripts/test_build_platform_images_workflow.py, which only runs on main
pushes; this file is the PR-time pin on the part of the relay that decides
whether a running relay survives the next merge: its concurrency block.

WHY (2026-09-02). From 2026-08-24 the relay ran in a fixed concurrency group
with cancel-in-progress: true. Every relay between 00:50Z and 03:07Z on
2026-09-02 (runs 33576940731, 33578177149, 33580565170, 33581652351) was
cancelled inside "Deploy each staging service in turn and prove each one
landed" by "a higher priority waiting request", after it had dispatched
both legs and before it had watched either to a terminal state. The legs
still landed (workflow_dispatch is fire-and-forget), but no relay published
a convergence receipt, none could retry an evicted leg, and a failed leg
would have failed with nobody watching. The workflow's own header calls a
relay that stops mid-release the exact split it was written to prevent.

Keys are pinned BY NAME with exact values. A count assertion over the block
would not notice `cancel-in-progress` flipping back to true, and that flip
is the whole regression. Every assertion is proven RED by mutation below.
"""

from pathlib import Path

import pytest

from test_contract_workflow_shape import _load

REPO_ROOT = Path(__file__).resolve().parent.parent
RELAY = REPO_ROOT / ".github" / "workflows" / "dispatch-staging-deploys.yml"

LANE = "dispatch-staging-deploys"
# The three conditions the dispatch job admits, in the order the job states
# them. The group predicate must be this text verbatim (see the pin below).
GATE = (
    "github.event.workflow_run.conclusion == 'success' && "
    "github.event.workflow_run.event == 'push' && "
    "github.event.workflow_run.head_branch == 'main'"
)
EXPECTED_GROUP = (
    "${{ (" + GATE + ") && '" + LANE + "' "
    "|| format('" + LANE + "-noop-{0}', github.run_id) }}"
)


def _relay_text() -> str:
    return RELAY.read_text(encoding="utf-8")


def check_relay_concurrency(text: str) -> None:
    """Every invariant the relay's concurrency block must hold.

    One function, used against both the real tree and the mutants, so a pin
    cannot pass the real file for a reason the battery never exercises.
    """
    wf = _load(text)
    assert "concurrency" in wf, "the relay must carry a workflow-level concurrency block"
    block = wf["concurrency"]
    assert isinstance(block, dict), "concurrency must be a mapping, not a bare group name"
    assert set(block) == {"group", "cancel-in-progress"}, (
        f"the relay concurrency block carries exactly group and cancel-in-progress; "
        f"found {sorted(block)}")

    # NEVER CANCEL A RUNNING RELAY. Pinned by key name and exact value: `is
    # False`, not `not block[...]`, so a missing key or a string "false"
    # cannot pass as the boolean the workflow needs.
    assert block["cancel-in-progress"] is False, (
        "cancel-in-progress must be the boolean false: a running relay that is "
        "cancelled after dispatching its legs loses its watch, its receipt and "
        "its eviction retries (four relays on 2026-09-02)")

    # ONE FIXED LANE FOR REAL RELAYS, A PRIVATE GROUP FOR EVERYTHING ELSE.
    # GitHub keeps at most one pending run per group and replaces it with each
    # newer one, so a fixed lane collapses the queue to the newest successful
    # main build. A completion the job would skip must not share that lane: it
    # would replace a pending main relay with a no-op.
    assert block["group"] == EXPECTED_GROUP, (
        "the group must be the fixed lane for exactly the completions the job "
        f"admits and a per-run group for the rest; found {block['group']!r}")

    # THE PREDICATE IS THE JOB GATE, VERBATIM. If the job admits a completion
    # the group sends to a private lane, that relay runs unserialized; if the
    # group serializes a completion the job skips, a no-op can displace a
    # pending relay. Pinning the two to the same text is what stops either.
    jobs = wf.get("jobs") or {}
    assert set(jobs) == {"dispatch"}, "the relay has exactly one job, `dispatch`"
    assert jobs["dispatch"].get("if") == GATE, (
        f"the dispatch job gate must be the pinned predicate; found "
        f"{jobs['dispatch'].get('if')!r}")
    assert "(" + GATE + ")" in block["group"], (
        "the group predicate must be the job gate verbatim")

    # The trigger is frozen too: the predicate reads workflow_run fields, so a
    # broader trigger would evaluate it against a payload that has none.
    triggers = wf.get(True, wf.get("on"))
    assert triggers == {
        "workflow_run": {
            "workflows": ["Build platform images"],
            "types": ["completed"],
        }
    }, f"the relay trigger is frozen; found {triggers!r}"


# --------------------------------------------------------------------------
# Green control: the checked-in workflow.
# --------------------------------------------------------------------------

def test_relay_concurrency_block_holds_in_real_tree():
    check_relay_concurrency(_relay_text())


def test_relay_group_predicate_is_the_dispatch_job_gate_verbatim():
    wf = _load(_relay_text())
    assert wf["jobs"]["dispatch"]["if"] == GATE
    assert wf["concurrency"]["group"] == EXPECTED_GROUP


# --------------------------------------------------------------------------
# Mutation battery: each fixture is the real file with ONE regression applied,
# and the same check must report it.
# --------------------------------------------------------------------------

def _mutate(old: str, new: str) -> str:
    text = _relay_text()
    assert text.count(old) == 1, f"battery fixture drifted: {old!r} occurs {text.count(old)} times"
    return text.replace(old, new)


_NEGATIVES = {
    "cancel-in-progress flipped back to true (the 2026-09-02 regression)": lambda: _mutate(
        "  cancel-in-progress: false\n", "  cancel-in-progress: true\n"),
    "cancel-in-progress as a string, which GitHub reads as truthy": lambda: _mutate(
        "  cancel-in-progress: false\n", '  cancel-in-progress: "false"\n'),
    "cancel-in-progress key removed (GitHub's default is no cancel, but the pin must be explicit)": lambda: _mutate(
        "  cancel-in-progress: false\n", ""),
    "group made unconditional, so a no-op completion can displace a pending relay": lambda: _mutate(
        "    && 'dispatch-staging-deploys'\n    || format('dispatch-staging-deploys-noop-{0}', github.run_id) }}\n",
        "    && 'dispatch-staging-deploys' || 'dispatch-staging-deploys' }}\n"),
    "group keyed per commit again, so relays run in parallel": lambda: _mutate(
        "    && 'dispatch-staging-deploys'\n",
        "    && format('dispatch-staging-deploys-{0}', github.event.workflow_run.head_sha)\n"),
    "group predicate drifted from the job gate": lambda: _mutate(
        "    github.event.workflow_run.head_branch == 'main')\n",
        "    github.event.workflow_run.head_branch == 'staging')\n"),
    "job gate drifted from the group predicate": lambda: _mutate(
        "      github.event.workflow_run.head_branch == 'main'\n",
        "      github.event.workflow_run.head_branch == 'staging'\n"),
    "concurrency block removed entirely": lambda: _mutate(
        "concurrency:\n", "concurrency_removed:\n"),
    "trigger broadened to push": lambda: _mutate(
        "on:\n  workflow_run:\n", "on:\n  push:\n  workflow_run:\n"),
}


@pytest.mark.parametrize("name", sorted(_NEGATIVES))
def test_mutation_is_caught(name):
    mutant = _NEGATIVES[name]()
    assert mutant != _relay_text()
    with pytest.raises(AssertionError):
        check_relay_concurrency(mutant)
