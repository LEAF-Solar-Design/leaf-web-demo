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

import hashlib
import json
import sys
import time
from functools import partial
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parent.parent
ROOT = SERVER_DIR.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import tool_loader  # noqa: E402
import tool_validate  # noqa: E402
import write_loop  # noqa: E402


def test_pinned_e2b_template_has_a_reproducible_nonroot_python_base():
    dockerfile = (ROOT / "e2b.Dockerfile").read_text(encoding="utf-8")
    assert (
        "FROM python:3.12-slim@"
        "sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de"
    ) in dockerfile
    assert "useradd --create-home --uid 1000 --gid 1000" in dockerfile
    assert 'test "$(id -u user)" = "1000"' in dockerfile
    assert "\nUSER user\n" in dockerfile
    assert "\nWORKDIR /home/user\n" in dockerfile
    assert tool_loader._SANDBOX_TEMPLATE_VERSION == "leaf-python-2026-07-29-v2"
    assert tool_loader._SANDBOX_TEMPLATE_ID == "r0kto3ypd1sgylx4tkz4"
    assert tool_loader._SANDBOX_TEMPLATE_BUILD_ID == (
        "273367ae-6a5b-47da-ba46-7782c2fa5d6b"
    )


def test_microvm_runner_irreversibly_caps_limits_without_raising_platform_hard_limits():
    runner = tool_loader._SANDBOX_RUNNER
    assert "soft, hard = resource.getrlimit(kind)" in runner
    assert "if soft != resource.RLIM_INFINITY" in runner
    assert "target = min(bounds)" in runner
    assert "resource.setrlimit(kind, (target, target))" in runner
    assert "resource.setrlimit(resource.RLIMIT_CPU, (30, 30))" not in runner
    wrapper = tool_loader._SANDBOX_WRAPPER
    assert "soft, hard = resource.getrlimit(resource.RLIMIT_FSIZE)" in wrapper
    assert "resource.setrlimit(resource.RLIMIT_FSIZE, (target, target))" in wrapper
    assert "resource.setrlimit(resource.RLIMIT_FSIZE, (_LIMIT, _LIMIT))" not in wrapper
    assert "if os.name == \"posix\" and require_network_namespace:" in wrapper
    assert 'child_cmd = ["unshare", "-Urn", "--", *child_cmd]' in wrapper


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
import sys, json, os, hashlib

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

def _canonical_bytes(value):
    def encode(item):
        if item is None:
            return "null"
        if isinstance(item, bool):
            return "true" if item else "false"
        if isinstance(item, str):
            return json.dumps(item, ensure_ascii=False, separators=(",", ":"))
        if isinstance(item, (int, float)):
            number = float(item)
            if number == 0:
                number = 0.0
            mantissa, exponent = format(number, ".16e").split("e")
            return f"{mantissa}e{int(exponent):+d}"
        if isinstance(item, list):
            return "[" + ",".join(encode(entry) for entry in item) + "]"
        if isinstance(item, dict):
            keys = sorted(item, key=lambda key: key.encode("utf-8"))
            return "{" + ",".join(
                f"{json.dumps(key, ensure_ascii=False)}:{encode(item[key])}"
                for key in keys
            ) + "}"
        raise TypeError(type(item).__name__)
    return encode(value).encode("utf-8")

def _strict_receipt(blob, result):
    audit = blob.get("audit") or {}
    broker_host = blob.get("broker_host", "httpbingo.org")
    denied = [
        *(blob.get("denied_targets") or []),
        "https://" + broker_host + "/",
    ]
    return {
        "passed": True,
        "configuredDenyAll": True,
        "configuredTemplate": True,
        "configuredBrokerOnly": True,
        "configuredNoPublicTraffic": True,
        "everyDeniedProbeBlocked": True,
        "deniedProbes": {target: {"blocked": True} for target in denied},
        "platformMetadata": {
            "target": blob.get("platform_metadata_target"),
            "credential_material_present": False,
            "attempts": {
                label: {"blocked": False, "status": 401}
                for label in (
                    "no_auth_get",
                    "aws_token_put",
                    "aws_invalid_token_get",
                    "gcp_flavor_get",
                    "e2b_invalid_access_token_get",
                    "invalid_bearer_get",
                )
            },
        },
        "platformMetadataSafe": True,
        "tenantNetworkNamespace": {
            "command_ok": True,
            "blocked": {
                target: {"blocked": True}
                for target in [
                    *(blob.get("denied_targets") or []),
                    "https://" + broker_host + "/",
                    blob.get("platform_metadata_target"),
                ]
            },
        },
        "tenantNetworkNamespaceIsolated": True,
        "brokerReached": None,
        "boundary": "tool",
        "network": {
            "allowOut": None,
            "denyOut": ["0.0.0.0/0"],
            "allowPublicTraffic": False,
        },
        "tenantHash": audit.get("tenant_hash"),
        "sourceHash": audit.get("source_hash"),
        "inputHash": audit.get("input_hash"),
        "jobHash": hashlib.sha256(_canonical_bytes(blob.get("job"))).hexdigest(),
        "templateVersion": audit.get("template_version"),
        "templateId": audit.get("template_id"),
        "templateBuildId": audit.get("template_build_id"),
        "policyVersion": audit.get("policy_version"),
        "startedAt": "2026-07-23T00:00:00Z",
        "stoppedAt": "2026-07-23T00:00:01Z",
        "resourceUse": {"wallMs": 1000},
        "resultHash": hashlib.sha256(_canonical_bytes(result)).hexdigest(),
    }

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
result = {"ok": True, "ret": _encode_ret(ret)}
_emit({
    "schema": "leaf.e2b.tool-exec-result.v1",
    "receipt": _strict_receipt(blob, result),
    "result": result,
    "helper_error": None,
})
''',
    "strict_ok": '''
blob = _read_job()
job = blob.get("job") or {}
audit = blob.get("audit") or {}
import hashlib
ns = {"__name__": "leaf_fake_helper_tool"}
exec(compile(job.get("source") or "", job.get("filename") or "<fake>", "exec"), ns)
ret = ns["run"](job.get("intake"), job.get("params"))
result = {"ok": True, "ret": _encode_ret(ret)}
_emit({
    "schema": "leaf.e2b.tool-exec-result.v1",
    "receipt": _strict_receipt(blob, result),
    "result": result,
    "helper_error": None,
})
''',
    "strict_missing_no_egress": '''
blob = _read_job()
job = blob.get("job") or {}
audit = blob.get("audit") or {}
import hashlib
ns = {"__name__": "leaf_fake_helper_tool"}
exec(compile(job.get("source") or "", job.get("filename") or "<fake>", "exec"), ns)
ret = ns["run"](job.get("intake"), job.get("params"))
result = {"ok": True, "ret": _encode_ret(ret)}
_emit({
    "schema": "leaf.e2b.tool-exec-result.v1",
    "receipt": {
        "passed": True,
        "tenantHash": audit.get("tenant_hash"),
        "sourceHash": audit.get("source_hash"),
        "inputHash": audit.get("input_hash"),
        "jobHash": hashlib.sha256(_canonical_bytes(job)).hexdigest(),
        "templateVersion": audit.get("template_version"),
        "policyVersion": audit.get("policy_version"),
        "startedAt": "2026-07-23T00:00:00Z",
        "stoppedAt": "2026-07-23T00:00:01Z",
        "resourceUse": {"wallMs": 1000},
        "resultHash": hashlib.sha256(_canonical_bytes(result)).hexdigest(),
    },
    "result": result,
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
blob = _read_job()
result = {"error": {"type": "RuntimeError", "msg": "boom from tenant tool"}}
_emit({
    "schema": "leaf.e2b.tool-exec-result.v1",
    "receipt": _strict_receipt(blob, result),
    "result": result,
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
blob = _read_job()
seen = {k: os.environ.get(k) for k in
        ("APS_CREDENTIALS_JSON", "LEAF_BROKER_SECRET", "E2B_API_KEY", "SOME_TOKEN")}
result = {"ok": True, "ret": {"form": "dict", "obj": {"env_seen": seen}}}
_emit({
    "schema": "leaf.e2b.tool-exec-result.v1",
    "receipt": _strict_receipt(blob, result),
    "result": result,
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
                  timeout_s=None, boot_budget_s=None, probe_budget_s=None):
    monkeypatch.setenv("LEAF_SANDBOX", "e2b-microvm")
    if timeout_s is not None:
        monkeypatch.setenv("LEAF_SANDBOX_TIMEOUT_S", str(timeout_s))
    if boot_budget_s is not None:
        monkeypatch.setenv("LEAF_SANDBOX_MICROVM_BOOT_BUDGET_S", str(boot_budget_s))
    if probe_budget_s is not None:
        monkeypatch.setenv("LEAF_SANDBOX_MICROVM_PROBE_BUDGET_S", str(probe_budget_s))
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

    monkeypatch.setenv("LEAF_SANDBOX", "E2B")
    assert tool_loader._sandbox_tier() == "subprocess"
    assert tool_loader._sandbox_enabled() is True

    monkeypatch.setenv("LEAF_SANDBOX", "e2b-microvm")
    assert tool_loader._sandbox_tier() == "microvm"
    assert tool_loader._sandbox_enabled() is False

    monkeypatch.setenv("LEAF_SANDBOX", "something-else")
    assert tool_loader._sandbox_tier() == "off"
    assert tool_loader._sandbox_enabled() is False


def test_tool_provider_is_separate_and_fail_closed(monkeypatch):
    monkeypatch.setenv("LEAF_SANDBOX", "e2b")
    monkeypatch.setenv("LEAF_TOOL_SANDBOX_PROVIDER", "e2b")
    assert tool_loader._sandbox_tier() == "microvm"
    monkeypatch.setenv("LEAF_TOOL_SANDBOX_PROVIDER", "unknown")
    assert tool_loader._sandbox_tier() == "invalid"


def test_canonical_json_bytes_matches_js_unicode_and_key_order():
    first = {"z": "雪", "tiny": 1e-5, "a": {"β": 1, "a": "é"}, "n": 1.0}
    reordered = {"n": 1, "a": {"a": "é", "β": 1}, "tiny": 1e-5, "z": "雪"}
    expected = (
        '{"a":{"a":"é","β":1.0000000000000000e+0},'
        '"n":1.0000000000000000e+0,'
        '"tiny":1.0000000000000001e-5,"z":"雪"}'
    ).encode("utf-8")
    assert tool_loader.canonical_json_bytes(first) == expected
    assert tool_loader.canonical_json_bytes(reordered) == expected
    assert hashlib.sha256(expected).hexdigest() == (
        "937d081cc77301a198516f5a9e8d728d060a52ee3a748e0bd78a589280b574b3"
    )


def test_tool_provider_requires_complete_matching_audit_receipt(
        tenant_repo, fake_helper, monkeypatch):
    tool = tenant_repo("counter", BENIGN_SRC)
    monkeypatch.setenv("LEAF_TOOL_SANDBOX_PROVIDER", "e2b")
    monkeypatch.setattr(
        tool_loader, "_microvm_cmd",
        lambda: [sys.executable, str(fake_helper("strict_ok"))],
    )
    env = tool_loader.run_tool_dynamic(
        tool, {"layers": ["A", "B", "C"]}, {}, aps_live=False, da=None,
        tenant_id="tenant-a")
    assert env["ok"] is True
    provenance = env["execution_provenance"]
    assert provenance["contract"] == "leaf.tool-execution.v1"
    assert provenance["provider"] == "e2b"
    assert provenance["isolation"] == "microvm"
    assert provenance["passed"] is True
    assert provenance["tenant_hash"] == hashlib.sha256(b"tenant-a").hexdigest()
    assert provenance["source_sha256"] == hashlib.sha256(
        BENIGN_SRC.encode("utf-8")).hexdigest()
    assert provenance["input_sha256"] == hashlib.sha256(json.dumps(
        {"intake": {"layers": ["A", "B", "C"]}, "params": {}},
        sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    assert provenance["result_sha256"]
    assert provenance["policy_version"] == "leaf.sandbox-policy.v1"
    assert provenance["resource_use"] == {"wallMs": 1000}

    monkeypatch.setattr(
        tool_loader, "_microvm_cmd",
        lambda: [sys.executable, str(fake_helper("strict_missing_no_egress"))],
    )
    refused_no_egress = tool_loader.run_tool_dynamic(
        tool, {"layers": ["A"]}, {}, aps_live=False, da=None,
        tenant_id="tenant-a")
    assert refused_no_egress["ok"] is False
    assert "audit receipt incomplete" in refused_no_egress["error"]["message"]

    bad_public_helper = fake_helper("strict_ok")
    bad_public_helper.write_text(
        bad_public_helper.read_text(encoding="utf-8").replace(
            '"configuredNoPublicTraffic": True',
            '"configuredNoPublicTraffic": False',
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        tool_loader, "_microvm_cmd",
        lambda: [sys.executable, str(bad_public_helper)],
    )
    refused_public = tool_loader.run_tool_dynamic(
        tool, {"layers": ["A"]}, {}, aps_live=False, da=None,
        tenant_id="tenant-a")
    assert refused_public["ok"] is False
    assert "no-egress receipt incomplete" in refused_public["error"]["message"]


def test_staged_source_receipt_binds_exact_submitted_bytes(
        tenant_repo, fake_helper, monkeypatch):
    tool = tenant_repo("counter", "def run(intake, params):\n    return ({'old': True}, None)\n")
    staged = BENIGN_SRC.replace("'n': len(layers)", "'n': len(layers) + 7")
    monkeypatch.setenv("LEAF_TOOL_SANDBOX_PROVIDER", "e2b")
    monkeypatch.setattr(
        tool_loader, "_microvm_cmd",
        lambda: [sys.executable, str(fake_helper("strict_ok"))],
    )

    env = tool_loader.run_tool_dynamic(
        tool, {"layers": ["A", "B"]}, {}, aps_live=False, da=None,
        tenant_id="tenant-a", test_source=staged,
    )

    assert env["ok"] is True
    assert env["result"]["n"] == 9
    assert env["execution_provenance"]["source_sha256"] == hashlib.sha256(
        staged.encode("utf-8")
    ).hexdigest()
    assert env["execution_provenance"]["tenant_hash"] == hashlib.sha256(
        b"tenant-a"
    ).hexdigest()


def test_staged_write_receipt_binds_request_tenant(
        tenant_repo, fake_helper, monkeypatch, tmp_path):
    tool = tenant_repo("counter", "def run(intake, params):\n    return ({'old': True}, None)\n")
    staged = BENIGN_SRC.replace("'n': len(layers)", "'n': len(layers) + 7")
    monkeypatch.setenv("LEAF_TOOL_SANDBOX_PROVIDER", "e2b")
    monkeypatch.setenv("LEAF_STORE_DIR", str(tmp_path / "drawings"))
    monkeypatch.setattr(
        tool_loader, "_microvm_cmd",
        lambda: [sys.executable, str(fake_helper("strict_ok"))],
    )

    env, status = write_loop.run_write_mock(
        tool,
        {"drawing_id": "demo", "dry_run": True},
        "tenant-a",
        backend=write_loop.default_backend(),
        t0=time.perf_counter(),
        run_tool_dynamic_fn=partial(tool_loader.run_tool_dynamic, test_source=staged),
    )

    assert status == 200
    assert env["result"]["dry_run"] is True
    provenance = env["execution_provenance"]
    assert provenance["tenant_hash"] == hashlib.sha256(b"tenant-a").hexdigest()
    assert provenance["tenant_hash"] != hashlib.sha256(b"demo-tenant").hexdigest()
    assert provenance["source_sha256"] == hashlib.sha256(staged.encode("utf-8")).hexdigest()


def test_microvm_refuses_input_job_and_result_replay_tampering(
        tenant_repo, fake_helper, monkeypatch):
    tool = tenant_repo("counter", BENIGN_SRC)
    monkeypatch.setenv("LEAF_SANDBOX", "e2b-microvm")
    monkeypatch.delenv("LEAF_TOOL_SANDBOX_PROVIDER", raising=False)

    input_tamper = fake_helper("strict_ok")
    input_tamper.write_text(
        input_tamper.read_text(encoding="utf-8").replace(
            '"inputHash": audit.get("input_hash")',
            '"inputHash": "0" * 64',
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        tool_loader, "_microvm_cmd",
        lambda: [sys.executable, str(input_tamper)],
    )
    refused_input = tool_loader.run_tool_dynamic(
        tool, {"layers": ["A"]}, {}, aps_live=False, da=None,
        tenant_id="tenant-a")
    assert refused_input["ok"] is False
    assert "audit receipt mismatch" in refused_input["error"]["message"]

    job_tamper = fake_helper("strict_ok")
    job_tamper.write_text(
        job_tamper.read_text(encoding="utf-8").replace(
            '"jobHash": hashlib.sha256(_canonical_bytes(blob.get("job"))).hexdigest()',
            '"jobHash": "0" * 64',
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        tool_loader, "_microvm_cmd",
        lambda: [sys.executable, str(job_tamper)],
    )
    refused_job = tool_loader.run_tool_dynamic(
        tool, {"layers": ["A"]}, {}, aps_live=False, da=None,
        tenant_id="tenant-a")
    assert refused_job["ok"] is False
    assert "audit receipt mismatch" in refused_job["error"]["message"]

    result_tamper = fake_helper("strict_ok")
    result_tamper.write_text(
        result_tamper.read_text(encoding="utf-8").replace(
            '"resultHash": hashlib.sha256(_canonical_bytes(result)).hexdigest()',
            '"resultHash": "0" * 64',
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        tool_loader, "_microvm_cmd",
        lambda: [sys.executable, str(result_tamper)],
    )
    refused_result = tool_loader.run_tool_dynamic(
        tool, {"layers": ["A"]}, {}, aps_live=False, da=None,
        tenant_id="tenant-a")
    assert refused_result["ok"] is False
    assert "result hash mismatch" in refused_result["error"]["message"]

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
                        timeout_s=1, boot_budget_s=1, probe_budget_s=1)
    assert env["ok"] is False
    assert env["error"]["error_code"] == "INTERNAL"
    assert "timed out" in env["error"]["message"].lower()
