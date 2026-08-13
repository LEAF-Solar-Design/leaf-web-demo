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
                          definition that omits it. Absence cannot be silent.
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


def _discovered_state_paths() -> dict[str, dict]:
    """Env vars whose DEFAULT resolves to a path inside the repo working tree.

    Inside the repo working tree is the whole criterion, and it is the accurate
    one: the Dockerfile copies that tree to /app, so such a default lands on the
    container's writable layer and dies with the task.
    """
    found: dict[str, dict] = {}
    for rel, tree in _scanned_modules().items():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or len(node.args) != 2:
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute) and func.attr == "get"):
                continue
            if ast.unparse(func.value) not in {"os.environ", "environ"}:
                continue
            key = node.args[0]
            if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
                continue
            default = _literal_path(node.args[1])
            if default is None or not Path(default).is_absolute():
                continue
            try:
                inside = Path(default).relative_to(ROOT).as_posix()
            except ValueError:
                continue
            entry = found.setdefault(
                key.value,
                {"container_default": IMAGE_ROOT / inside, "modules": set()},
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
    """Write primitives called anywhere in a module. Empty means read-only."""
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
        "prove nothing writes it."
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


def _selector_reachable(rel: str, selector: str) -> bool:
    """True if the module names the selector, directly or via an import."""
    source = (ROOT / rel).read_text(encoding="utf-8")
    if selector in source:
        return True
    tree = _scanned_modules()[rel]
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module.split(".")[0])
    for name in imported:
        sibling = SERVER / f"{name}.py"
        if sibling.exists() and selector in sibling.read_text(encoding="utf-8"):
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
