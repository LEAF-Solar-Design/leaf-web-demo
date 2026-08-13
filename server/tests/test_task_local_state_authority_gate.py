"""A deploy must not silently destroy state that only LOOKS durable.

THE HAZARD, in one paragraph, because a future reader will otherwise
"simplify" this file away as ceremony.

Every module in this package resolves its storage path with the same idiom:
``os.environ.get("<VAR>", str(SERVER_DIR / "<name>"))``. When the variable is
set to a path on a mounted volume the state survives. When the variable is
UNSET the default wins, and that default resolves INSIDE THE IMAGE -- ``/app``
in ``deploy/Dockerfile.app``, on the container's writable layer. That layer is
created empty when the task starts (the image ships no such file) and is
destroyed when the task stops. An ordinary app-image deploy replaces the task,
so it destroys the file. Nothing in the deploy path notices, warns, or asks:
the service comes back healthy, serving an empty database, and the only symptom
is that history is gone.

This is not hypothetical. Staging left ``SESSIONS_DB`` unset, so
``server/session_store.py``, ``server/checkpoints.py`` and
``server/session_policy.py`` all put ``sessions``, ``session_events``,
``approvals``, ``session_checkpoints`` and ``session_policies`` in the
task-local ``server/sessions.db``. It was found only because
``scripts/reconcile_sessions_authority.py`` was written to READ that file, and
shipping the reconciler would have destroyed the data it existed to read
(``docs/POSTGRES-CUTOVER.md``, "Staging app-sessions pre-deploy source data:
DISCARD").

WHAT THIS GATE ENFORCES. It does NOT forbid task-local state -- some of it is
genuinely throwaway, and for ``SESSIONS_DB`` the recorded decision is that a
fresh durable path would be WORSE (an empty SQLite facing a populated
PostgreSQL fails every shadow comparison permanently, and the reconciler runs
SQLite-to-PostgreSQL only). What it forbids is state that is task-local
BY ACCIDENT AND IN SILENCE. Every path that defaults inside the image must be
declared in ``_TASK_LOCAL_STATE`` below, and every declaration must name a real
mechanism, verified here against the real producer, that stops a routine deploy
from destroying it without anyone deciding to:

  ``manifest_required``   the NAME is in ``deploy/required-config.*.json``, so
                          the terraform manifest check refuses a task
                          definition that omits it. Read this one precisely: it
                          removes the SILENT default, and that is all it does.
                          The manifest is a flat list of names and the check
                          only asserts membership, so a task definition that
                          sets ``JOBS_DB=/app/server/jobs.db`` satisfies it and
                          still loses the database on every replacement. What
                          the control buys is that SOMEBODY had to type a path,
                          which is reviewable, instead of a default nobody saw.
                          The ECS values live in the terraform repo, so no test
                          here can close that gap; the durability of an actual
                          value is only verified for the one deployment
                          producer this repo owns, ``docker-compose.yml``.
  ``selector_guarded``    the path is authoritative only under certain modes of
                          a selector variable, and every one of those selectors
                          is itself manifest-required. This is the shape
                          ``SESSIONS_DB`` needs, because its requirement is
                          conditional ("durable if and only if a mode still
                          reads the legacy SQLite") and a flat name list cannot
                          express a condition.
  ``read_only_input``     nothing ever writes it, so a task replacement removes
                          no state. Verified by scanning the resolving modules
                          for write primitives, not by assertion.
  ``derived_from``        the default is built from another state path, so it
                          inherits that one's durability. The anchor must itself
                          be governed.
  ``dark_unless``         the surface that reads it is not mounted unless a flag
                          is armed, and no producer in this repo arms it. Arming
                          it anywhere in-repo turns the gate red, which is the
                          point at which the path has to become durable.

TWO TIERS, because the alternative fails open. Tier one (``_TASK_LOCAL_STATE``)
is discovered mechanically: the scan evaluates the default expression and the
ledger is checked against what it found. Tier two (``_UNREADABLE_DEFAULTS``) is
for defaults the evaluator cannot compute -- a helper call, ``__file__``
arithmetic, a relative ``os.path.join``. Teaching the evaluator all of Python is
an endless job, and an evaluator that silently skips what it cannot read is the
worst outcome: the variable never reaches the ledger and nothing says a path
went unexamined. So the rule is NOT "the evaluator reads everything", it is
"nothing goes unexamined". Anything path-shaped and unreadable must be declared
by hand with its resolved path and its control, and its source expression is
pinned verbatim so an edit forces a human to re-derive rather than let the
recorded path drift. Tier two is how the operator control plane's four files and
three derived paths entered this ledger at all; the first version of this gate
could not see any of them.

WHY IT IS BUILT OUT OF THE REAL PRODUCERS, and not out of a hand-kept list.
Two green tests once pinned a Dockerfile COPY target and a documented command
string independently, and a broken command passed CI for weeks because neither
knew about ``WORKDIR``. So every input here is derived: the variables come from
an AST scan of the shipped source, the durable mounts come from the manifests,
the image root comes from the Dockerfile, the deployed values come from
``docker-compose.yml``. Each derivation asserts it found something before it
asserts anything about what it found, so a refactor that breaks a parser turns
this file red instead of vacuously green.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path, PurePosixPath

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SERVER = ROOT / "server"

# Proved against deploy/Dockerfile.app by
# test_the_image_root_is_where_the_ledger_says_it_is.
IMAGE_ROOT = PurePosixPath("/app")

_MANIFESTS = {
    "app": "deploy/required-config.app.json",
    "broker": "deploy/required-config.broker.json",
}

# The smallest server package that could plausibly still be this repo. A scan
# that sees fewer modules than this has broken, not shrunk.
_MIN_SCANNED_MODULES = 60


class _State:
    """One declared task-local state path.

    Every field is asserted against a real producer; nothing here is taken on
    trust. ``why`` is the part a human has to write, and it is the whole point
    of the ledger: it records that somebody decided, rather than that nobody
    noticed.
    """

    def __init__(
        self,
        *,
        container_default: str,
        modules: tuple[str, ...],
        manifests: tuple[str, ...],
        compose: dict[str, str],
        why: str,
        selectors: tuple[str, ...] = (),
        read_only: bool = False,
        compose_absent_why: str = "",
    ) -> None:
        self.container_default = PurePosixPath(container_default)
        self.modules = modules
        self.manifests = manifests
        self.compose = compose
        self.why = why
        self.selectors = selectors
        self.read_only = read_only
        self.compose_absent_why = compose_absent_why


_TASK_LOCAL_STATE: dict[str, _State] = {
    "BROKER_LEDGER": _State(
        container_default="/app/server/broker_ledger.jsonl",
        modules=("server/broker.py", "server/site_demo.py"),
        manifests=("app", "broker"),
        compose={
            "app": "/data/state/broker_ledger.jsonl",
            "broker": "/data/state/broker_ledger.jsonl",
        },
        why=(
            "Append-only spend and quota ledger. The broker writes it and the "
            "app reads it for /api/usage, so losing it loses billing history "
            "and silently resets every daily quota. Both manifests name it, so "
            "a task definition cannot omit it and inherit the image-local "
            "default."
        ),
    ),
    "BROKER_TENANTS": _State(
        container_default="/app/server/broker_tenants.json",
        modules=("server/broker.py", "server/routers/ops.py"),
        manifests=("broker",),
        compose={"broker": "/data/state/broker_tenants.json"},
        why=(
            "Persisted per-tenant kill switch. Only the broker writes it "
            "(server/broker.py does the atomic replace); server/routers/ops.py "
            "reads it in the app as a fallback view. So the broker manifest is "
            "the one that has to name it, and does."
        ),
    ),
    "JOBS_DB": _State(
        container_default="/app/server/jobs.db",
        modules=("server/da/callbacks.py", "server/jobs.py"),
        manifests=("app",),
        compose={"app": "/data/state/jobs.db"},
        why=(
            "Async job records and their callback receipts. Task-local would "
            "mean every deploy strands in-flight jobs with no record they ever "
            "existed. The app manifest names it."
        ),
    ),
    "LEAF_GUEST_STORE_DIR": _State(
        container_default="/app/server/guest_drawings",
        modules=("server/write_loop.py",),
        manifests=("app",),
        compose={},
        compose_absent_why=(
            "docker-compose.yml deliberately leaves it at the image-local "
            "default. Guest drawings are purged on a 24h retention timer "
            "(LEAF_GUEST_RETENTION_HOURS, LEAF_GUEST_PURGE_INTERVAL_S), so "
            "losing them on a compose restart destroys only work the service "
            "was about to delete anyway. The deployed app is NOT allowed the "
            "same shortcut: the app manifest requires the name."
        ),
        why=(
            "Guest (unauthenticated) uploaded drawings. Explicitly short-lived, "
            "so task-local is a defensible answer for a local compose stack and "
            "a stated one for the deployed app."
        ),
    ),
    "LEAF_STORE_DIR": _State(
        container_default="/app/server/drawings",
        modules=("server/write_loop.py",),
        manifests=("app", "broker"),
        compose={"app": "/data/drawings", "broker": "/data/drawings"},
        why=(
            "Tenant drawing files, the product's actual output. Both the app "
            "and the broker write here and must agree on the path, so both "
            "manifests name it."
        ),
    ),
    "LEAF_TENANTS_FILE": _State(
        container_default="/app/data/tenants.sample.json",
        modules=("server/tenancy.py",),
        manifests=(),
        compose={},
        read_only=True,
        why=(
            "A sample tenant catalogue shipped IN the image and only ever read "
            "(server/tenancy.py's JsonTenantStore parses it; nothing writes "
            "it). Image-local is correct for image-shipped input: a task "
            "replacement restores it rather than destroying it. If a write ever "
            "appears in that module this stops being true and the read-only "
            "check below turns red."
        ),
    ),
    "LEAF_UPLOADS_DIR": _State(
        container_default="/app/data/uploads",
        modules=("server/broker.py", "server/guest_uploads.py"),
        manifests=("app", "broker"),
        compose={"app": "/data/uploads", "broker": "/data/uploads"},
        why=(
            "Staged uploads handed from the app to the broker for extraction. "
            "Image-local would also break the handoff, not just durability, "
            "since the two containers would not share the directory."
        ),
    ),
    "LEAF_WORKSPACE_BASE": _State(
        container_default="/app/data/workspaces",
        modules=("server/tenancy.py",),
        manifests=(),
        compose={},
        read_only=True,
        why=(
            "Used only to COMPOSE a workspace_dir string for a Workspace "
            "record; server/tenancy.py never creates or writes the directory "
            "(provisioning is a downstream credential-broker job). Nothing is "
            "stored here, so nothing is lost."
        ),
    ),
    "SESSIONS_DB": _State(
        container_default="/app/server/sessions.db",
        modules=(
            "server/checkpoints.py",
            "server/session_policy.py",
            "server/session_store.py",
        ),
        manifests=(),
        selectors=("LEAF_SESSIONS_STORE", "LEAF_SESSION_ANNEX_STORE"),
        compose={"app": "/data/state/sessions.db"},
        why=(
            "The hazard this whole file was built for, and the one entry whose "
            "control is NOT its own manifest name. Requiring SESSIONS_DB would "
            "be the obvious fix and is the wrong one twice over. First, the "
            "manifest is a flat list of NAMES and the gate only asserts "
            "membership, so an explicitly task-local value would satisfy it "
            "while changing nothing. Second, the real requirement is "
            "conditional -- durable if and only if a selector mode still reads "
            "the legacy SQLite -- and under a legacy-touching mode a FRESH "
            "durable path is worse than the task-local one: it leaves an empty "
            "SQLite facing a populated PostgreSQL, so every existing row fails "
            "_shadow_equal permanently instead of only until the next task "
            "replacement, and scripts/reconcile_sessions_authority.py copies "
            "SQLite to PostgreSQL only, with no reverse direction. The control "
            "is therefore the two SELECTORS: neither may be absent from the app "
            "manifest, so no deploy can silently inherit the legacy default "
            "that makes this file authoritative. See docs/POSTGRES-CUTOVER.md, "
            "'The SESSIONS_DB drift is deliberate until the flip'. Do not add "
            "SESSIONS_DB to the manifest to make this test quieter; the "
            "manifest assertion below exists to catch exactly that edit."
        ),
    ),
}


class _Unreadable:
    """A task-local default the evaluator cannot compute, declared by hand.

    The second tier exists because the alternative is worse. Teaching
    ``_literal_path`` every expression Python can write is an endless job, and
    an evaluator that silently skips what it cannot read fails OPEN: the
    variable never reaches the ledger and nothing says a path went unexamined.
    So the rule is not "the evaluator must read everything", it is "nothing goes
    unexamined". An unreadable default is allowed exactly once somebody writes
    down what it resolves to and what stops a deploy destroying it.

    ``source`` is pinned verbatim, so editing the expression turns the gate red
    and the resolved path gets re-derived by a human instead of drifting.
    """

    def __init__(
        self,
        *,
        source: str,
        container_default: str,
        why: str,
        derived_from: str = "",
        read_only: bool = False,
        dark_unless: str = "",
    ) -> None:
        self.source = source
        self.container_default = PurePosixPath(container_default)
        self.why = why
        self.derived_from = derived_from
        self.read_only = read_only
        self.dark_unless = dark_unless


_UNREADABLE_DEFAULTS: dict[str, _Unreadable] = {
    "server/broker.py:BROKER_ACTIVE_WORKITEMS_PATH": _Unreadable(
        source="str(Path(LEDGER_PATH).parent / 'active_workitems.jsonl')",
        container_default="/app/server/active_workitems.jsonl",
        derived_from="BROKER_LEDGER",
        why=(
            "Sits beside the broker ledger by construction, so it is durable "
            "exactly when BROKER_LEDGER is and task-local exactly when it is "
            "not. Pointing the ledger at /data/state carries this with it."
        ),
    ),
    "server/jobs.py:PENDING_REAPS_PATH": _Unreadable(
        source="str(DB_PATH.parent / 'pending_reaps.jsonl')",
        container_default="/app/server/pending_reaps.jsonl",
        derived_from="JOBS_DB",
        why=(
            "Sits beside the jobs database by construction, so JOBS_DB's "
            "durability decides this one's. Same inheritance as the broker's "
            "active-workitems file."
        ),
    ),
    "server/customization_service.py:LEAF_CUSTOMIZATION_WORKTREES": _Unreadable(
        source="str(database_path().parent / 'customization-worktrees')",
        container_default="/app/server/customization-worktrees",
        derived_from="LEAF_CUSTOMIZATION_DB",
        why=(
            "database_path() returns LEAF_CUSTOMIZATION_DB when set and a module "
            "default otherwise, so the worktree directory follows the "
            "customization database. LEAF_CUSTOMIZATION_DB is required in the "
            "app manifest, so a deploy must state it and this follows."
        ),
    ),
    "server/operator_policy.py:LEAF_OPERATOR_POLICY_FILE": _Unreadable(
        source="str(Path(__file__).resolve().parent / 'operator_policy.json')",
        container_default="/app/server/operator_policy.json",
        read_only=True,
        dark_unless="LEAF_OPERATOR_ENABLED",
        why=(
            "Operator control-plane policy, shipped IN the image and only read. "
            "A task replacement restores it rather than destroying it."
        ),
    ),
    "server/operator_secret_broker.py:LEAF_OPERATOR_SECRETS_FILE": _Unreadable(
        source="str(Path(__file__).resolve().parent / 'operator_secrets.json')",
        container_default="/app/server/operator_secrets.json",
        read_only=True,
        dark_unless="LEAF_OPERATOR_ENABLED",
        why=(
            "Operator secret-broker descriptor, image-shipped and only read by "
            "server/operator_secret_broker.py."
        ),
    ),
    "server/operator_external_adapters.py:LEAF_OPERATOR_DESTINATIONS_FILE": _Unreadable(
        source="str(Path(__file__).resolve().parent / 'operator_external_destinations.json')",
        container_default="/app/server/operator_external_destinations.json",
        read_only=True,
        dark_unless="LEAF_OPERATOR_ENABLED",
        why=(
            "Allowed external destinations for the operator plane, image-shipped "
            "and only read."
        ),
    ),
    "server/operator_authority.py:LEAF_OPERATOR_KILL_FILE": _Unreadable(
        source="str(os.path.join('data', 'operator.disabled'))",
        container_default="/app/server/data/operator.disabled",
        dark_unless="LEAF_OPERATOR_ENABLED",
        why=(
            "The one entry here that is NOT read-only in the sense that matters, "
            "and it is deliberately not declared as such. The app never writes "
            "it -- kill_switch_active() only calls os.path.exists -- so the write "
            "scan sees nothing and would happily call it read-only. But the file "
            "is created OUT OF BAND by an operator to disable the control plane, "
            "and the default is a RELATIVE path, resolved against the image "
            "WORKDIR /app/server. So an operator who kills the surface loses the "
            "kill on the next deploy and the plane silently re-arms itself: the "
            "same hazard class as SESSIONS_DB, with a worse failure. Compare "
            "LEAF_AGENT_KILL_FILE, which docker-compose.yml puts on "
            "/data/state/agent.disabled. The reason this is not a live defect is "
            "reachability, not durability: the operator plane is dark unless "
            "LEAF_OPERATOR_ENABLED=1 (server/app.py _mount_operator_router), and "
            "no producer in this repo arms it. The dark_unless check below is "
            "what keeps that true -- arm the flag anywhere in-repo and this goes "
            "red, which is the moment to move the file to a durable mount."
        ),
    ),
}


# --------------------------------------------------------------------------- #
# derivations from the real producers                                          #
# --------------------------------------------------------------------------- #


def _scanned_modules() -> dict[str, ast.Module]:
    """Every shipped server module, parsed. Tests and fixtures excluded."""
    parsed: dict[str, ast.Module] = {}
    for py in sorted(SERVER.rglob("*.py")):
        rel = py.relative_to(ROOT).as_posix()
        parts = set(PurePosixPath(rel).parts)
        if "tests" in parts or PurePosixPath(rel).name == "conftest.py":
            continue
        try:
            parsed[rel] = ast.parse(py.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError) as exc:  # pragma: no cover
            pytest.fail(f"{rel} did not parse, so the scan cannot see it: {exc}")
    return parsed


def _literal_path(node: ast.expr) -> str | None:
    """Evaluate a default expression to a path, or None if it is not one.

    Only the forms this repo actually uses: a string constant, ``str(...)`` or
    ``Path(...)`` around one, the module anchors, and ``/`` joins between them.
    Anything else (a call, an f-string, an int) returns None rather than a
    guess -- a scan that guesses would be the vacuous kind.
    """
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.Name):
        return {
            "SERVER_DIR": str(SERVER),
            "PROJECT_ROOT": str(ROOT),
            "_PROJECT_ROOT": str(ROOT),
        }.get(node.id)
    if isinstance(node, ast.Attribute):
        # e.g. deps.SERVER_DIR
        return {"SERVER_DIR": str(SERVER), "PROJECT_ROOT": str(ROOT)}.get(node.attr)
    if isinstance(node, ast.Call):
        func = node.func
        name = getattr(func, "id", None) or getattr(func, "attr", None)
        if name in {"str", "Path"} and len(node.args) == 1:
            return _literal_path(node.args[0])
        return None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        left = _literal_path(node.left)
        right = _literal_path(node.right)
        if left is None or right is None:
            return None
        return str(Path(left) / right)
    return None


# Every way this package reads an env var WITH a fallback. A form absent from
# here is a form the scan cannot see, which is why
# test_the_default_expression_scan_reads_every_env_lookup_form exercises each
# one against a fixture instead of trusting this list.
def _env_default_calls(tree: ast.Module):
    """Yield (variable, default expression) for each env read with a default.

    ``os.environ["X"]`` is deliberately absent: a subscript has no default, so
    it cannot silently select a task-local path. Only the fallback forms can.
    """
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = getattr(func, "attr", None) or getattr(func, "id", None)
        if name == "get":
            if not isinstance(func, ast.Attribute):
                continue
            if ast.unparse(func.value) not in {"os.environ", "environ"}:
                continue
        elif name != "getenv":
            continue
        elif isinstance(func, ast.Attribute) and ast.unparse(func.value) not in {
            "os",
            "environ",
        }:
            continue

        if not node.args:
            continue
        key = node.args[0]
        if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
            continue

        default = None
        if len(node.args) >= 2:
            default = node.args[1]
        else:
            for keyword in node.keywords:
                if keyword.arg == "default":
                    default = keyword.value
        if default is None:
            continue
        yield key.value, default


# Markers that a default expression is TRYING to be a path. Used only to decide
# whether an expression the evaluator could not read is worth failing over.
_PATH_SHAPED = ("SERVER_DIR", "PROJECT_ROOT", "ROOT", "Path(", "os.path", "/")


def _unreadable_path_defaults() -> dict[str, str]:
    """Env defaults that LOOK like a path and that the evaluator cannot read.

    Without this the scan fails open on exactly the interesting case: a default
    built by a helper or an f-string is skipped, so its variable never reaches
    the ledger and nothing says so.
    """
    unreadable: dict[str, str] = {}
    for rel, tree in _scanned_modules().items():
        for var, default in _env_default_calls(tree):
            if _literal_path(default) is not None:
                continue
            source = ast.unparse(default)
            if any(marker in source for marker in _PATH_SHAPED):
                unreadable[f"{rel}:{var}"] = source
    return unreadable


def _discovered_state_paths() -> dict[str, dict]:
    """Env vars whose DEFAULT resolves to a path inside the repo working tree.

    Inside the repo working tree is the whole criterion, and it is the accurate
    one: the Dockerfile copies that tree to /app, so such a default lands on the
    container's writable layer and dies with the task.
    """
    found: dict[str, dict] = {}
    for rel, tree in _scanned_modules().items():
        for var, default_node in _env_default_calls(tree):
            default = _literal_path(default_node)
            if default is None or not Path(default).is_absolute():
                continue
            try:
                inside = Path(default).relative_to(ROOT).as_posix()
            except ValueError:
                continue
            entry = found.setdefault(
                var, {"container_default": IMAGE_ROOT / inside, "modules": set()}
            )
            entry["modules"].add(rel)
    return found


def _manifest(name: str) -> dict:
    data = json.loads((ROOT / _MANIFESTS[name]).read_text(encoding="utf-8"))
    required = data["required"]
    assert required["environment"], f"{_MANIFESTS[name]} declares no environment"
    assert required["mountPaths"], f"{_MANIFESTS[name]} declares no mountPaths"
    return required


def _durable_mounts() -> set[PurePosixPath]:
    mounts: set[PurePosixPath] = set()
    for name in _MANIFESTS:
        mounts.update(PurePosixPath(p) for p in _manifest(name)["mountPaths"])
    return mounts


def _on_a_durable_mount(path: PurePosixPath, mounts) -> bool:
    return any(path == m or m in path.parents for m in mounts)


def _compose() -> dict:
    data = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    services = data.get("services") or {}
    assert services, "docker-compose.yml declares no services"
    return services


def _dockerfile_instructions(relative: str) -> list[tuple[str, str]]:
    """Logical (KEYWORD, argument) pairs, backslash continuations joined."""
    text = (ROOT / relative).read_text(encoding="utf-8")
    logical: list[str] = []
    buffer = ""
    for raw in text.splitlines():
        line = raw.rstrip()
        if not buffer and (not line.strip() or line.lstrip().startswith("#")):
            continue
        if line.endswith("\\"):
            buffer += line[:-1] + " "
            continue
        logical.append((buffer + line).strip())
        buffer = ""
    if buffer.strip():
        logical.append(buffer.strip())
    instructions = []
    for line in logical:
        head, _, rest = line.partition(" ")
        if head.isalpha():
            instructions.append((head.upper(), rest.strip()))
    assert instructions, f"{relative} parsed to no instructions"
    return instructions


# --------------------------------------------------------------------------- #
# tests                                                                        #
# --------------------------------------------------------------------------- #


def test_the_source_scan_can_actually_see_the_server_package():
    """Anti-vacuity. Every check below is empty-set-true if the scan breaks."""
    modules = _scanned_modules()
    assert len(modules) >= _MIN_SCANNED_MODULES, (
        f"the AST scan found only {len(modules)} shipped server modules; "
        "every check in this file would pass vacuously. The scan is broken, "
        "not the package."
    )

    discovered = _discovered_state_paths()
    assert discovered, (
        "the scan found no env var defaulting to a path inside the repo. That "
        "has never been true of this package; the default-expression evaluator "
        "in _literal_path has stopped recognising the idiom."
    )

    # A known anchor, resolved by three separate modules. If the evaluator
    # regresses to only handling simple constants this goes red.
    assert "SESSIONS_DB" in discovered, (
        "the scan lost SESSIONS_DB, the variable this gate was built for"
    )
    assert discovered["SESSIONS_DB"]["modules"] == {
        "server/checkpoints.py",
        "server/session_policy.py",
        "server/session_store.py",
    }, (
        "the set of modules resolving SESSIONS_DB changed: "
        f"{sorted(discovered['SESSIONS_DB']['modules'])}"
    )


_SCAN_FIXTURE = '''
import os
from pathlib import Path
SERVER_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SERVER_DIR.parent

A = Path(os.environ.get("FORM_ENVIRON_GET", str(SERVER_DIR / "a.db")))
B = Path(os.getenv("FORM_GETENV", str(SERVER_DIR / "b.db")))
C = Path(os.environ.get("FORM_KEYWORD", default=str(PROJECT_ROOT / "data" / "c.db")))
D = Path(os.environ.get("FORM_PATH_CALL", Path(SERVER_DIR) / "d.db"))
E = os.environ.get("FORM_BARE_STRING", "/data/state/e.db")
F = os.environ.get("FORM_NOT_A_PATH", str(64 * 1024))
G = os.environ["FORM_NO_DEFAULT"]
'''


def test_the_default_expression_scan_reads_every_env_lookup_form():
    """A positive control, because the anchor test only proves ONE form works.

    Preserving SESSIONS_DB proves the scan did not die; it does not prove the
    scan covers the next variable somebody writes. A form this fixture does not
    exercise is a form a new task-local path can arrive through unseen, so the
    fixture is the coverage claim and it is executed rather than asserted.
    """
    tree = ast.parse(_SCAN_FIXTURE)
    seen = {var: _literal_path(default) for var, default in _env_default_calls(tree)}

    assert "FORM_NO_DEFAULT" not in seen, (
        "a bare os.environ[...] subscript has no default, so it cannot silently "
        "select a task-local path and must not be treated as a state path"
    )
    for var in (
        "FORM_ENVIRON_GET",
        "FORM_GETENV",
        "FORM_KEYWORD",
        "FORM_PATH_CALL",
        "FORM_BARE_STRING",
        "FORM_NOT_A_PATH",
    ):
        assert var in seen, (
            f"the scan does not see {var}. A shipped module written that way "
            "could put state on a task-local path and never reach the ledger."
        )

    for var in ("FORM_ENVIRON_GET", "FORM_GETENV", "FORM_KEYWORD", "FORM_PATH_CALL"):
        assert seen[var] is not None and Path(seen[var]).is_absolute(), (
            f"the scan sees {var} but cannot evaluate its default to a path, so "
            "it would be dropped before the in-repo test"
        )
    assert seen["FORM_BARE_STRING"] == "/data/state/e.db"
    # Not a path, and correctly not treated as one -- the in-repo filter, not a
    # special case, is what excludes it.
    assert not Path(seen["FORM_NOT_A_PATH"] or "x").is_absolute()


def test_no_env_default_that_looks_like_a_path_is_unreadable_to_the_scan():
    """Fail CLOSED on an expression the evaluator cannot read.

    Skipping what it cannot parse is how a scan fails open: the variable never
    reaches the ledger, and nothing anywhere says a path went unexamined.
    """
    unreadable = _unreadable_path_defaults()

    undeclared = sorted(set(unreadable) - set(_UNREADABLE_DEFAULTS))
    assert not undeclared, "\n".join(
        [
            "these env defaults look like filesystem paths but the evaluator in "
            "_literal_path cannot resolve them, so nothing has established "
            "whether they are task-local:",
        ]
        + [f"  {where} = {unreadable[where]}" for where in undeclared]
        + [
            "",
            "Teach _literal_path the form (and add it to _SCAN_FIXTURE), rewrite "
            "the default as a plain path expression, or declare it in "
            "_UNREADABLE_DEFAULTS with its resolved container path and the "
            "control that stops a deploy destroying it silently.",
        ]
    )

    stale = sorted(set(_UNREADABLE_DEFAULTS) - set(unreadable))
    assert not stale, (
        f"_UNREADABLE_DEFAULTS declares {stale}, which the scan no longer "
        "reports as an unreadable path-shaped default. Either the expression "
        "changed or the evaluator learned it; re-derive the entry or delete it."
    )

    for where, declared in sorted(_UNREADABLE_DEFAULTS.items()):
        assert unreadable[where] == declared.source, (
            f"{where}'s default expression is now {unreadable[where]!r}, not the "
            f"pinned {declared.source!r}. The resolved container path was "
            "derived by hand from the old expression and has to be re-derived."
        )


@pytest.mark.parametrize("where", sorted(_UNREADABLE_DEFAULTS))
def test_every_hand_declared_task_local_path_names_a_control(where: str):
    """Tier two answers to the same rule as tier one, by its own evidence."""
    declared = _UNREADABLE_DEFAULTS[where]
    assert declared.why.strip(), f"{where} declares no reason"
    assert not _on_a_durable_mount(declared.container_default, _durable_mounts()), (
        f"{where} resolves to {declared.container_default}, which is on a "
        "declared durable mount, so it does not belong in this ledger"
    )

    controls = []

    if declared.derived_from:
        anchor = declared.derived_from
        anchored = anchor in _TASK_LOCAL_STATE or any(
            anchor in set(_manifest(name)["environment"]) for name in _MANIFESTS
        )
        assert anchored, (
            f"{where} is declared to follow {anchor}, but {anchor} is neither a "
            "declared task-local state path nor required by any deployment "
            f"manifest. Then nothing governs {anchor}, so nothing governs this."
        )
        controls.append("derived_from")

    if declared.read_only:
        module = where.split(":")[0]
        writes = _writes_anything(module)
        assert not writes, (
            f"{where} is declared read-only, but {module} now calls write "
            f"primitives: {writes}. If any of them can reach "
            f"{declared.container_default}, a task replacement destroys state."
        )
        controls.append("read_only_input")

    if declared.dark_unless:
        flag = declared.dark_unless
        armed = []
        dockerfile = (ROOT / "deploy" / "Dockerfile.app").read_text(encoding="utf-8")
        if f"{flag}=1" in dockerfile:
            armed.append("deploy/Dockerfile.app")
        for service, spec in _compose().items():
            if str((spec.get("environment") or {}).get(flag, "")).strip() == "1":
                armed.append(f"docker-compose.yml:{service}")
        assert not armed, (
            f"{where} is only unreachable because {flag} is not armed, and "
            f"{armed} now arms it. The surface that reads "
            f"{declared.container_default} is live, and that path dies with the "
            f"task. Move it to a declared durable mount before enabling "
            f"{flag}.\n\n{declared.why}"
        )
        controls.append("dark_unless")

    assert controls, (
        f"{where} resolves to {declared.container_default}, which a deploy "
        "destroys, and declares no control at all."
    )


def test_the_image_root_is_where_the_ledger_says_it_is():
    """The ledger's container paths assume the tree lands at /app. Prove it.

    This is the WORKDIR-blindness guard. Pinning a COPY target and a path
    separately, with neither aware of where the image actually roots the tree,
    is how a broken command once passed CI for weeks.
    """
    instructions = _dockerfile_instructions("deploy/Dockerfile.app")

    workdirs = [arg for keyword, arg in instructions if keyword == "WORKDIR"]
    assert workdirs, "deploy/Dockerfile.app sets no WORKDIR"
    assert PurePosixPath(workdirs[0]) == IMAGE_ROOT, (
        f"deploy/Dockerfile.app roots the tree at {workdirs[0]!r}, not "
        f"{IMAGE_ROOT}, so every container_default in _TASK_LOCAL_STATE names "
        "a path that does not exist"
    )
    # The final WORKDIR is server/, which is why a relative default resolves
    # against the image tree at all. Recorded so a change to it is deliberate.
    assert PurePosixPath(workdirs[-1]) == IMAGE_ROOT / "server", (
        f"deploy/Dockerfile.app's final WORKDIR is {workdirs[-1]!r}"
    )

    for source, destination in (
        (arg.split()[0], arg.split()[-1])
        for keyword, arg in instructions
        if keyword == "COPY" and len(arg.split()) >= 2
    ):
        if source.startswith("--"):
            continue
        target = PurePosixPath(destination)
        if not target.is_absolute():
            target = IMAGE_ROOT / destination
        assert not _on_a_durable_mount(target, _durable_mounts()), (
            f"deploy/Dockerfile.app COPYs {source} to {destination}, which is "
            "under a declared durable mountPath. Image content under a mount is "
            "shadowed at runtime, and it would also make this gate vacuous by "
            "letting image-local paths count as durable."
        )


def test_no_declared_durable_mount_lives_inside_the_image_tree():
    """Otherwise 'task-local' and 'durable' stop being distinguishable."""
    for mount in sorted(_durable_mounts()):
        assert mount != IMAGE_ROOT and IMAGE_ROOT not in mount.parents, (
            f"declared durable mountPath {mount} is inside the image tree at "
            f"{IMAGE_ROOT}. Every task-local default would then classify as "
            "durable and this whole gate would pass vacuously."
        )


def test_every_task_local_state_path_is_declared():
    """A new state path that dies with the task cannot arrive unnoticed."""
    discovered = _discovered_state_paths()

    undeclared = sorted(set(discovered) - set(_TASK_LOCAL_STATE))
    assert not undeclared, "\n".join(
        [
            "these environment variables default to a path INSIDE the image, so "
            "whatever they hold is created empty at task start and destroyed by "
            "the next deploy, and nothing declares that:",
        ]
        + [
            f"  {var}: default resolves to {discovered[var]['container_default']} "
            f"(resolved in {', '.join(sorted(discovered[var]['modules']))})"
            for var in undeclared
        ]
        + [
            "",
            "Add each to _TASK_LOCAL_STATE in this file with the mechanism that "
            "stops a deploy destroying it silently, or point the default at a "
            "declared durable mount.",
        ]
    )

    stale = sorted(set(_TASK_LOCAL_STATE) - set(discovered))
    assert not stale, (
        f"_TASK_LOCAL_STATE declares {stale}, which no shipped server module "
        "resolves to an image-local default any more. Remove the entry (and "
        "check the variable did not simply move out of the scan's reach)."
    )

    for var, state in sorted(_TASK_LOCAL_STATE.items()):
        assert discovered[var]["container_default"] == state.container_default, (
            f"{var} now defaults to "
            f"{discovered[var]['container_default']}, not the declared "
            f"{state.container_default}"
        )
        assert discovered[var]["modules"] == set(state.modules), (
            f"{var} is resolved by "
            f"{sorted(discovered[var]['modules'])}, not the declared "
            f"{sorted(state.modules)}. A new resolver has to be reviewed "
            "against this variable's durability story, not inherited."
        )


@pytest.mark.parametrize("var", sorted(_TASK_LOCAL_STATE))
def test_each_declared_default_really_dies_with_the_task(var: str):
    """The ledger must never fill up with entries that were never at risk."""
    state = _TASK_LOCAL_STATE[var]
    mounts = _durable_mounts()
    assert not _on_a_durable_mount(state.container_default, mounts), (
        f"{var}'s default {state.container_default} is on a declared durable "
        f"mount, so it is not task-local and does not belong in this ledger"
    )


@pytest.mark.parametrize("var", sorted(_TASK_LOCAL_STATE))
def test_manifest_membership_matches_the_declaration(var: str):
    """Both directions. Adding a name is as reviewable as dropping one."""
    state = _TASK_LOCAL_STATE[var]
    actual = tuple(
        sorted(name for name in _MANIFESTS if var in set(_manifest(name)["environment"]))
    )
    assert actual == tuple(sorted(state.manifests)), (
        f"{var} is required by manifests {list(actual)}, but "
        f"_TASK_LOCAL_STATE declares {list(sorted(state.manifests))}. "
        f"Why this variable is declared the way it is:\n\n{state.why}"
    )


@pytest.mark.parametrize("var", sorted(_TASK_LOCAL_STATE))
def test_compose_puts_each_state_path_on_a_mount_that_service_actually_has(var: str):
    """Presence in a manifest is a name check; this is the VALUE check.

    docker-compose.yml is a real deployment producer in this repo, so it is the
    one place a durability claim can be verified rather than documented. A path
    set to a volume the service does not mount is the same silent loss with an
    extra step.
    """
    state = _TASK_LOCAL_STATE[var]
    services = _compose()

    actual = {
        name: (svc.get("environment") or {})[var]
        for name, svc in services.items()
        if var in (svc.get("environment") or {})
    }
    assert actual == state.compose, (
        f"docker-compose.yml sets {var} as {actual}, but _TASK_LOCAL_STATE "
        f"declares {state.compose}.\n\n{state.why}"
    )

    if not actual:
        assert state.compose_absent_why or state.read_only, (
            f"docker-compose.yml never sets {var}, so under compose it stays at "
            f"the image-local {state.container_default} and every restart of a "
            "rebuilt image destroys it. Declare compose_absent_why with the "
            "reason that is acceptable, or set it to a mounted path."
        )
        return

    for service, value in sorted(actual.items()):
        path = PurePosixPath(value)
        mounted = {
            PurePosixPath(entry.split(":")[1])
            for entry in (services[service].get("volumes") or [])
            if ":" in entry
        }
        assert mounted, f"compose service {service} sets {var} but mounts nothing"
        assert _on_a_durable_mount(path, mounted), (
            f"compose service {service} sets {var}={value}, which is not under "
            f"any volume it mounts ({sorted(str(m) for m in mounted)}). The "
            "value looks durable and is not."
        )
        # Checked against the SERVICE's own volumes, deliberately, and not
        # against the manifests' mountPaths. The two producers describe
        # different deployments and their mount sets are not the same set
        # today: compose mounts /data/uploads, /data/grants and /data/sessions,
        # none of which either required-config manifest lists. Asserting
        # equality here would be asserting a claim about the ECS task that
        # nothing in this repo can settle. What IS checkable, and is what
        # matters, is that each producer's own value sits on that producer's
        # own durable storage.


_WRITE_ATTRIBUTES = frozenset(
    {
        "write_text",
        "write_bytes",
        "mkdir",
        "makedirs",
        "touch",
        "unlink",
        "rmtree",
        "replace",
        "rename",
        "connect",
        "writelines",
        "dump",
    }
)


def _writes_anything(rel: str) -> list[str]:
    """Write primitives called ANYWHERE in a module. Empty means read-only.

    Module-wide on purpose, and yes that over-triggers: a write added elsewhere
    in the module turns the gate red even when it cannot reach the declared
    path. That is the safe direction. Proving a write cannot reach a path needs
    dataflow analysis this gate does not attempt, so the alternative to a false
    red is a false green on the one question that matters. A false red costs a
    human re-reading one module and either moving the write or replacing the
    read_only_input claim with a real control.
    """
    tree = _scanned_modules()[rel]
    hits: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        attribute = getattr(func, "attr", None)
        if attribute in _WRITE_ATTRIBUTES:
            hits.append(f"{ast.unparse(func)} (line {node.lineno})")
        elif getattr(func, "id", None) == "open":
            modes = [
                arg.value
                for arg in list(node.args[1:]) + [kw.value for kw in node.keywords]
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
            ]
            if any(set("wax+") & set(mode) for mode in modes):
                hits.append(f"open(mode={modes}) (line {node.lineno})")
    return hits


@pytest.mark.parametrize("var", sorted(_TASK_LOCAL_STATE))
def test_every_task_local_state_path_names_a_control_a_deploy_cannot_bypass(var: str):
    """The rule this file exists for.

    Task-local is allowed. Task-local with nothing stopping a routine deploy
    from destroying it, and nobody having decided that, is not.
    """
    state = _TASK_LOCAL_STATE[var]
    assert state.why.strip(), f"{var} declares no reason"

    controls = []
    if state.manifests:
        controls.append("manifest_required")
    if state.selectors:
        controls.append("selector_guarded")
    if state.read_only:
        controls.append("read_only_input")
    assert controls, (
        f"{var} defaults to {state.container_default}, which a deploy destroys, "
        "and declares no control: it is in no deployment manifest, behind no "
        "selector, and not read-only. A deploy would take it out silently. "
        "Require the name, gate it behind a manifest-required selector, or "
        "prove nothing writes it. Requiring the name makes the loss VISIBLE, "
        "not impossible -- somebody still has to set a durable value in the "
        "task definition, which lives outside this repo."
    )

    if state.read_only:
        writes = {rel: _writes_anything(rel) for rel in state.modules}
        offenders = {rel: hits for rel, hits in writes.items() if hits}
        assert not offenders, (
            f"{var} is declared read_only_input, but its resolving modules now "
            f"call write primitives: {offenders}. If that write can reach this "
            f"path, {var} holds state that {state.container_default} loses on "
            "every task replacement, and it needs a real control instead."
        )


def _names_selector(tree: ast.Module, selector: str) -> bool:
    """True only if a string literal EQUALS the selector.

    Equality, not substring, and the AST, not the file text. A comment or a
    docstring that discusses the selector in prose is not a module consulting
    it, and both this package's docstrings do exactly that -- checkpoints.py
    and session_policy.py describe LEAF_SESSION_ANNEX_STORE at length while
    actually reaching it through ``import session_annex``. Matching on prose
    would let the import branch below rot unnoticed.
    """
    return any(
        isinstance(node, ast.Constant) and node.value == selector
        for node in ast.walk(tree)
    )


def _imported_server_modules(tree: ast.Module) -> set[str]:
    """Sibling module basenames this module imports, in any ordinary shape.

    Handles ``import x``, ``import server.x``, ``from x import y``,
    ``from server.x import y`` and the relative ``from .x import y``. Only the
    last dotted component that names a real file is used, because that is the
    module whose source could define the selector.
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.update(alias.name.split("."))
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.update(node.module.split("."))
            if node.level:
                # `from . import session_annex` puts the module in names.
                names.update(alias.name for alias in node.names)
    return {name for name in names if (SERVER / f"{name}.py").exists()}


def _selector_reachable(rel: str, selector: str) -> bool:
    """True if the module consults the selector, directly or via an import.

    A NECESSARY condition, not a sufficient one: it shows the selector is in
    reach of the code that resolves the path, not that the selector governs
    every access. Proving governance would need dataflow analysis this gate
    does not attempt, and the weaker claim is still what catches the real
    regression -- a new resolver wired up with no selector at all.
    """
    tree = _scanned_modules()[rel]
    if _names_selector(tree, selector):
        return True
    for name in _imported_server_modules(tree):
        sibling = _scanned_modules().get(f"server/{name}.py")
        if sibling is not None and _names_selector(sibling, selector):
            return True
    return False


@pytest.mark.parametrize(
    "var", sorted(v for v, s in _TASK_LOCAL_STATE.items() if s.selectors)
)
def test_a_selector_guarded_path_has_a_manifest_required_selector_on_every_reader(
    var: str,
):
    """A selector only controls what it can be read by, and only if required.

    Both halves matter. A selector absent from the manifest can be omitted from
    a task definition, and its default is the legacy mode that makes this file
    authoritative -- the exact silence this gate exists to break. And a module
    that resolves the path while consulting no selector at all is authoritative
    unconditionally, so the selector story does not cover it.
    """
    state = _TASK_LOCAL_STATE[var]
    app_environment = set(_manifest("app")["environment"])

    for selector in state.selectors:
        assert selector in app_environment, (
            f"{var} is guarded by {selector}, but {selector} is not required in "
            f"{_MANIFESTS['app']}. A deploy can then omit it, its default is the "
            f"legacy mode, and legacy makes {state.container_default} the "
            f"authority -- destroyed by the next deploy, silently.\n\n{state.why}"
        )

    for rel in state.modules:
        reached = [s for s in state.selectors if _selector_reachable(rel, s)]
        assert reached, (
            f"{rel} resolves {var} but names none of its declared selectors "
            f"{list(state.selectors)}, directly or through an import. It "
            f"therefore treats {state.container_default} as authoritative "
            "unconditionally, and no selector can stop a deploy destroying what "
            "it writes there."
        )
