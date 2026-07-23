#!/usr/bin/env python3
"""Containerized smoke for the authoring-harness lane (census #13, CONTRACT-ADDENDUM
section 15/16/17 + HARNESS-CONTRACT).

Drives the REAL compose topology (broker + harness + app; scripts/compose.harness-smoke.yml
overrides) and proves, from inside the compose network, the five claims this lane ships:

  1. the harness runs as the platform's third process/container and boots healthy;
  2. the app->harness hop is authenticated: no/wrong X-Harness-Secret -> 401
     (code harness_auth_required), and the app's own calls succeed because compose
     wires the SAME LEAF_HARNESS_SECRET into both services;
  3. per-tenant grants live on the durable leaf-grants volume: linked via the app
     proxy, never echoed, still linked after a harness container restart;
  4. per-tenant repos live on the durable leaf-tenants volume shared app<->harness
     (section 16.H): a mock-authored tool materializes the repo AND shows up in the
     app's /api/tools catalog fold;
  5. logs are secret-free: neither the grant token value nor the harness secret
     appears in `docker compose logs`.

All HTTP probes run INSIDE the app container (`docker compose exec app python -`),
so the smoke needs no host port forwarding and the harness stays
compose-network-internal, exactly like the production posture. The probes therefore
also prove what a LATERAL container sees: the harness rejects any caller without
the secret. The harness runs LEAF_AGENT_MOCK=1 (scripted fakes): no Agent SDK, no
Anthropic egress, no LLM credit.

Exit 0 = PASS; exit 1 = FAIL (compose logs tail printed for postmortem); exit 3 =
environment unavailable (no Docker / compose < 2.24) — the gate runner maps 3 to SKIP.

Run:  python scripts/harness-container-smoke.py [--keep]
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PROJECT = "leaf-harness-smoke"
COMPOSE = ["docker", "compose", "-p", PROJECT,
           "-f", str(REPO / "docker-compose.yml"),
           "-f", str(REPO / "scripts" / "compose.harness-smoke.yml")]

HARNESS_SECRET = secrets.token_hex(24)
FAKE_TOKEN = f"FAKE-OAUTH-container-smoke-{secrets.token_hex(12)}"

UNAVAILABLE = 3

# ---- in-container probe payloads (run via `docker compose exec -T app python -`).
# They see the compose-internal DNS names (http://localhost:8130 = the app itself,
# http://harness:8150 = the harness) and print ONE json line the host asserts on.
# sys.argv[1] is the fake token. They never see the real harness secret.
PROBE_COMMON = r"""
import json, sys, urllib.error, urllib.request

def http(method, url, body=None, headers=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("content-type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")

def j(raw):
    try:
        return json.loads(raw)
    except Exception:
        return {"_raw": raw[:500]}

APP = "http://localhost:8130"
HARNESS = "http://harness:8150"
out = {}
"""

PROBE_MAIN = PROBE_COMMON + r"""
fake_token = sys.argv[1]

# 1+2: harness health is open; everything else fail-closed without the secret.
s, raw = http("GET", HARNESS + "/health")
out["health"] = {"status": s, "body": j(raw)}
s, raw = http("POST", HARNESS + "/author", {"description": "x"})
out["author_no_secret"] = {"status": s, "body": j(raw)}
s, raw = http("POST", HARNESS + "/author", {"description": "x"},
              headers={"X-Harness-Secret": "not-the-secret"})
out["author_wrong_secret"] = {"status": s, "body": j(raw)}

# 3: link a grant through the APP proxy (the app adds the real secret itself).
s, raw = http("POST", APP + "/api/tenant/claude-grant", {"token": fake_token})
out["grant_put"] = {"status": s, "body": j(raw), "echoed": fake_token in raw}
s, raw = http("GET", APP + "/api/tenant/claude-grant")
out["grant_get"] = {"status": s, "body": j(raw), "echoed": fake_token in raw}

# 4: author through the app (mock agent) + catalog fold.
s, raw = http("POST", APP + "/api/author",
              {"description": "count panels by layer"}, timeout=120)
body = j(raw)
out["author"] = {"status": s, "source": body.get("source"),
                 "tool": (body.get("tool") or {}).get("name")}
s, raw = http("GET", APP + "/api/tools")
out["tools"] = {"status": s,
                "names": [t.get("name") for t in (j(raw).get("tools") or [])]}
print(json.dumps(out))
"""

PROBE_AFTER_RESTART = PROBE_COMMON + r"""
fake_token = sys.argv[1]
s, raw = http("GET", APP + "/api/tenant/claude-grant")
out["grant_get"] = {"status": s, "body": j(raw), "echoed": fake_token in raw}
s, raw = http("DELETE", APP + "/api/tenant/claude-grant")
out["grant_delete"] = {"status": s, "body": j(raw)}
# sol-critic F6: DELETE alone is not proof — a fallback could resurrect the
# grant. Read back the actual post-delete state (no env fallback is mounted in
# this stack, so honest state is unlinked).
s, raw = http("GET", APP + "/api/tenant/claude-grant")
out["grant_get_after_delete"] = {"status": s, "body": j(raw), "echoed": fake_token in raw}
print(json.dumps(out))
"""


def run(argv: list[str], timeout: int = 120, check: bool = True,
        stdin_text: str | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["LEAF_HARNESS_SECRET"] = HARNESS_SECRET
    proc = subprocess.run(argv, cwd=str(REPO), env=env, capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          timeout=timeout, input=stdin_text)
    if check and proc.returncode != 0:
        raise RuntimeError(f"command failed ({proc.returncode}): {' '.join(argv)}\n"
                           f"stdout: {proc.stdout[-2000:]}\nstderr: {proc.stderr[-2000:]}")
    return proc


def probe(payload: str) -> dict:
    proc = run(COMPOSE + ["exec", "-T", "app", "python", "-", FAKE_TOKEN],
               timeout=300, stdin_text=payload)
    line = proc.stdout.strip().splitlines()[-1]
    return json.loads(line)


def expect(cond: bool, what: str) -> None:
    if not cond:
        raise AssertionError(what)
    print(f"  ok: {what}", flush=True)


def wait_container_healthy(name: str, deadline_s: int = 120) -> None:
    t0 = time.monotonic()
    while time.monotonic() - t0 < deadline_s:
        proc = run(["docker", "inspect", "-f", "{{.State.Health.Status}}",
                    f"{PROJECT}-{name}-1"], check=False)
        if proc.stdout.strip() == "healthy":
            return
        time.sleep(2)
    raise RuntimeError(f"{name} not healthy after {deadline_s}s")


def preflight() -> None:
    try:
        v = run(["docker", "version", "--format", "{{.Server.Version}}"], timeout=30)
    except Exception as e:  # noqa: BLE001 - docker missing/not running
        print(f"UNAVAILABLE: docker not usable ({e})")
        sys.exit(UNAVAILABLE)
    cv = run(["docker", "compose", "version", "--short"], timeout=30, check=False)
    ver = (cv.stdout or "").strip().lstrip("v")
    parts = ver.split(".")
    try:
        ok = (int(parts[0]), int(parts[1])) >= (2, 24)  # !override needs v2.24+
    except (ValueError, IndexError):
        ok = False
    if cv.returncode != 0 or not ok:
        print(f"UNAVAILABLE: docker compose >= 2.24 required (got {ver or 'none'})")
        sys.exit(UNAVAILABLE)
    print(f"docker {v.stdout.strip()} / compose {ver}", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true",
                    help="leave the stack up for debugging (skip down -v)")
    args = ap.parse_args()

    preflight()
    t0 = time.monotonic()
    failed = False
    try:
        print("== build + up (broker, harness, app; --wait blocks on healthchecks) ==",
              flush=True)
        run(COMPOSE + ["up", "-d", "--build", "--wait", "app"], timeout=900)

        print("== probes (inside the compose network, via the app container) ==",
              flush=True)
        r = probe(PROBE_MAIN)

        expect(r["health"]["status"] == 200, "harness GET /health needs no secret (200)")
        expect(r["author_no_secret"]["status"] == 401
               and r["author_no_secret"]["body"].get("error", {}).get("code")
               == "harness_auth_required",
               "POST /author with NO secret -> 401 harness_auth_required")
        expect(r["author_wrong_secret"]["status"] == 401
               and r["author_wrong_secret"]["body"].get("error", {}).get("code")
               == "harness_auth_required",
               "POST /author with WRONG secret -> 401 harness_auth_required")

        expect(r["grant_put"]["status"] == 200
               and r["grant_put"]["body"].get("linked") is True,
               "POST /api/tenant/claude-grant -> linked:true (app->harness hop authed)")
        expect(r["grant_put"]["echoed"] is False, "link response does not echo the token")
        expect("token" not in r["grant_put"]["body"]
               and "token" not in r["grant_get"]["body"],
               "grant wire carries no 'token' key (only linked/linked_at/kind + envelope)")
        expect(r["grant_get"]["status"] == 200
               and r["grant_get"]["body"].get("linked") is True
               and r["grant_get"]["body"].get("kind") == "oauth",
               "GET grant -> linked, kind=oauth")
        expect(r["grant_get"]["echoed"] is False, "status response does not echo the token")

        expect(r["author"]["status"] == 200 and r["author"]["source"] == "harness",
               "POST /api/author -> 200 source:harness (mock agent, zero Anthropic egress)")
        tool_name = r["author"]["tool"] or ""
        expect(bool(tool_name), f"authored tool has a name ({tool_name!r})")
        ls = run(COMPOSE + ["exec", "-T", "harness", "ls", "/data/tenants/demo-tenant"])
        expect("registry.json" in ls.stdout,
               "tenant repo materialized on the leaf-tenants volume (registry.json)")
        expect(tool_name in r["tools"]["names"],
               f"authored tool {tool_name!r} appears in the app catalog fold (section 16.H)")

        print("== restart durability ==", flush=True)
        run(COMPOSE + ["restart", "harness"], timeout=120)
        wait_container_healthy("harness")
        r2 = probe(PROBE_AFTER_RESTART)
        expect(r2["grant_get"]["status"] == 200
               and r2["grant_get"]["body"].get("linked") is True,
               "grant still linked after harness restart (leaf-grants volume durable)")
        expect(r2["grant_get"]["echoed"] is False, "post-restart status does not echo the token")
        ls = run(COMPOSE + ["exec", "-T", "harness", "ls", "/data/tenants/demo-tenant"])
        expect("registry.json" in ls.stdout, "tenant repo survives restart")
        expect(r2["grant_delete"]["status"] == 200
               and r2["grant_delete"]["body"].get("linked") is False,
               "DELETE grant -> linked:false (no fallback mounted in this stack)")
        expect(r2["grant_get_after_delete"]["status"] == 200
               and r2["grant_get_after_delete"]["body"].get("linked") is False,
               "GET after DELETE -> still unlinked (deletion is real, not cosmetic)")

        print("== logs are secret-free ==", flush=True)
        logs = run(COMPOSE + ["logs", "harness", "app"], timeout=120)
        blob = logs.stdout + logs.stderr
        expect(FAKE_TOKEN not in blob, "grant token value never appears in logs")
        expect(HARNESS_SECRET not in blob, "harness secret value never appears in logs")

        wall = time.monotonic() - t0
        print(json.dumps({"status": "passed", "wall_s": round(wall, 1),
                          "authored_tool": tool_name}, sort_keys=True))
        return 0
    except BaseException:
        failed = True
        raise
    finally:
        if failed:
            tail = run(COMPOSE + ["logs", "--tail", "80"], timeout=120, check=False)
            print("---- compose logs tail (postmortem) ----\n"
                  + tail.stdout[-6000:], file=sys.stderr)
        if args.keep:
            print(f"--keep: stack left up (project {PROJECT})")
        else:
            run(COMPOSE + ["down", "-v", "--remove-orphans"], timeout=300, check=False)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (AssertionError, RuntimeError) as e:
        print(f"FAIL: {e}", file=sys.stderr)
        sys.exit(1)
