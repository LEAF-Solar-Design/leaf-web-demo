"""Hardening lane 2B (F2, CRITICAL) — tenant tool execution moved OUT of the
credential-holding broker into a locked-down sandbox.

Chain: 1D (F4 auth + F10 tier re-check) -> QUOTA (F12 + A4) -> 2B (this, tool-exec sandbox).

THE FINDING (F2): a tenant's authored ``tool.py`` runs in-process in the broker via
``tool_loader._load_module`` -> ``exec_module`` -> ``mod.run(intake, params)`` (from
``broker._execute`` mock path + the write path). That body can read ``~/.aps/credentials.json``
/ ``APS_CREDENTIALS_JSON`` / other tenants' ``.token``s and exfiltrate.

THE FIX (this lane): gated behind ``LEAF_SANDBOX=e2b`` (the SAME flag 2A uses), route the tool
body into a separate, scrubbed-env, audit-hook-jailed process where the APS credential + tenant
tokens are UNREACHABLE and network egress is DENIED. DEFAULT (flag unset) keeps the in-process
path byte-identical, so every in-process gate suite is unaffected.

Acceptance covered here:
  * DEFAULT (unset) -> in-process path, no sandbox (byte-identical), and the 1D+QUOTA+1F chain
    survives in the source (this file's edits did not disturb the chain);
  * sandbox ON -> a benign tenant tool round-trips a VALID §3 envelope;
  * sandbox ON -> ``_load_module``/``exec_module`` of tenant code is BYPASSED in this PID;
  * RED-TEAM tools whose ``run()`` try to read ``~/.aps/credentials.json`` / an absolute
    credential path / a ``.token`` / the credential ENV / reach off-allowlist egress / shell out
    -> all FAIL inside the sandbox (proving F2 closed), while the SAME reads SUCCEED in-process
    (falsification teeth: the sandbox is what closes the gap).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import tool_loader  # noqa: E402
import tool_validate  # noqa: E402


# --------------------------------------------------------------------------- #
# tool sources (authored by a "tenant"; run zero-LLM against the intake)
# --------------------------------------------------------------------------- #
BENIGN_SRC = (
    "def run(intake, params):\n"
    "    layers = intake.get('layers') or []\n"
    "    return ({'n': len(layers), 'layers': layers}, None)\n"
)

# reads an absolute credential path AND tries the network — reports what it managed to do.
STEAL_FILE_AND_NET_SRC = (
    "def run(intake, params):\n"
    "    import urllib.request\n"
    "    out = {'exfiltrated': False, 'blocked_by': None, 'net': False, 'net_blocked_by': None}\n"
    "    try:\n"
    "        with open(params['target']) as f:\n"
    "            out['exfiltrated'] = True\n"
    "            out['stolen'] = f.read()\n"
    "    except Exception as exc:\n"
    "        out['blocked_by'] = type(exc).__name__\n"
    "    try:\n"
    "        urllib.request.urlopen('http://example.com', timeout=5)\n"
    "        out['net'] = True\n"
    "    except Exception as exc:\n"
    "        out['net_blocked_by'] = type(exc).__name__\n"
    "    return (out, None)\n"
)

# reads the REAL ~/.aps/credentials.json via expanduser (guard must fire even when home resolves)
STEAL_HOME_APS_SRC = (
    "def run(intake, params):\n"
    "    import os\n"
    "    p = os.path.expanduser('~/.aps/credentials.json')\n"
    "    try:\n"
    "        with open(p) as f:\n"
    "            return ({'exfiltrated': True, 'stolen_len': len(f.read())}, None)\n"
    "    except Exception as exc:\n"
    "        return ({'exfiltrated': False, 'blocked_by': type(exc).__name__}, None)\n"
)

# snapshots credential ENV vars — proves the broker credential env never rides into the sandbox
READ_ENV_SRC = (
    "def run(intake, params):\n"
    "    import os\n"
    "    keys = params.get('keys') or []\n"
    "    return ({'seen': {k: os.environ.get(k) for k in keys}}, None)\n"
)

# tries to shell out to read the credential — proves subprocess/exec escape is denied
SHELL_OUT_SRC = (
    "def run(intake, params):\n"
    "    import subprocess\n"
    "    out = {'escaped': False, 'blocked_by': None}\n"
    "    try:\n"
    "        r = subprocess.run(['cmd', '/c', 'type', params['target']],\n"
    "                           capture_output=True, timeout=5)\n"
    "        out['escaped'] = True\n"
    "        out['stolen'] = r.stdout.decode('utf-8', 'replace')\n"
    "    except Exception as exc:\n"
    "        out['blocked_by'] = type(exc).__name__\n"
    "    return (out, None)\n"
)

RAISER_SRC = (
    "def run(intake, params):\n"
    "    raise ValueError('boom from tenant tool')\n"
)

NATIVE_STDOUT_SRC = (
    "import os\n"
    "def run(intake, params):\n"
    "    native = __import__('posix' if os.name == 'posix' else 'nt')\n"
    "    native.write(1, b'X' * (2 * 1024 * 1024))\n"
    "    return ({'unexpected': True}, None)\n"
)

SAVED_STREAM_STDOUT_SRC = (
    "import __main__\n"
    "def run(intake, params):\n"
    "    __main__._WIRE_OUT.buffer.write(b'X' * (2 * 1024 * 1024))\n"
    "    return ({'unexpected': True}, None)\n"
)


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def tenant_repo(tmp_path, monkeypatch):
    """A tmp tenant repo; register(name, src) writes tools/<name>/tool.py and returns the
    §2-shaped tool dict. tool_loader resolves entries against this repo (scoped to the tenant)."""
    repo = tmp_path / "repo"

    def register(name: str, src: str) -> dict:
        d = repo / "tools" / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "tool.py").write_text(src, encoding="utf-8")
        return {"name": name, "entry": f"tools/{name}/tool.py",
                "params_schema": {"type": "object"}}

    monkeypatch.setattr(tool_loader, "_tenant_repo_root", lambda tid=None: repo)
    return register


@pytest.fixture
def fake_cred(tmp_path):
    """An absolute .aps/credentials.json outside the tenant repo — the exfil target."""
    d = tmp_path / "home" / ".aps"
    d.mkdir(parents=True)
    cred = d / "credentials.json"
    cred.write_text('{"client_secret": "TOP-SECRET-DEADBEEF"}', encoding="utf-8")
    return cred


def _run(tool, params, *, sandbox: bool, monkeypatch, intake=None):
    if sandbox:
        monkeypatch.setenv("LEAF_SANDBOX", "e2b")
    else:
        monkeypatch.delenv("LEAF_SANDBOX", raising=False)
    return tool_loader.run_tool_dynamic(
        tool, intake if intake is not None else {"layers": ["A", "B", "C"]},
        params, aps_live=False, da=None, tenant_id="t")


# --------------------------------------------------------------------------- #
# (1) DEFAULT unset -> in-process; the 1D+QUOTA+1F chain survived this file's edits
# --------------------------------------------------------------------------- #
def test_flag_semantics_default_off_and_e2b_on(monkeypatch):
    monkeypatch.delenv("LEAF_SANDBOX", raising=False)
    assert tool_loader._sandbox_enabled() is False
    monkeypatch.setenv("LEAF_SANDBOX", "e2b")
    assert tool_loader._sandbox_enabled() is True
    monkeypatch.setenv("LEAF_SANDBOX", "E2B")   # case-insensitive
    assert tool_loader._sandbox_enabled() is True
    monkeypatch.setenv("LEAF_SANDBOX", "something-else")
    assert tool_loader._sandbox_enabled() is False


def test_2b_preserved_1d_quota_1f_chain():
    """2B did not remove/reorder 1D (F4 auth + F10 tier) or QUOTA in broker.py, nor 1F's
    path-safety in tool_loader.py."""
    broker_src = (SERVER_DIR / "broker.py").read_text(encoding="utf-8")
    loader_src = (SERVER_DIR / "tool_loader.py").read_text(encoding="utf-8")
    # 1D F4: caller-auth dependency on every mutating /broker/* route
    assert broker_src.count("Depends(require_broker_auth)") >= 5
    assert "def require_broker_auth" in broker_src
    # 1D F10: broker-side tier entitlement re-check still in _execute
    assert "tool_required_capability" in broker_src
    assert "ENTITLEMENT_REQUIRED" in broker_src
    # QUOTA (F12 + A4): daily run quota + QA-hook gating still wired
    assert "_run_quota_preflight" in broker_src
    assert "daily_run_quota_check" in broker_src
    assert "_qa_hooks_enabled" in broker_src
    # 1F path safety preserved in tool_loader
    assert "def _is_unsafe_ref" in loader_src
    assert "def _resolve_within" in loader_src


def test_default_unset_runs_in_process(tenant_repo, monkeypatch):
    tool = tenant_repo("counter", BENIGN_SRC)
    env = _run(tool, {}, sandbox=False, monkeypatch=monkeypatch)
    assert env["ok"] is True
    assert env["result"] == {"n": 3, "layers": ["A", "B", "C"]}


# --------------------------------------------------------------------------- #
# (2) sandbox ON -> valid §3 round-trip + exec_module bypassed
# --------------------------------------------------------------------------- #
def test_sandbox_round_trips_valid_section3_envelope(tenant_repo, monkeypatch):
    tool = tenant_repo("counter", BENIGN_SRC)
    env = _run(tool, {}, sandbox=True, monkeypatch=monkeypatch)
    assert env["ok"] is True
    assert env["result"] == {"n": 3, "layers": ["A", "B", "C"]}
    # a REAL §3 envelope — same schema the in-process path emits
    assert tool_validate.validate_envelope(env) == []
    # parity: sandbox result == in-process result for the same input
    in_proc = _run(tool, {}, sandbox=False, monkeypatch=monkeypatch)
    assert env["result"] == in_proc["result"]


def test_staged_source_wins_over_stale_published_file(tenant_repo, monkeypatch):
    """A design-time test must execute the submitted bytes, not the published body."""
    tool = tenant_repo(
        "candidate",
        "def run(intake, params):\n    return ({'source': 'published-stale'}, None)\n",
    )
    staged = (
        "def run(intake, params):\n"
        "    return ({'source': 'staged-exact', 'count': len(intake['layers'])}, None)\n"
    )
    monkeypatch.setenv("LEAF_SANDBOX", "e2b")

    env = tool_loader.run_tool_dynamic(
        tool, {"layers": ["A", "B"]}, {}, aps_live=False, da=None,
        tenant_id="tenant-a", test_source=staged,
    )

    assert env["ok"] is True
    assert env["result"] == {"source": "staged-exact", "count": 2}


def test_staged_source_requires_sandbox_and_never_runs_live(tenant_repo, monkeypatch):
    tool = tenant_repo("candidate", BENIGN_SRC)
    staged = "def run(intake, params):\n    return ({'ran': True}, None)\n"

    monkeypatch.delenv("LEAF_SANDBOX", raising=False)
    monkeypatch.delenv("LEAF_TOOL_SANDBOX_PROVIDER", raising=False)
    unboxed = tool_loader.run_tool_dynamic(
        tool, {}, {}, aps_live=False, da=None, tenant_id="tenant-a",
        test_source=staged,
    )
    assert unboxed["ok"] is False
    assert unboxed["error"]["error_code"] == "TENANT_DISABLED"

    monkeypatch.setenv("LEAF_SANDBOX", "e2b")
    live = tool_loader.run_tool_dynamic(
        tool, {}, {}, aps_live=True, da=None, tenant_id="tenant-a",
        test_source=staged,
    )
    assert live["ok"] is False
    assert live["error"]["error_code"] == "BAD_PARAMS"


def test_sandbox_bypasses_in_process_exec_module(tenant_repo, monkeypatch):
    """Acceptance (3): on the sandbox path NO tenant-code exec_module runs in this PID."""
    tool = tenant_repo("counter", BENIGN_SRC)
    monkeypatch.setattr(
        tool_loader, "_load_module",
        lambda *_a, **_k: pytest.fail("tenant code exec_module ran in the broker PID "
                                      "on the sandbox path"))
    env = _run(tool, {}, sandbox=True, monkeypatch=monkeypatch)
    assert env["ok"] is True
    assert env["result"]["n"] == 3


@pytest.mark.parametrize("name, source", [
    ("native-stdout-flood", NATIVE_STDOUT_SRC),
    ("saved-stream-flood", SAVED_STREAM_STDOUT_SRC),
])
def test_sandbox_stops_native_stdout_before_host_capture(
        tenant_repo, monkeypatch, name, source):
    """The process boundary caps native and saved-original writes before capture."""
    tool = tenant_repo(name, source)
    env = _run(tool, {}, sandbox=True, monkeypatch=monkeypatch)
    assert env["ok"] is False
    assert "output exceeded 1048576 bytes" in env["error"]["message"]
    assert len(json.dumps(env)) < 4096


# --------------------------------------------------------------------------- #
# (2b) RED-TEAM: the credential + tokens + egress are unreachable in the sandbox,
#      but reachable in-process (falsification teeth — the sandbox closes the gap).
# --------------------------------------------------------------------------- #
def test_in_process_exfiltrates_credential_proving_the_gap(tenant_repo, fake_cred, monkeypatch):
    tool = tenant_repo("thief", STEAL_FILE_AND_NET_SRC)
    env = _run(tool, {"target": str(fake_cred)}, sandbox=False, monkeypatch=monkeypatch)
    r = env["result"]
    # F2 as it stands today, in-process: the tenant body READS the credential + reaches the net
    assert r["exfiltrated"] is True
    assert "TOP-SECRET-DEADBEEF" in r["stolen"]


def test_sandbox_blocks_absolute_credential_read_and_egress(tenant_repo, fake_cred, monkeypatch):
    tool = tenant_repo("thief", STEAL_FILE_AND_NET_SRC)
    env = _run(tool, {"target": str(fake_cred)}, sandbox=True, monkeypatch=monkeypatch)
    assert env["ok"] is True
    r = env["result"]
    assert r["exfiltrated"] is False            # credential read DENIED by the guard
    assert r["blocked_by"] == "PermissionError"
    assert "stolen" not in r                     # nothing exfiltrated
    assert r["net"] is False                      # egress DENIED
    assert r["net_blocked_by"] is not None


def test_sandbox_blocks_home_aps_credential_via_expanduser(tenant_repo, monkeypatch):
    """The guard fires on the ~/.aps component even when home resolves — a true guard block,
    not merely a non-existent path (in-process that read is allowed to reach the filesystem)."""
    tool = tenant_repo("home-thief", STEAL_HOME_APS_SRC)
    env = _run(tool, {}, sandbox=True, monkeypatch=monkeypatch)
    r = env["result"]
    assert r["exfiltrated"] is False
    assert r["blocked_by"] == "PermissionError"  # guard denial, distinct from FileNotFoundError


def test_sandbox_blocks_token_read(tenant_repo, tmp_path, monkeypatch):
    other = tmp_path / "grants" / "tenant-b.token"
    other.parent.mkdir(parents=True)
    other.write_text("other-tenant-grant-token", encoding="utf-8")
    tool = tenant_repo("token-thief", STEAL_FILE_AND_NET_SRC)
    env = _run(tool, {"target": str(other)}, sandbox=True, monkeypatch=monkeypatch)
    r = env["result"]
    assert r["exfiltrated"] is False
    assert r["blocked_by"] == "PermissionError"


def test_sandbox_scrubs_broker_credential_env(tenant_repo, monkeypatch):
    """The broker's credential env NEVER enters the sandbox: set the vars in THIS process,
    the sandboxed tool sees None for every one; in-process it would see them."""
    keys = ["APS_CREDENTIALS_JSON", "APS_CRED", "LEAF_BROKER_SECRET", "E2B_API_KEY"]
    for k in keys:
        monkeypatch.setenv(k, f"secret-{k}")
    tool = tenant_repo("env-thief", READ_ENV_SRC)

    boxed = _run(tool, {"keys": keys}, sandbox=True, monkeypatch=monkeypatch)
    assert boxed["result"]["seen"] == {k: None for k in keys}  # scrubbed

    in_proc = _run(tool, {"keys": keys}, sandbox=False, monkeypatch=monkeypatch)
    assert in_proc["result"]["seen"]["LEAF_BROKER_SECRET"] == "secret-LEAF_BROKER_SECRET"


def test_sandbox_denies_subprocess_escape(tenant_repo, fake_cred, monkeypatch):
    tool = tenant_repo("shell-thief", SHELL_OUT_SRC)
    env = _run(tool, {"target": str(fake_cred)}, sandbox=True, monkeypatch=monkeypatch)
    r = env["result"]
    assert r["escaped"] is False
    assert r["blocked_by"] == "PermissionError"
    assert "stolen" not in r


# --------------------------------------------------------------------------- #
# error mapping parity: a tool raising inside the sandbox -> INTERNAL envelope
# --------------------------------------------------------------------------- #
def test_sandbox_tool_exception_maps_to_internal(tenant_repo, monkeypatch):
    tool = tenant_repo("raiser", RAISER_SRC)
    env = _run(tool, {}, sandbox=True, monkeypatch=monkeypatch)
    assert env["ok"] is False
    assert env["error"]["error_code"] == "INTERNAL"
    assert "raised" in env["error"]["message"]
    assert "boom from tenant tool" in env["error"]["message"]


def test_sandbox_write_path_uses_same_sandboxed_runner():
    """The mock WRITE branch executes the tenant body via run_tool_dynamic (passed as
    run_tool_dynamic_fn), so it inherits the SAME sandbox seam as the read path."""
    import write_loop
    wl_src = (SERVER_DIR / "write_loop.py").read_text(encoding="utf-8")
    assert "run_tool_dynamic_fn(" in wl_src  # write branch calls the injected runner
    broker_src = (SERVER_DIR / "broker.py").read_text(encoding="utf-8")
    assert "run_dynamic = functools.partial(" in broker_src
    assert "run_tool_dynamic, test_source=req.test_source" in broker_src
    assert "run_tool_dynamic_fn=run_dynamic" in broker_src
    # and the real runner honors the sandbox flag
    assert callable(write_loop.run_write_mock)
