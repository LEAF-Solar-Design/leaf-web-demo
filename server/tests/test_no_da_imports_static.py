"""The "app imports no da/" static invariant as its OWN gate (census #4).

CONTRACT-ADDENDUM §8 (FROZEN): the tenant-facing app process NEVER holds the
APS credential; server/broker.py is the only process that loads da/client.py.
Until now the static half of that property lived inside test_backbone.py's
booted-stack e2e (app.py/jobs.py only). This suite is the standalone,
dependency-free version covering the WHOLE app-side surface — recursively
(builtins/, solver_adapters/, routers/, authored modules), at the AST level
(conditional / semicolon-joined / function-local imports all count), plus the
dynamic import_module/__import__ spellings — so CI fails the moment any
app-side file grows a da/ import, however it is written. No server boot.

The documented, allowed seams (checked here, not waived silently):
  * broker.py            — IS the broker process (loads da/client.py by path).
  * write_loop_live.py   — standalone live-receipt SCRIPT (`python
                           write_loop_live.py`), imported by nothing app-side;
                           runs credential-side by design.
  * deps.get_da_client() — the ONE §11 legacy loader for APS_LIVE=1 store
                           reads (OSSBackend); every call-site must be
                           APS_LIVE-gated (`... if deps.APS_LIVE else None`).
  * `import store` (da/store.py via write_loop's sys.path append) — the §11
    versioned-store seam; store.py itself imports da/client as a MODULE, but
    the credential file is only read on live token fetch (the dynamic
    APS_CRED=/nonexistent test in test_backbone.py proves APS_LIVE=0 stays
    credential-free).
  * deps._load_module_from(DA_DIR / "usage.py") — pure metering module, holds
    no credential (usage/ops routers).
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parent.parent

# Files that legitimately live on the credential side of the boundary —
# matched by path RELATIVE to server/, so only these two top-level files are
# exempt (a nested broker.py would still be swept).
CREDENTIAL_SIDE = {"broker.py", "write_loop_live.py"}

GET_DA_CLIENT_RE = re.compile(r"deps\.get_da_client\(\)")
# The §11 idiom every app-side call-site must use, verbatim:
GATED_IDIOM = "deps.get_da_client() if deps.APS_LIVE else None"
DA_PATH_LOAD_RE = re.compile(r"""DA_DIR\s*/\s*["']([\w.]+)["']""")
# importlib.import_module("da...") / __import__("da") / ...("client") — the
# dynamic spellings a line-regex import check cannot see.
DYNAMIC_IMPORT_RE = re.compile(
    r"""(?:import_module|__import__)\s*\(\s*['"](?:da(?:[.'"])|client['"])""")


def _app_side_files() -> list[Path]:
    """EVERY .py under server/ the app process could import — RECURSIVE, so
    app-loaded subpackages (builtins/, solver_adapters/, routers/, authored
    tool modules) are inside the net, not just the top level."""
    files = [p for p in sorted(SERVER_DIR.rglob("*.py"))
             if "__pycache__" not in p.parts
             and "tests" not in p.parts
             and p.relative_to(SERVER_DIR).as_posix() not in CREDENTIAL_SIDE]
    assert len(files) > 30, "app-side sweep found suspiciously few files"
    return files


def _rel(p: Path) -> str:
    return p.relative_to(SERVER_DIR).as_posix()


def _src(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _import_offenders(p: Path, roots: frozenset[str]) -> list[str]:
    """AST-walk every Import/ImportFrom whose root module is in `roots` —
    catches `if live: import da`, semicolon-joined statements, and
    function-local lazy imports that a line-anchored regex misses. An
    unparseable file is itself an offence (the sweep must see everything)."""
    try:
        tree = ast.parse(_src(p))
    except SyntaxError as exc:
        return [f"{_rel(p)}: unparseable app-side file ({exc})"]
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in roots:
                    out.append(f"{_rel(p)}:{node.lineno}: import {alias.name}")
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            if (node.module or "").split(".")[0] in roots:
                out.append(f"{_rel(p)}:{node.lineno}: from {node.module} import ...")
    return out


def test_no_app_side_file_imports_da_package():
    """No `import da` / `from da import` anywhere the app process can import —
    at any nesting, conditional or not (AST-level, not line-regex)."""
    offenders = [o for p in _app_side_files()
                 for o in _import_offenders(p, frozenset({"da"}))]
    assert not offenders, "app-side da import(s): " + "; ".join(offenders)


def test_no_app_side_file_imports_bare_client_module():
    """`import client` (da/client.py via the store seam's sys.path append) is
    credential-side only — write_loop_live.py and da/ itself, never the app."""
    offenders = [o for p in _app_side_files()
                 for o in _import_offenders(p, frozenset({"client"}))]
    assert not offenders, "app-side bare client import(s): " + "; ".join(offenders)


def _dynamic_import_offenders(src: str, rel: str) -> list[str]:
    """Full-text scan (NOT per-line): `import_module(\n"da")` split across
    lines must still match — \\s* in the pattern spans newlines when the whole
    source is searched at once."""
    return [f"{rel}:{src.count(chr(10), 0, m.start()) + 1}: {m.group(0)!r}"
            for m in DYNAMIC_IMPORT_RE.finditer(src)]


def test_no_app_side_dynamic_da_imports():
    """importlib.import_module / __import__ with a "da"/"client" literal —
    the dynamic spelling of the same boundary breach."""
    offenders = [o for p in _app_side_files()
                 for o in _dynamic_import_offenders(_src(p), _rel(p))]
    assert not offenders, "app-side dynamic da/client import(s): " + "; ".join(offenders)


def test_dynamic_import_scanner_catches_multiline_calls():
    """Self-test: the scanner must see calls split across lines and both
    quote styles; a per-line scan would miss every one of these."""
    smuggled = 'x = import_module(\n    "da.client")\ny = __import__(\n"client")\n'
    hits = _dynamic_import_offenders(smuggled, "synthetic.py")
    assert len(hits) == 2, hits
    assert hits[0].startswith("synthetic.py:1:")
    assert hits[1].startswith("synthetic.py:3:")
    assert not _dynamic_import_offenders('import_module("engine_selfcheck")\n', "ok.py")


def test_app_and_jobs_never_reference_da_client():
    """The frozen §8 wording, verbatim, as its own gate: app.py/jobs.py contain
    no da import and no `da.client` reference (mirrors test_backbone check 4)."""
    for fname in ("app.py", "jobs.py"):
        offenders = _import_offenders(SERVER_DIR / fname, frozenset({"da"}))
        assert not offenders, "; ".join(offenders)
        assert "da.client" not in _src(SERVER_DIR / fname), f"{fname} references da.client"


def test_broker_client_reaches_execution_over_http_only():
    """broker_client.py is the app's ONLY road to APS-capable execution — it
    must stay a pure HTTP client: no da import, no path-based module loading."""
    src = _src(SERVER_DIR / "broker_client.py")
    offenders = _import_offenders(SERVER_DIR / "broker_client.py", frozenset({"da"}))
    assert not offenders, "; ".join(offenders)
    assert "spec_from_file_location" not in src
    assert "_load_module_from" not in src
    assert "DA_DIR" not in src


def test_every_get_da_client_call_site_is_aps_live_gated():
    """Every app-side use of the §11 legacy loader must be the gated idiom
    `deps.get_da_client() if deps.APS_LIVE else None` — never unconditional.
    (deps.py itself defines the function; call-sites live elsewhere.)"""
    offenders = []
    for p in _app_side_files():
        if p.name == "deps.py":
            continue
        for i, line in enumerate(_src(p).splitlines(), 1):
            if GET_DA_CLIENT_RE.search(line) and GATED_IDIOM not in line:
                offenders.append(f"{_rel(p)}:{i}: {line.strip()}")
    assert not offenders, "ungated get_da_client call(s): " + "; ".join(offenders)


def test_da_path_loads_target_only_documented_pure_modules():
    """App-side `DA_DIR / "<file>"` references may target only the documented
    seams: usage.py (pure metering) everywhere, client.py only inside
    deps.get_da_client (the loader) and app.py's health presence-probe
    (`.exists()` — reads nothing)."""
    allowed_anywhere = {"usage.py"}
    client_allowed_in = {"deps.py", "app.py"}
    offenders = []
    for p in _app_side_files():
        for i, line in enumerate(_src(p).splitlines(), 1):
            for target in DA_PATH_LOAD_RE.findall(line):
                if target in allowed_anywhere:
                    continue
                if target == "client.py" and p.name in client_allowed_in:
                    if p.name == "app.py" and ".exists()" not in line:
                        offenders.append(f"{_rel(p)}:{i}: {line.strip()}")
                    continue
                offenders.append(f"{_rel(p)}:{i}: {line.strip()}")
    assert not offenders, "undocumented DA_DIR load(s): " + "; ".join(offenders)
