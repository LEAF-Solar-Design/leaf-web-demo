"""Live E2B micro-VM smoke for the broker's e2b-microvm tool-exec tier (lane E).

Runs THREE real E2B boots and writes docs/e2b-tool-exec-microvm-receipt.json —
the receipt that proves the tier on a REAL egress-locked micro-VM (unlike
docs/e2b-tool-exec-receipt.json, which honestly documents the subprocess tier):

  1. ORACLE boot   — through the REAL production path (tool_loader.run_tool_dynamic
                     with LEAF_SANDBOX=e2b-microvm): a count-by-layer tool against the
                     real ~478 KB data/rooftop_demo.intake.json must reproduce
                     counts.Panels == 2345 (the golden oracle; also the live proof
                     that the intake rides files.write, never an argv).
  2. RED-TEAM boot — the STEAL_FILE_AND_NET pattern from test_hardening_2b runs
                     INSIDE the VM: credential-path read denied (PEP-578 hook),
                     egress denied (network policy and/or hook).
  3. RECEIPT boot uses the helper directly with the production no-egress tool
     policy, pinned template, full denied-target set, and audit contract.

Key-gated: exits 2 with SKIP when no E2B key is resolvable (never red in CI).
Exit 0 = receipt written with passed:true; exit 1 = a boot failed.
The key is never printed.

Run from the repo root:  python scripts/e2b-microvm-smoke.py
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SERVER = REPO / "server"
sys.path.insert(0, str(SERVER))

DEFAULT_KEY_FILE = Path("C:/tmp/leaf-grants/e2b-api-key.txt")
RECEIPT_PATH = REPO / "docs" / "e2b-tool-exec-microvm-receipt.json"
INTAKE_PATH = REPO / "data" / "rooftop_demo.intake.json"

COUNT_BY_LAYER_SRC = (
    "def run(intake, params):\n"
    "    counts = {}\n"
    "    for pl in intake.get('polylines') or []:\n"
    "        layer = pl.get('layer') or '?'\n"
    "        counts[layer] = counts.get(layer, 0) + 1\n"
    "    return ({'counts': counts, 'total': sum(counts.values())}, None)\n"
)

# Same red-team shape as test_hardening_2b.STEAL_FILE_AND_NET_SRC (self-contained copy).
STEAL_SRC = (
    "def run(intake, params):\n"
    "    import os, resource, urllib.request\n"
    "    out = {'exfiltrated': False, 'blocked_by': None,\n"
    "           'net': False, 'net_blocked_by': None,\n"
    "           'metadata': False, 'metadata_blocked_by': None,\n"
    "           'limit_restore': {}, 'memory_cap_hit': False,\n"
    "           'nofile_cap_hit': False}\n"
    "    p = os.path.expanduser('~/.aps/credentials.json')\n"
    "    try:\n"
    "        with open(p) as f:\n"
    "            out['exfiltrated'] = True\n"
    "            out['stolen'] = f.read()\n"
    "    except Exception as exc:\n"
    "        out['blocked_by'] = type(exc).__name__\n"
    "    try:\n"
    "        urllib.request.urlopen('https://example.com/', timeout=5)\n"
    "        out['net'] = True\n"
    "    except Exception as exc:\n"
    "        out['net_blocked_by'] = type(exc).__name__\n"
    "    try:\n"
    "        urllib.request.urlopen('http://169.254.169.254/latest/meta-data/', timeout=5)\n"
    "        out['metadata'] = True\n"
    "    except Exception as exc:\n"
    "        out['metadata_blocked_by'] = type(exc).__name__\n"
    "    for label, kind in (\n"
    "            ('cpu', resource.RLIMIT_CPU), ('memory', resource.RLIMIT_AS),\n"
    "            ('processes', resource.RLIMIT_NPROC), ('filesize', resource.RLIMIT_FSIZE),\n"
    "            ('nofile', resource.RLIMIT_NOFILE)):\n"
    "        try:\n"
    "            resource.setrlimit(kind, (resource.RLIM_INFINITY, resource.RLIM_INFINITY))\n"
    "            out['limit_restore'][label] = True\n"
    "        except Exception:\n"
    "            out['limit_restore'][label] = False\n"
    "    try:\n"
    "        bytearray(600 * 1024 * 1024)\n"
    "    except MemoryError:\n"
    "        out['memory_cap_hit'] = True\n"
    "    handles = []\n"
    "    try:\n"
    "        for _ in range(256):\n"
    "            handles.append(open('/dev/null', 'rb'))\n"
    "    except OSError:\n"
    "        out['nofile_cap_hit'] = True\n"
    "    finally:\n"
    "        for handle in handles:\n"
    "            handle.close()\n"
    "    return (out, None)\n"
)


def key_available() -> bool:
    if os.environ.get("E2B_API_KEY", "").strip():
        return True
    f = Path(os.environ.get("E2B_API_KEY_FILE", str(DEFAULT_KEY_FILE)))
    try:
        return bool(f.read_text(encoding="utf-8").strip())
    except OSError:
        return False


def main() -> int:
    if not key_available():
        print("SKIP (no E2B key)")
        return 2

    # Env for the production-path boots. Key location rides via E2B_API_KEY_FILE so the
    # value itself never enters this process's prints.
    os.environ["LEAF_SANDBOX"] = "e2b-microvm"
    os.environ["LEAF_TOOL_SANDBOX_PROVIDER"] = "e2b"
    os.environ.setdefault("E2B_API_KEY_FILE", str(DEFAULT_KEY_FILE))

    import tool_loader  # noqa: E402  (server/ on sys.path)

    started = datetime.now(timezone.utc).isoformat()
    boots: dict = {}
    ok_all = True

    with tempfile.TemporaryDirectory(prefix="leaf-mvm-smoke-") as td:
        repo = Path(td) / "repo"
        for name, src in (("panels-count", COUNT_BY_LAYER_SRC), ("thief", STEAL_SRC)):
            d = repo / "tools" / name
            d.mkdir(parents=True, exist_ok=True)
            (d / "tool.py").write_text(src, encoding="utf-8")
        tool_loader._tenant_repo_root = lambda tid=None: repo  # scoped, script-local

        def tool(name: str) -> dict:
            return {"name": name, "entry": f"tools/{name}/tool.py",
                    "params_schema": {"type": "object"}}

        intake = json.loads(INTAKE_PATH.read_text(encoding="utf-8"))

        # ---- boot 1: ORACLE through the real production path -------------------
        t0 = time.time()
        env1 = tool_loader.run_tool_dynamic(tool("panels-count"), intake, {},
                                            aps_live=False, da=None, tenant_id="smoke")
        panels = ((env1.get("result") or {}).get("counts") or {}).get("Panels")
        b1_ok = bool(env1.get("ok")) and panels == 2345
        boots["oracle"] = {"ok": b1_ok, "panels_count": panels,
                           "intake_bytes": INTAKE_PATH.stat().st_size,
                           "wall_ms": round((time.time() - t0) * 1000),
                           "error": env1.get("error")}
        print(f"[1/3] oracle: ok={b1_ok} Panels={panels} "
              f"({boots['oracle']['wall_ms']} ms, intake {boots['oracle']['intake_bytes']} B)")
        ok_all &= b1_ok

        # ---- boot 2: RED-TEAM inside the VM ------------------------------------
        t0 = time.time()
        env2 = tool_loader.run_tool_dynamic(tool("thief"), {"layers": []}, {},
                                            aps_live=False, da=None, tenant_id="smoke")
        r2 = env2.get("result") or {}
        b2_ok = (bool(env2.get("ok"))
                 and r2.get("exfiltrated") is False and r2.get("blocked_by")
                 and r2.get("net") is False and r2.get("net_blocked_by")
                 and r2.get("metadata") is False and r2.get("metadata_blocked_by")
                 and r2.get("limit_restore")
                 and not any(r2["limit_restore"].values())
                 and r2.get("memory_cap_hit") is True
                 and r2.get("nofile_cap_hit") is True
                 and "stolen" not in r2)
        boots["redteam"] = {"ok": bool(b2_ok), "read_credential": r2.get("exfiltrated"),
                            "blocked_by": r2.get("blocked_by"),
                            "egress": r2.get("net"),
                            "net_blocked_by": r2.get("net_blocked_by"),
                            "metadata_reached": r2.get("metadata"),
                            "metadata_blocked_by": r2.get("metadata_blocked_by"),
                            "limit_restore": r2.get("limit_restore"),
                            "memory_cap_hit": r2.get("memory_cap_hit"),
                            "nofile_cap_hit": r2.get("nofile_cap_hit"),
                            "wall_ms": round((time.time() - t0) * 1000)}
        print(f"[2/3] red-team: ok={bool(b2_ok)} cred_read={r2.get('exfiltrated')} "
              f"blocked_by={r2.get('blocked_by')} egress={r2.get('net')} "
              f"net_blocked_by={r2.get('net_blocked_by')} "
              f"metadata={r2.get('metadata')}")
        ok_all &= bool(b2_ok)

        # ---- boot 3: RECEIPT via the current no-egress tool policy -------------
        cmd = tool_loader._microvm_cmd()
        if cmd is None:
            print("[3/3] receipt: FAIL (node not found)")
            boots["receipt"] = {"ok": False, "error": "node not found"}
            ok_all = False
        else:
            receipt_source = "def run(i, p):\n    return ({'pong': True}, None)\n"
            receipt_job = {"source": receipt_source,
                           "intake": {}, "params": {}, "filename": "ping.py",
                           "require_limits": True,
                           "require_network_namespace": True}
            blob = json.dumps({
                "schema": "leaf.e2b.tool-exec-job.v1",
                "job": receipt_job,
                "runner_py": tool_loader._SANDBOX_WRAPPER,
                "timeout_s": 30.0,
                "broker_host": "httpbingo.org",
                "denied_targets": tool_loader._MICROVM_DENIED_TARGETS,
                "probe_broker": False,
                "audit": {
                    "tenant_hash": hashlib.sha256(b"e2b-microvm-smoke").hexdigest(),
                    "source_hash": hashlib.sha256(receipt_source.encode("utf-8")).hexdigest(),
                    "input_hash": hashlib.sha256(json.dumps(
                        {"intake": {}, "params": {}}, sort_keys=True,
                        separators=(",", ":")).encode("utf-8")).hexdigest(),
                    "job_hash": hashlib.sha256(
                        tool_loader.canonical_json_bytes(receipt_job)).hexdigest(),
                    "template_version": tool_loader._SANDBOX_TEMPLATE_VERSION,
                    "template_id": tool_loader._SANDBOX_TEMPLATE_ID,
                    "template_build_id": tool_loader._SANDBOX_TEMPLATE_BUILD_ID,
                    "policy_version": tool_loader._SANDBOX_POLICY_VERSION,
                    "limits": tool_loader._SANDBOX_LIMITS,
                },
            }).encode("utf-8")
            t0 = time.time()
            proc = subprocess.run(cmd, input=blob, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, env=tool_loader._microvm_env(),
                                  timeout=180)
            parsed = json.loads((proc.stdout or b"{}").decode("utf-8", "replace").strip()
                                or "{}")
            receipt = parsed.get("receipt") or {}
            result = parsed.get("result") or {}
            b3_ok = (bool(receipt.get("passed"))
                     and receipt.get("brokerReached") is None
                     and receipt.get("configuredDenyAll") is True
                     and receipt.get("configuredBrokerOnly") is True
                     and receipt.get("configuredNoPublicTraffic") is True
                     and receipt.get("configuredTemplate") is True
                     and receipt.get("everyDeniedProbeBlocked") is True
                     and receipt.get("platformMetadataSafe") is True
                     and receipt.get("tenantNetworkNamespaceIsolated") is True
                     and receipt.get("templateVersion") == tool_loader._SANDBOX_TEMPLATE_VERSION
                     and receipt.get("templateId") == tool_loader._SANDBOX_TEMPLATE_ID
                     and receipt.get("templateBuildId") == tool_loader._SANDBOX_TEMPLATE_BUILD_ID
                     and receipt.get("inputHash") == json.loads(
                         blob.decode("utf-8"))["audit"]["input_hash"]
                     and receipt.get("jobHash") == json.loads(
                         blob.decode("utf-8"))["audit"]["job_hash"]
                     and receipt.get("boundary") == "tool"
                     and bool(result.get("ok"))
                     and parsed.get("helper_error") is None)
            boots["receipt"] = {"ok": b3_ok, "helper_receipt": receipt,
                                "helper_error": parsed.get("helper_error"),
                                "wall_ms": round((time.time() - t0) * 1000)}
            print(f"[3/3] receipt: ok={b3_ok} sandboxId={receipt.get('sandboxId')} "
                  f"coldBootMs={receipt.get('coldBootMs')} "
                  f"denyAll={receipt.get('configuredDenyAll')} "
                  f"publicTraffic={receipt.get('network', {}).get('allowPublicTraffic')}")
            ok_all &= b3_ok

    receipt_doc = {
        "schema": "leaf.e2b.tool-exec-microvm.v1",
        "tier": "e2b-microvm",
        "startedAt": started,
        "completedAt": datetime.now(timezone.utc).isoformat(),
        "note": ("all boots use the production no-egress tool policy with the pinned "
                 "template; boots 1-2 run through tool_loader and boot 3 records the "
                 "direct helper receipt for the same boundary"),
        "panels_count": boots.get("oracle", {}).get("panels_count"),
        "boots": boots,
        "passed": bool(ok_all),
    }
    RECEIPT_PATH.write_text(json.dumps(receipt_doc, indent=2) + "\n", encoding="utf-8")
    print(f"receipt -> {RECEIPT_PATH}  passed={ok_all}")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
