"""Hardening lane 2C — the opt-in ``e2b-microvm`` sandbox tier (Lane A, tool_loader.py).

Mirrors ``test_hardening_2b.py``'s fixture/`_run` idioms, but instead of shelling out to the
REAL Node helper (external network + E2B API key + a booted micro-VM — not available/desired in
a unit-test run), every microvm test monkeypatches ``tool_loader._microvm_cmd`` (THE seam) to
point at a small hermetic Python fake that speaks the exact stdin/stdout protocol
``_run_in_sandbox_e2b`` uses (schemas ``leaf.e2b.tool-exec-job.v1`` / ``leaf.e2b.tool-exec-result.v1``).
This proves the broker-side wiring (tier selection, seam, env scrub, error mapping, timeout)
without needing a real E2B account.

Acceptance covered here:
  * ``_sandbox_tier()`` tri-state (off/subprocess/microvm) incl. case-insensitivity + garbage,
    and ``_sandbox_enabled()`` keeps its exact v1 contract (True only for e2b/E2B);
  * microvm ON + a benign tool -> a valid §3 envelope, parity with the in-process result;
  * microvm ON -> neither `_load_module` (in-process exec) nor the subprocess-tier
    `_run_in_sandbox` ever run (proves no in-process exec and no silent tier fallback);
  * `LEAF_SANDBOX=e2b` still routes through the REAL subprocess tier, not the microvm helper
    (back-compat: the v1 flag value is untouched by the new tri-state);
  * a large (~1MB) intake payload round-trips through the stdin channel intact;
  * the helper's OWN env gets the E2B key but NOT the broker's APS/LEAF secrets
    (`_microvm_env` == the default-deny allowlist + E2B_API_KEY/E2B_API_KEY_FILE only);
  * helper_error / failed egress receipt / tenant exception / non-JSON output / missing node /
    timeout all map to the SAME INTERNAL envelope shape the subprocess tier uses.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import tool_loader  # noqa: E402
import tool_validate  # noqa: E402


# --------------------------------------------------------------------------- #
# tool sources (authored by a "tenant")
# --------------------------------------------------------------------------- #
BENIGN_SRC = (
    "def run(intake, params):\n"
    "    layers = intake.get('layers') or []\n"
    "    return ({'n': len(layers), 'layers': layers}, None)\n"
)

LARGE_ECHO_SRC = (
    "def run(intake, params):\n"
    "    big = intake.get('filler') or ''\n"
    "    return ({'filler_len': len(big)}, None)\n"
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


# ---- the fake Node-helper substitute: a hermetic Python script that speaks the exact
# stdin-job / stdout-result protocol `_run_in_sandbox_e2b` sends/expects. THE monkeypatch
# seam is `tool_loader._microvm_cmd` -> [sys.executable, str(this script)]. ----
_FAKE_HELPER_PREAMBLE = '''
import sys, json, os

def _read_job():
    raw = sys.stdin.buffer.read()
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return {}

def _encode_ret(ret):
    if isinstance(ret, tuple) and len(ret) == 2:
        return {"form": "pair", "result": ret[0], "overlay": ret[1]}
    if isinstance(ret, dict):
        return {"form": "dict", "obj": ret}
    return {"form": "scalar", "value": ret}

def _emit(obj):
    sys.stdout.buffer.write(json.dumps(obj).encode("utf-8"))
    sys.stdout.buffer.flush()

'''

_FAKE_HELPER_BODIES = {
    # actually execute the job's tenant source (like the real VM would), emit a passing
    # receipt + an ok result whose ret is the same JSON-tagged encoding the real
    # _SANDBOX_RUNNER uses, so `_decode_ret` downstream works unmodified.
    "ok": '''
blob = _read_job()
job = blob.get("job") or {}
ns = {"__name__": "leaf_fake_helper_tool"}
exec(compile(job.get("source") or "", job.get("filename") or "<fake>", "exec"), ns)
ret = ns["run"](job.get("intake"), job.get("params"))
_emit({
    "schema": "leaf.e2b.tool-exec-result.v1",
    "receipt": {"passed": True},
    "result": {"ok": True, "ret": _encode_ret(ret)},
    "helper_error": None,
})
''',
    # failed egress-lock receipt: the helper REFUSES to relay any tool output.
    "receipt_fail": '''
_read_job()
_emit({
    "schema": "leaf.e2b.tool-exec-result.v1",
    "receipt": {"passed": False},
    "result": None,
    "helper_error": {"stage": "probe", "type": "EgressLockFailed",
                      "msg": "egress receipt not passed"},
})
''',
    # receipt passed, but the tenant body raised inside the VM.
    "tool_error": '''
_read_job()
_emit({
    "schema": "leaf.e2b.tool-exec-result.v1",
    "receipt": {"passed": True},
    "result": {"error": {"type": "RuntimeError", "msg": "boom from tenant tool"}},
    "helper_error": None,
})
''',
    # the helper crashes / prints something that is not the result protocol at all.
    "garbage": '''
_read_job()
sys.stdout.write("not-json-at-all-{{{")
sys.stdout.flush()
''',
    # never responds within the outer timeout.
    "sleep": '''
import time
_read_job()
time.sleep(5)
''',
    # ignores the tenant source; reports back its OWN env for the 4 probe keys (proves
    # _microvm_env scrubs the broker secrets but passes the E2B key through to the helper).
    "echo_env": '''
_read_job()
seen = {k: os.environ.get(k) for k in
        ("APS_CREDENTIALS_JSON", "LEAF_BROKER_SECRET", "E2B_API_KEY", "SOME_TOKEN")}
_emit({
    "schema": "leaf.e2b.tool-exec-result.v1",
    "receipt": {"passed": True},
    "result": {"ok": True, "ret": {"form": "dict", "obj": {"env_seen": seen}}},
    "helper_error": None,
})
''',
}


@pytest.fixture
def fake_helper(tmp_path):
    """fake_helper(mode) -> Path to a hermetic Python script speaking the result protocol
    for that mode. Wire it in via ``monkeypatch.setattr(tool_loader, "_microvm_cmd", ...)``."""
    def write(mode: str) -> Path:
        p = tmp_path / f"fake_helper_{mode}.py"
        p.write_text(_FAKE_HELPER_PREAMBLE + _FAKE_HELPER_BODIES[mode], encoding="utf-8")
        return p
    return write


def _run_microvm(tool, params, *, fake_helper_path, monkeypatch, intake=None,
                  timeout_s=None, boot_budget_s=None):
    monkeypatch.setenv("LEAF_SANDBOX", "e2b-microvm")
    if timeout_s is not None:
        monkeypatch.setenv("LEAF_SANDBOX_TIMEOUT_S", str(timeout_s))
    if boot_budget_s is not None:
        monkeypatch.setenv("LEAF_SANDBOX_MICROVM_BOOT_BUDGET_S", str(boot_budget_s))
    monkeypatch.setattr(tool_loader, "_microvm_cmd",
                         lambda: [sys.executable, str(fake_helper_path)])
    return tool_loader.run_tool_dynamic(
        tool, intake if intake is not None else {"layers": ["A", "B", "C"]},
        params, aps_live=False, da=None, tenant_id="t")


# --------------------------------------------------------------------------- #
# (1) tri-state tier selection + _sandbox_enabled back-compat
# --------------------------------------------------------------------------- #
def test_sandbox_tier_tri_state_and_enabled_flag_backcompat(monkeypatch):
    monkeypatch.delenv("LEAF_SANDBOX", raising=False)
    assert tool_loader._sandbox_tier() == "off"
    assert tool_loader._sandbox_enabled() is False

    monkeypatch.setenv("LEAF_SANDBOX", "e2b")
    assert tool_loader._sandbox_tier() == "subprocess"
    assert tool_loader._sandbox_enabled() is True

    monkeypatch.setenv("LEAF_SANDBOX", "E2B")  # case-insensitive
    assert tool_loader._sandbox_tier() == "subprocess"
    assert tool_loader._sandbox_enabled() is True

    monkeypatch.setenv("LEAF_SANDBOX", "e2b-microvm")
    assert tool_loader._sandbox_tier() == "microvm"
    assert tool_loader._sandbox_enabled() is False  # NOT the v1 flag

    monkeypatch.setenv("LEAF_SANDBOX", "E2B-MICROVM")  # case-insensitive
    assert tool_loader._sandbox_tier() == "microvm"
    assert tool_loader._sandbox_enabled() is False

    monkeypatch.setenv("LEAF_SANDBOX", "something-else")  # garbage -> off
    assert tool_loader._sandbox_tier() == "off"
    assert tool_loader._sandbox_enabled() is False


# --------------------------------------------------------------------------- #
# (2) microvm ON + fake(ok) -> valid §3 envelope, parity with in-process
# --------------------------------------------------------------------------- #
def test_microvm_ok_round_trips_valid_envelope_and_matches_in_process(
        tenant_repo, fake_helper, monkeypatch):
    tool = tenant_repo("counter", BENIGN_SRC)
    helper = fake_helper("ok")
    env = _run_microvm(tool, {}, fake_helper_path=helper, monkeypatch=monkeypatch)
    assert env["ok"] is True
    assert env["result"] == {"n": 3, "layers": ["A", "B", "C"]}
    assert tool_validate.validate_envelope(env) == []

    monkeypatch.delenv("LEAF_SANDBOX", raising=False)
    in_proc = tool_loader.run_tool_dynamic(
        tool, {"layers": ["A", "B", "C"]}, {}, aps_live=False, da=None, tenant_id="t")
    assert env["result"] == in_proc["result"]


# --------------------------------------------------------------------------- #
# (3) microvm ON -> no in-process exec_module AND no silent subprocess-tier fallback
# --------------------------------------------------------------------------- #
def test_microvm_bypasses_load_module_and_subprocess_tier(tenant_repo, fake_helper, monkeypatch):
    tool = tenant_repo("counter", BENIGN_SRC)
    helper = fake_helper("ok")
    monkeypatch.setattr(
        tool_loader, "_load_module",
        lambda *_a, **_k: pytest.fail("tenant code exec_module ran in-process on the "
                                      "microvm tier"))
    monkeypatch.setattr(
        tool_loader, "_run_in_sandbox",
        lambda *_a, **_k: pytest.fail("the subprocess tier ran instead of the microvm tier"))
    env = _run_microvm(tool, {}, fake_helper_path=helper, monkeypatch=monkeypatch)
    assert env["ok"] is True
    assert env["result"]["n"] == 3


# --------------------------------------------------------------------------- #
# (4) back-compat: LEAF_SANDBOX=e2b still uses the REAL subprocess tier
# --------------------------------------------------------------------------- #
def test_e2b_flag_still_uses_real_subprocess_tier_not_microvm_helper(tenant_repo, monkeypatch):
    tool = tenant_repo("counter", BENIGN_SRC)
    monkeypatch.setattr(
        tool_loader, "_run_in_sandbox_e2b",
        lambda *_a, **_k: pytest.fail("the microvm helper ran for LEAF_SANDBOX=e2b"))
    monkeypatch.setenv("LEAF_SANDBOX", "e2b")
    env = tool_loader.run_tool_dynamic(
        tool, {"layers": ["A", "B", "C"]}, {}, aps_live=False, da=None, tenant_id="t")
    assert env["ok"] is True
    assert env["result"] == {"n": 3, "layers": ["A", "B", "C"]}


# --------------------------------------------------------------------------- #
# (5) large (~1MB) payload round-trips through the stdin channel intact
# --------------------------------------------------------------------------- #
def test_microvm_large_payload_round_trips(tenant_repo, fake_helper, monkeypatch):
    tool = tenant_repo("bigecho", LARGE_ECHO_SRC)
    helper = fake_helper("ok")
    filler = "x" * (1024 * 1024)
    env = _run_microvm(tool, {}, fake_helper_path=helper, monkeypatch=monkeypatch,
                        intake={"filler": filler})
    assert env["ok"] is True
    assert env["result"]["filler_len"] == len(filler)


# --------------------------------------------------------------------------- #
# (6) _microvm_env: broker secrets scrubbed, E2B key passed through to the helper
# --------------------------------------------------------------------------- #
def test_microvm_helper_env_scrubs_secrets_but_passes_e2b_key(tenant_repo, fake_helper,
                                                               monkeypatch):
    monkeypatch.setenv("APS_CREDENTIALS_JSON", "secret-aps")
    monkeypatch.setenv("LEAF_BROKER_SECRET", "secret-broker")
    monkeypatch.setenv("SOME_TOKEN", "secret-token")
    monkeypatch.setenv("E2B_API_KEY", "e2b-key-value")
    tool = tenant_repo("envecho", BENIGN_SRC)  # source unused by echo_env mode
    helper = fake_helper("echo_env")
    env = _run_microvm(tool, {}, fake_helper_path=helper, monkeypatch=monkeypatch)
    assert env["ok"] is True
    seen = env["result"]["env_seen"]
    assert seen["APS_CREDENTIALS_JSON"] is None
    assert seen["LEAF_BROKER_SECRET"] is None
    assert seen["SOME_TOKEN"] is None
    assert seen["E2B_API_KEY"] == "e2b-key-value"


# --------------------------------------------------------------------------- #
# (7) failed egress receipt -> INTERNAL, no tool result leaks through
# --------------------------------------------------------------------------- #
def test_microvm_receipt_fail_maps_to_internal(tenant_repo, fake_helper, monkeypatch):
    tool = tenant_repo("counter", BENIGN_SRC)
    helper = fake_helper("receipt_fail")
    env = _run_microvm(tool, {}, fake_helper_path=helper, monkeypatch=monkeypatch)
    assert env["ok"] is False
    assert env["error"]["error_code"] == "INTERNAL"
    msg = env["error"]["message"].lower()
    assert any(kw in msg for kw in ("egress", "receipt", "sandbox"))
    assert env["result"] is None


# --------------------------------------------------------------------------- #
# (8) tenant exception inside the VM -> INTERNAL "raised ..." (parity with subprocess tier)
# --------------------------------------------------------------------------- #
def test_microvm_tool_error_maps_to_internal_raised(tenant_repo, fake_helper, monkeypatch):
    tool = tenant_repo("counter", BENIGN_SRC)
    helper = fake_helper("tool_error")
    env = _run_microvm(tool, {}, fake_helper_path=helper, monkeypatch=monkeypatch)
    assert env["ok"] is False
    assert env["error"]["error_code"] == "INTERNAL"
    assert "raised" in env["error"]["message"]
    assert "boom from tenant tool" in env["error"]["message"]


# --------------------------------------------------------------------------- #
# (9) non-JSON helper output -> INTERNAL
# --------------------------------------------------------------------------- #
def test_microvm_garbage_output_maps_to_internal(tenant_repo, fake_helper, monkeypatch):
    tool = tenant_repo("counter", BENIGN_SRC)
    helper = fake_helper("garbage")
    env = _run_microvm(tool, {}, fake_helper_path=helper, monkeypatch=monkeypatch)
    assert env["ok"] is False
    assert env["error"]["error_code"] == "INTERNAL"
    msg = env["error"]["message"].lower()
    assert "json" in msg or "output" in msg


# --------------------------------------------------------------------------- #
# (10) node missing -> INTERNAL, fail-closed (never silently downgrades tier)
# --------------------------------------------------------------------------- #
def test_microvm_node_missing_maps_to_internal(tenant_repo, monkeypatch):
    tool = tenant_repo("counter", BENIGN_SRC)
    monkeypatch.setenv("LEAF_SANDBOX", "e2b-microvm")
    monkeypatch.setattr(tool_loader, "_microvm_cmd", lambda: None)
    env = tool_loader.run_tool_dynamic(
        tool, {"layers": ["A", "B", "C"]}, {}, aps_live=False, da=None, tenant_id="t")
    assert env["ok"] is False
    assert env["error"]["error_code"] == "INTERNAL"
    assert "node" in env["error"]["message"].lower()


# --------------------------------------------------------------------------- #
# (11) helper hangs past the outer (timeout + boot budget) window -> INTERNAL "timed out"
# --------------------------------------------------------------------------- #
def test_microvm_timeout_maps_to_internal(tenant_repo, fake_helper, monkeypatch):
    tool = tenant_repo("counter", BENIGN_SRC)
    helper = fake_helper("sleep")
    env = _run_microvm(tool, {}, fake_helper_path=helper, monkeypatch=monkeypatch,
                        timeout_s=1, boot_budget_s=1)
    assert env["ok"] is False
    assert env["error"]["error_code"] == "INTERNAL"
    assert "timed out" in env["error"]["message"].lower()
