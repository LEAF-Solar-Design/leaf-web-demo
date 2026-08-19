#!/usr/bin/env python3
"""Contract check for .github/workflows/ (card PIPE-11).

Runs from .github/workflows/contract.yml on every pull_request. Mirrors the
intent of the tf apply-safety-contract.yml, scoped to what leaf-web-demo
workflows must hold:

  * every workflow file parses as YAML, with no duplicate mapping keys
    silently shadowing an earlier value
  * every job declares runs-on (or uses: for a reusable workflow call)
  * no workflow grants permissions: write-all, top-level or per-job
  * contract.yml itself is pull_request-triggered, exposes a job id
    `contract`, and carries no concurrency block (the tf immune pattern;
    do not copy test-gate.yml's cancel-in-progress: true + per-PR group)

Every assertion function below is proven RED by mutation: a fixture string
that violates it is fed through the exact same function used against the
real tree, and the violation must be reported. The real-tree tests are the
green control -- the checked-in workflows must report zero violations.
"""

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
CONTRACT_WORKFLOW = WORKFLOWS_DIR / "contract.yml"


class _StrictLoader(yaml.SafeLoader):
    """SafeLoader that refuses duplicate mapping keys.

    yaml.safe_load silently keeps the LAST duplicate key, so a workflow with
    two `jobs:` blocks (or two same-named jobs) would show these assertions
    only the final one. Ambiguity fails loud here instead.
    """

    def construct_mapping(self, node, deep=False):
        seen = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=True)
            if key in seen:
                raise ValueError(
                    f"duplicate YAML mapping key {key!r} at {key_node.start_mark}"
                )
            seen.add(key)
        return super().construct_mapping(node, deep)


def _load(text: str) -> dict:
    return yaml.load(text, Loader=_StrictLoader)


def _all_workflow_files() -> list[Path]:
    return sorted(WORKFLOWS_DIR.glob("*.yml"))


def get_triggers(workflow: dict) -> set:
    """Return the set of trigger names a workflow reacts to.

    YAML 1.1 parses the bare `on:` key as the boolean True unless it is
    quoted, so PyYAML surfaces it under the True key, not the string "on".
    """
    raw = workflow.get(True, workflow.get("on"))
    if isinstance(raw, dict):
        return set(raw.keys())
    if isinstance(raw, list):
        return set(raw)
    if isinstance(raw, str):
        return {raw}
    return set()


def find_jobs_missing_runs_on(workflow: dict) -> list[str]:
    """Return job ids that declare neither runs-on nor uses:."""
    jobs = workflow.get("jobs") or {}
    missing = []
    for job_id, job in jobs.items():
        if not isinstance(job, dict) or ("runs-on" not in job and "uses" not in job):
            missing.append(job_id)
    return missing


def _grants_write_all(permissions) -> bool:
    if permissions == "write-all":
        return True
    if isinstance(permissions, dict):
        return "write-all" in permissions.values()
    return False


def find_write_all_permissions(workflow: dict) -> list[str]:
    """Return locations ("top-level" or "job:<id>") granting write-all."""
    hits = []
    if _grants_write_all(workflow.get("permissions")):
        hits.append("top-level")
    for job_id, job in (workflow.get("jobs") or {}).items():
        if isinstance(job, dict) and _grants_write_all(job.get("permissions")):
            hits.append(f"job:{job_id}")
    return hits


# ---------------------------------------------------------------------------
# Real-tree assertions (the green control): every checked-in workflow must
# report zero violations for each of the three checks above.
# ---------------------------------------------------------------------------


def test_all_workflow_files_parse_as_yaml():
    failures = {}
    for path in _all_workflow_files():
        try:
            _load(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 - report every parse failure
            failures[path.name] = str(exc)
    assert failures == {}, f"workflow files failed to parse: {failures}"


def test_no_job_missing_runs_on_in_real_tree():
    violations = {}
    for path in _all_workflow_files():
        wf = _load(path.read_text(encoding="utf-8"))
        missing = find_jobs_missing_runs_on(wf)
        if missing:
            violations[path.name] = missing
    assert violations == {}, f"jobs missing runs-on/uses: {violations}"


def test_no_write_all_permissions_in_real_tree():
    violations = {}
    for path in _all_workflow_files():
        wf = _load(path.read_text(encoding="utf-8"))
        hits = find_write_all_permissions(wf)
        if hits:
            violations[path.name] = hits
    assert violations == {}, f"permissions: write-all found: {violations}"


# ---------------------------------------------------------------------------
# contract.yml's own shape.
# ---------------------------------------------------------------------------


def test_contract_workflow_exists():
    assert CONTRACT_WORKFLOW.exists(), "contract.yml must exist under .github/workflows/"


def test_contract_workflow_triggers_on_pull_request():
    wf = _load(CONTRACT_WORKFLOW.read_text(encoding="utf-8"))
    assert "pull_request" in get_triggers(wf)


def test_contract_workflow_has_contract_job_id():
    wf = _load(CONTRACT_WORKFLOW.read_text(encoding="utf-8"))
    assert "contract" in (wf.get("jobs") or {})


def test_contract_workflow_has_no_concurrency_block():
    wf = _load(CONTRACT_WORKFLOW.read_text(encoding="utf-8"))
    assert "concurrency" not in wf, (
        "contract.yml must not carry a concurrency block (tf immune "
        "pattern) -- do not copy test-gate.yml's cancel-in-progress: true "
        "+ per-PR group"
    )


# ---------------------------------------------------------------------------
# Mutation proof: each real-tree assertion above is provably RED. A fixture
# that violates it must fail the same check that the real tree passes.
# ---------------------------------------------------------------------------

_FIXTURE_MISSING_RUNS_ON = """
name: fixture
on:
  pull_request: {}
jobs:
  broken:
    steps:
      - run: echo hi
"""

_FIXTURE_WRITE_ALL_TOP_LEVEL = """
name: fixture
on:
  pull_request: {}
permissions: write-all
jobs:
  ok:
    runs-on: ubuntu-latest
    steps:
      - run: echo hi
"""

_FIXTURE_WRITE_ALL_JOB_LEVEL = """
name: fixture
on:
  pull_request: {}
jobs:
  ok:
    runs-on: ubuntu-latest
    permissions:
      contents: write-all
    steps:
      - run: echo hi
"""

_FIXTURE_DUPLICATE_KEY = """
name: fixture
on:
  pull_request: {}
jobs:
  a:
    runs-on: ubuntu-latest
  a:
    runs-on: ubuntu-latest
"""

_FIXTURE_NO_PULL_REQUEST_TRIGGER = """
name: fixture-contract
on:
  workflow_dispatch: {}
jobs:
  contract:
    runs-on: ubuntu-latest
"""

_FIXTURE_MISSING_CONTRACT_JOB = """
name: fixture-contract
on:
  pull_request: {}
jobs:
  other:
    runs-on: ubuntu-latest
"""

_FIXTURE_CONCURRENCY_BLOCK = """
name: fixture-contract
on:
  pull_request: {}
jobs:
  contract:
    runs-on: ubuntu-latest
concurrency:
  group: fixture
  cancel-in-progress: true
"""


def test_mutation_missing_runs_on_is_caught():
    wf = _load(_FIXTURE_MISSING_RUNS_ON)
    assert find_jobs_missing_runs_on(wf) == ["broken"]


def test_mutation_write_all_top_level_is_caught():
    wf = _load(_FIXTURE_WRITE_ALL_TOP_LEVEL)
    assert find_write_all_permissions(wf) == ["top-level"]


def test_mutation_write_all_job_level_is_caught():
    wf = _load(_FIXTURE_WRITE_ALL_JOB_LEVEL)
    assert find_write_all_permissions(wf) == ["job:ok"]


def test_mutation_duplicate_keys_are_caught():
    with pytest.raises(ValueError, match="duplicate YAML mapping key"):
        _load(_FIXTURE_DUPLICATE_KEY)


def test_mutation_missing_pull_request_trigger_is_caught():
    wf = _load(_FIXTURE_NO_PULL_REQUEST_TRIGGER)
    assert "pull_request" not in get_triggers(wf)


def test_mutation_missing_contract_job_is_caught():
    wf = _load(_FIXTURE_MISSING_CONTRACT_JOB)
    assert "contract" not in (wf.get("jobs") or {})


def test_mutation_concurrency_block_is_caught():
    wf = _load(_FIXTURE_CONCURRENCY_BLOCK)
    assert "concurrency" in wf
