"""
Binary acceptance for WAVE 4 (backend) — the Build lane made MULTI-TENANT.

  Grant linking   /api/tenant/claude-grant link/status/unlink round-trip against a STUB
                  harness with a FAKE token — the app NEVER persists/logs/echoes it; and
                  harness-unreachable -> BROKER_UNREACHABLE 502.
  Grant-required  a missing per-tenant grant -> the §16 GRANT_REQUIRED shape (no template
                  fallback), keyed by `grant_required: true` / `reason: "GRANT_REQUIRED"`.
  Catalog scope   two fake tenant repos under a temp $LEAF_TENANTS_DIR: t1 sees its own
                  tool, t2 does NOT, both see the engine globals (/api/tools + /api/capabilities).
  Exec scope      t1's tool RESOLVES + RUNS from t1's repo at APS_LIVE=0; t2 cannot resolve it.
  Demo compat     the wave-3 single-repo env fallback ($LEAF_TENANT_REPO) still folds + runs.

Hermetic: FAKE tokens + temp dirs only. NEVER touches the real grant at
C:/tmp/hosted-oauth-spike/.grant/token (the app is a pure proxy to the stub harness; the
in-process fold/exec use seeded temp repos).

Run:  cd server && python -m pytest tests/test_wave4.py -q
"""
from __future__ import annotations

# Cache the STDLIB `platform` module BEFORE PROJECT_ROOT lands on sys.path (mirrors wave3).
import platform as _stdlib_platform  # noqa: E402
_stdlib_platform.python_implementation()

import http.server  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import socket  # noqa: E402
import tempfile  # noqa: E402
import threading  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
from pathlib import Path  # noqa: E402

import jsonschema  # noqa: E402
import pytest  # noqa: E402

SERVER_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = SERVER_DIR.parent

# route the jobs SQLite DB to a throwaway dir BEFORE `jobs` is imported anywhere.
os.environ.setdefault("JOBS_DB", str(Path(tempfile.mkdtemp(prefix="wave4-jobs-")) / "jobs.db"))

import sys  # noqa: E402
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

ENVELOPE_SCHEMA = json.loads((SERVER_DIR / "envelope_schema.json").read_text(encoding="utf-8"))
FAKE_TOKEN = "FAKE-OAUTH-not-a-real-token-wave4-abc123"


# =========================================================================== #
# helpers: seed a fake tenant repo (registry.json + optional tool files)
# =========================================================================== #
def _tool_pkg(name: str, entry: str | None = None) -> dict:
    pkg = {
        "name": name,
        "version": "1.0.0",
        "description": f"{name} (wave4 fake)",
        "kind": "script",
        "engine_op": name.replace("-", "_"),
        "params": {"type": "object", "properties": {}},
        "returns": {"type": "object"},
        "capabilities": ["drawing.read"],
        "provenance": {"author": "agent", "created": "2026-07-18T00:00:00Z"},
    }
    if entry:
        pkg["entry"] = entry
    return pkg


def _seed_tenant_repo(base: Path, tenant_id: str, tools: list[dict],
                      tool_files: dict[str, str] | None = None) -> Path:
    """Write <base>/<tenant_id>/registry.json + any tool files. No git needed for the
    Python fold/exec (resolution reads files directly)."""
    root = base / tenant_id
    root.mkdir(parents=True, exist_ok=True)
    (root / "registry.json").write_text(json.dumps({"tools": tools}, indent=2), encoding="utf-8")
    for rel, content in (tool_files or {}).items():
        f = root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content, encoding="utf-8")
    return root


# =========================================================================== #
# stub harness (grant admin + /author) — records the forwarded token
# =========================================================================== #
class _HarnessStub(http.server.BaseHTTPRequestHandler):
    STORE: dict = {}                 # tenant_id -> token (proves forwarding)
    LAST_PUT_TOKEN: str | None = None
    LAST_PUT_TENANT: str | None = None
    AUTHOR_STATUS: int = 200
    AUTHOR_BODY: dict = {}

    def _send(self, status: int, body: dict) -> None:
        raw = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _tenant(self) -> str:
        return self.path.rsplit("/", 1)[-1]

    def do_PUT(self):  # noqa: N802
        length = int(self.headers.get("content-length", 0) or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        cls = type(self)
        tid = self._tenant()
        cls.STORE[tid] = payload.get("token")
        cls.LAST_PUT_TOKEN = payload.get("token")
        cls.LAST_PUT_TENANT = tid
        self._send(200, {"linked": True,
                         "linked_at": datetime.now(timezone.utc).isoformat()})

    def do_GET(self):  # noqa: N802
        tid = self._tenant()
        linked = tid in type(self).STORE
        self._send(200, {"linked": linked,
                         "linked_at": datetime.now(timezone.utc).isoformat() if linked else None})

    def do_DELETE(self):  # noqa: N802
        type(self).STORE.pop(self._tenant(), None)
        self._send(200, {"linked": False, "linked_at": None})

    def do_POST(self):  # noqa: N802 (only /author)
        length = int(self.headers.get("content-length", 0) or 0)
        self.rfile.read(length)
        self._send(type(self).AUTHOR_STATUS, type(self).AUTHOR_BODY)

    def log_message(self, *a):  # silence
        return


@pytest.fixture
def harness_stub():
    _HarnessStub.STORE = {}
    _HarnessStub.LAST_PUT_TOKEN = None
    _HarnessStub.LAST_PUT_TENANT = None
    _HarnessStub.AUTHOR_STATUS = 200
    _HarnessStub.AUTHOR_BODY = {}
    srv = http.server.HTTPServer(("127.0.0.1", 0), _HarnessStub)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{port}", _HarnessStub
    finally:
        srv.shutdown()


def _client():
    from fastapi.testclient import TestClient
    from app import app
    return TestClient(app)


def _h(tenant: str) -> dict:
    return {"X-Tenant-Id": tenant}


# =========================================================================== #
# Contract 1 — grant link / status / unlink (app proxy, FAKE token)
# =========================================================================== #
def test_grant_link_status_unlink_roundtrip(monkeypatch, harness_stub):
    url, stub = harness_stub
    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", url)
    c = _client()

    # LINK — token forwarded to the harness, NEVER echoed by the app.
    r = c.post("/api/tenant/claude-grant", json={"token": FAKE_TOKEN}, headers=_h("acme"))
    assert r.status_code == 200, r.text
    b = r.json()
    jsonschema.validate(b, ENVELOPE_SCHEMA)
    assert b["linked"] is True and isinstance(b["linked_at"], str)
    assert "token" not in b and FAKE_TOKEN not in r.text
    assert stub.LAST_PUT_TENANT == "acme" and stub.LAST_PUT_TOKEN == FAKE_TOKEN  # forwarded

    # STATUS — never the token.
    r2 = c.get("/api/tenant/claude-grant", headers=_h("acme"))
    assert r2.status_code == 200
    assert r2.json()["linked"] is True and FAKE_TOKEN not in r2.text

    # a DIFFERENT tenant is not linked (per-tenant).
    assert c.get("/api/tenant/claude-grant", headers=_h("other")).json()["linked"] is False

    # UNLINK.
    r3 = c.delete("/api/tenant/claude-grant", headers=_h("acme"))
    assert r3.status_code == 200 and r3.json()["linked"] is False
    assert c.get("/api/tenant/claude-grant", headers=_h("acme")).json()["linked"] is False


def _free_closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()  # closed -> connection refused
    return port


def test_grant_harness_unreachable_is_502(monkeypatch):
    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", f"http://127.0.0.1:{_free_closed_port()}")
    c = _client()
    r = c.post("/api/tenant/claude-grant", json={"token": FAKE_TOKEN}, headers=_h("acme"))
    assert r.status_code == 502, r.text
    b = r.json()
    jsonschema.validate(b, ENVELOPE_SCHEMA)
    assert b["error"]["error_code"] == "BROKER_UNREACHABLE"


def test_grant_harness_not_configured_is_502(monkeypatch):
    monkeypatch.delenv("LEAF_AUTHOR_HARNESS_URL", raising=False)
    c = _client()
    r = c.get("/api/tenant/claude-grant", headers=_h("acme"))
    assert r.status_code == 502
    assert r.json()["error"]["error_code"] == "BROKER_UNREACHABLE"


# =========================================================================== #
# Contract 3 — missing-grant author -> the GRANT_REQUIRED shape (no fallback)
# =========================================================================== #
def test_author_grant_required_shape(monkeypatch, harness_stub):
    url, stub = harness_stub
    stub.AUTHOR_STATUS = 401
    stub.AUTHOR_BODY = {"grant_required": True,
                        "error": {"message": "tenant 'acme' has no linked Claude grant",
                                  "code": "grant_required"}}
    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", url)
    c = _client()

    r = c.post("/api/author", json={"description": "make a tool that tallies panels"},
               headers=_h("acme"))
    assert r.status_code == 401, r.text
    b = r.json()
    jsonschema.validate(b, ENVELOPE_SCHEMA)
    # keyable by the frontend; §10-valid error object; NOT a template fallback.
    assert b["grant_required"] is True
    assert b["reason"] == "GRANT_REQUIRED"
    assert b["source"] == "grant_required"
    assert b["tool"] is None
    assert b["error"]["error_code"] == "GRANT_REQUIRED" and b["error"]["retryable"] is False


# =========================================================================== #
# Contract 5 — per-tenant catalog isolation (/api/tools + /api/capabilities)
# =========================================================================== #
def test_catalog_isolation_between_tenants(monkeypatch, tmp_path):
    import deps
    base = tmp_path / "tenants"
    _seed_tenant_repo(base, "t1", [_tool_pkg("t1-only-tool", "tools/t1-only-tool/tool.py")])
    _seed_tenant_repo(base, "t2", [])  # t2 has NO tenant tools -> only globals
    monkeypatch.setenv("LEAF_TENANTS_DIR", str(base))
    monkeypatch.delenv("LEAF_TENANT_REPO", raising=False)
    monkeypatch.setattr(deps, "_AUTHORED", [])  # isolate the process-global authored layer

    c = _client()
    t1 = {t["name"] for t in c.get("/api/tools", headers=_h("t1")).json()["tools"]}
    t2 = {t["name"] for t in c.get("/api/tools", headers=_h("t2")).json()["tools"]}

    assert "t1-only-tool" in t1
    assert "t1-only-tool" not in t2                 # tenant A's tool invisible to tenant B
    assert t2, "t2 must still see the engine globals"
    assert t2 <= t1                                  # every global t2 sees is visible to t1
    assert (t1 - t2) == {"t1-only-tool"}             # the ONLY difference is t1's own tool
    assert "count-by-layer" in t1 and "count-by-layer" in t2  # a concrete shared global

    # /api/capabilities is scoped the same way for the folded portion.
    def _cap_names(body: dict) -> set:
        return {cap["name"] for fam in body["families"] for cap in fam["capabilities"]}
    c1 = _cap_names(c.get("/api/capabilities", headers=_h("t1")).json())
    c2 = _cap_names(c.get("/api/capabilities", headers=_h("t2")).json())
    assert "t1-only-tool" in c1 and "t1-only-tool" not in c2


# =========================================================================== #
# Contract 5 — per-tenant EXECUTION resolution (APS_LIVE=0)
# =========================================================================== #
_SENTINEL_TOOL_PY = (
    "def run(intake, params):\n"
    "    return ({'sentinel': 't1-repo-file'}, None)\n"
)


def test_execution_resolves_from_requesting_tenant_repo(monkeypatch, tmp_path):
    import deps
    from tool_loader import resolve_local_file, run_tool_dynamic
    base = tmp_path / "tenants"
    _seed_tenant_repo(base, "t1",
                      [_tool_pkg("echo-tenant", "tools/echo-tenant/tool.py")],
                      {"tools/echo-tenant/tool.py": _SENTINEL_TOOL_PY})
    _seed_tenant_repo(base, "t2", [])
    monkeypatch.setenv("LEAF_TENANTS_DIR", str(base))
    monkeypatch.delenv("LEAF_TENANT_REPO", raising=False)
    monkeypatch.setattr(deps, "_AUTHORED", [])

    tool = deps.find_tool("echo-tenant", "t1")
    assert tool is not None and tool.get("entry") == "tools/echo-tenant/tool.py"

    # resolves to t1's absolute file, runs t1's body.
    resolved = resolve_local_file(tool, "t1")
    assert resolved is not None and resolved.is_absolute()
    assert resolved == (base / "t1" / "tools" / "echo-tenant" / "tool.py")
    env = run_tool_dynamic(tool, {}, {}, aps_live=False, da=None, tenant_id="t1")
    assert env["ok"] is True and env["result"]["sentinel"] == "t1-repo-file"

    # t2 cannot resolve or run t1's tool (execution isolation).
    assert deps.find_tool("echo-tenant", "t2") is None
    assert resolve_local_file(tool, "t2") is None
    env2 = run_tool_dynamic(tool, {}, {}, aps_live=False, da=None, tenant_id="t2")
    assert env2["ok"] is False and env2["error"]["error_code"] == "BAD_PARAMS"


# =========================================================================== #
# Demo-tenant back-compat — the wave-3 single-repo env fallback still works
# =========================================================================== #
def test_demo_tenant_single_repo_backcompat(monkeypatch, tmp_path):
    import deps
    from tool_loader import run_tool_dynamic
    repo = tmp_path / "demo-repo"
    _seed_tenant_repo(repo.parent, repo.name,
                      [_tool_pkg("demo-fallback-tool", "tools/demo-fallback-tool/tool.py")],
                      {"tools/demo-fallback-tool/tool.py": _SENTINEL_TOOL_PY})
    monkeypatch.setenv("LEAF_TENANT_REPO", str(repo))
    monkeypatch.delenv("LEAF_TENANTS_DIR", raising=False)  # legacy single-repo mode
    monkeypatch.setattr(deps, "_AUTHORED", [])

    # folds for the demo tenant via the env fallback (no-arg + explicit).
    assert "demo-fallback-tool" in [t["name"] for t in deps.all_tools()]
    assert "demo-fallback-tool" in [t["name"] for t in deps.all_tools("demo-tenant")]

    # and runs.
    tool = deps.find_tool("demo-fallback-tool", "demo-tenant")
    env = run_tool_dynamic(tool, {}, {}, aps_live=False, da=None, tenant_id="demo-tenant")
    assert env["ok"] is True and env["result"]["sentinel"] == "t1-repo-file"

    # legacy single-repo mode: the ONE repo serves every tenant (documented back-compat;
    # isolation is the $LEAF_TENANTS_DIR mode exercised above).
    assert "demo-fallback-tool" in [t["name"] for t in deps.all_tools("some-other-tenant")]
