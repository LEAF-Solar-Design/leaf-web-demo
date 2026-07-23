"""Containerized smoke for the credential-broker keystone (census #4, §8 FROZEN).

Host-side orchestrator for the compose stack (docker-compose.yml). Proves, on
the real containers, the properties §8 freezes:

  1. all four services come up healthy (build → up -d → healthchecks);
  2. the broker publishes NO host port (internal-network only);
  3. the X-Broker-Secret hop is REAL: this smoke sets LEAF_BROKER_SECRET for
     the stack, a correct-secret call works, a wrong-secret call is 401;
  4. one tenant-facing async run (§7 spine) crosses app → broker and appends
     exactly ONE schema-valid ledger line;
  5. the kill-switch cycle: disable → run denied (TENANT_DISABLED, one more
     ledger line) → enable → run ok.

Run:  python scripts/broker-container-smoke.py [--no-build] [--keep]
Final stdout line: `BROKER-SMOKE: PASS` (exit 0) or `BROKER-SMOKE: FAIL <why>`
(exit 1). Requires Docker with compose v2; tears the stack down (down -v)
unless --keep.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import sys
import time
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
# In-container base URL (every HTTP leg runs via `compose exec app` — see
# app_json's docstring for why the host's published port is not used).
APP = "http://localhost:8130"
FROZEN_KEYS = {"ts", "tenant_id", "tool", "engine_op", "aps_endpoint",
               "aps_live", "engine_seconds", "usd_est", "status"}
SECRET = secrets.token_hex(16)


def fail(why: str) -> None:
    print(f"BROKER-SMOKE: FAIL {why}")
    sys.exit(1)


def compose(*args: str, check: bool = True, timeout: int = 900) -> subprocess.CompletedProcess:
    env = {**os.environ, "LEAF_BROKER_SECRET": SECRET}
    proc = subprocess.run(["docker", "compose", *args], cwd=str(REPO), env=env,
                          capture_output=True, text=True, timeout=timeout,
                          encoding="utf-8", errors="replace")
    if check and proc.returncode != 0:
        fail(f"docker compose {' '.join(args)} rc={proc.returncode}: "
             f"{(proc.stderr or proc.stdout)[-400:]}")
    return proc


def wait_all_healthy(deadline_s: int = 300) -> None:
    t0 = time.monotonic()
    while time.monotonic() - t0 < deadline_s:
        proc = compose("ps", "--format", "json", check=False)
        rows = [json.loads(l) for l in proc.stdout.splitlines() if l.strip().startswith("{")]
        states = {r.get("Service"): (r.get("Health") or r.get("State", "")) for r in rows}
        want = {"broker", "harness", "app", "web"}
        if want <= set(states) and all("healthy" in str(states[s]) for s in want):
            print(f"  all healthy in {time.monotonic() - t0:.0f}s: {states}")
            return
        time.sleep(5)
    fail(f"services not healthy within {deadline_s}s: {states}")


def broker_exec_py(code: str) -> subprocess.CompletedProcess:
    """Run python inside the APP container (it has requests + network to broker)."""
    return compose("exec", "-T", "app", "python", "-c", code, check=False, timeout=120)


def ledger_lines() -> list[dict]:
    proc = compose("exec", "-T", "broker", "sh", "-c",
                   "cat /data/state/broker_ledger.jsonl 2>/dev/null || true",
                   check=False, timeout=60)
    out = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line.startswith("{"):
            out.append(json.loads(line))
    return out


def app_json(code: str, timeout: int = 200) -> dict:
    """Run python inside the app container and parse its LAST stdout line as
    JSON. The HTTP legs of this smoke run IN-NETWORK deliberately: every §8
    property under test is a compose-network property, and Windows hosts with
    WSL mirrored networking (`.wslconfig networkingMode=mirrored`) may not
    expose Docker-published ports on host loopback at all — that would fail
    the smoke for a reason §8 doesn't care about."""
    proc = compose("exec", "-T", "app", "python", "-c", code,
                   check=False, timeout=timeout)
    lines = [l for l in proc.stdout.splitlines() if l.strip().startswith(("{", "["))]
    if not lines:
        fail(f"in-container call returned no JSON: rc={proc.returncode} "
             f"out={proc.stdout[-200:]!r} err={proc.stderr[-200:]!r}")
    return json.loads(lines[-1])


def pick_tool() -> dict:
    body = app_json(
        "import requests,json;"
        f"print(json.dumps(requests.get('{APP}/api/tools',timeout=30).json()))")
    tools = body.get("tools") or []
    if not tools:
        fail("no tools in catalog")
    for t in tools:
        schema = t.get("params_schema") or t.get("params") or {}
        if not (schema.get("required") or []):
            return t
    return tools[0]


def run_job(tenant: str, tool_name: str) -> dict:
    """POST /api/run (§7 202+job) in-container and poll to terminal."""
    code = (
        "import requests,json,time\n"
        f"r=requests.post('{APP}/api/run',json={{'tool':{tool_name!r},'params':{{}}}},"
        f"headers={{'X-Tenant-Id':{tenant!r}}},timeout=60)\n"
        "assert r.status_code==202, (r.status_code, r.text[:200])\n"
        "jid=r.json()['job_id']\n"
        "t0=time.time()\n"
        "while time.time()-t0<150:\n"
        f"    j=requests.get('{APP}/api/jobs/'+jid,headers={{'X-Tenant-Id':{tenant!r}}},timeout=30).json()\n"
        "    if j.get('status') in ('complete','failed'):\n"
        "        print(json.dumps(j)); break\n"
        "    time.sleep(1)\n"
        "else:\n"
        "    print(json.dumps({'status':'poll-timeout','job_id':jid}))\n")
    job = app_json(code)
    if job.get("status") == "poll-timeout":
        fail(f"job {job.get('job_id')} not terminal within 150s")
    return job


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-build", action="store_true")
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()

    # 0) static config sanity: the broker service must publish no ports.
    cfg = compose("config", "--format", "json").stdout
    cfg_j = json.loads(cfg)
    broker_cfg = cfg_j["services"]["broker"]
    if broker_cfg.get("ports"):
        fail(f"compose config publishes broker ports: {broker_cfg['ports']}")
    if "LEAF_BROKER_SECRET" not in (broker_cfg.get("environment") or {}):
        fail("broker service missing LEAF_BROKER_SECRET wiring")

    try:
        if not args.no_build:
            print("  building images (cached after first run)...")
            compose("build")
        compose("up", "-d")
        wait_all_healthy()

        # 1) broker port truly unpublished on the live stack. compose v2 prints
        # ":0" / "0.0.0.0:0" for an UNpublished port, so parse the port number
        # instead of testing for any output, and cross-check via inspect.
        port = compose("port", "broker", "8140", check=False)
        bound = port.stdout.strip()
        if bound and not bound.endswith(":0"):
            fail(f"broker 8140 is published on the host: {bound}")
        insp = compose("ps", "--format", "json", check=False)
        for line in insp.stdout.splitlines():
            if line.strip().startswith("{"):
                row = json.loads(line)
                if row.get("Service") == "broker" and row.get("Publishers"):
                    pubs = [p for p in row["Publishers"] if p.get("PublishedPort")]
                    if pubs:
                        fail(f"broker has host publishers: {pubs}")

        # 2) secret hop enforced: wrong secret -> 401, health stays open
        probe = broker_exec_py(
            "import requests;"
            "r1=requests.post('http://broker:8140/broker/tenants/smoke-neg/disable',"
            "headers={'X-Broker-Secret':'WRONG'},timeout=15);"
            "r2=requests.get('http://broker:8140/broker/health',timeout=15);"
            "print(r1.status_code, r2.status_code)")
        got = probe.stdout.split()
        if got[:2] != ["401", "200"]:
            fail(f"secret-hop probe expected '401 200', got {probe.stdout!r} "
                 f"{probe.stderr[-200:]!r}")

        tenant = f"broker-smoke-{uuid.uuid4().hex[:8]}"
        tool = pick_tool()
        print(f"  tenant={tenant} tool={tool.get('name')}")

        # 3) one async run -> exactly one new schema-valid ledger line
        before = len(ledger_lines())
        job = run_job(tenant, tool["name"])
        if job.get("status") != "complete":
            fail(f"baseline run failed: {json.dumps(job)[:300]}")
        lines = ledger_lines()
        mine = [l for l in lines if l.get("tenant_id") == tenant]
        if len(lines) != before + 1 or len(mine) != 1:
            fail(f"expected exactly one new ledger line (before={before}, "
                 f"after={len(lines)}, tenant lines={len(mine)})")
        missing = FROZEN_KEYS - set(mine[0])
        if missing or mine[0]["status"] != "ok":
            fail(f"ledger line invalid: missing={missing} line={mine[0]}")

        # 4) kill-switch cycle through the secret hop
        code = (
            "import requests;"
            f"r=requests.post('http://broker:8140/broker/tenants/{tenant}/disable',"
            f"headers={{'X-Broker-Secret':'{SECRET}'}},timeout=15);"
            "print(r.status_code)")
        if broker_exec_py(code).stdout.strip() != "200":
            fail("disable with correct secret did not return 200")
        job2 = run_job(tenant, tool["name"])
        err_code = ((job2.get("error") or {}).get("error_code"))
        if job2.get("status") != "failed" or err_code != "TENANT_DISABLED":
            fail(f"disabled tenant run expected TENANT_DISABLED, got {json.dumps(job2)[:300]}")
        mine2 = [l for l in ledger_lines() if l.get("tenant_id") == tenant]
        if len(mine2) != 2 or mine2[-1].get("status") != "TENANT_DISABLED":
            fail(f"denial must append one TENANT_DISABLED line, got {mine2}")
        code = code.replace("/disable", "/enable")
        if broker_exec_py(code).stdout.strip() != "200":
            fail("enable with correct secret did not return 200")
        job3 = run_job(tenant, tool["name"])
        if job3.get("status") != "complete":
            fail(f"re-enabled tenant run failed: {json.dumps(job3)[:300]}")

        print("BROKER-SMOKE: PASS")
    finally:
        if not args.keep:
            compose("down", "-v", check=False, timeout=300)


if __name__ == "__main__":
    main()
