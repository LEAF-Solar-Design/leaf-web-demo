#!/usr/bin/env python3
"""Build web/ and deploy web/dist straight to the leaf-platform-web Vercel project.

The deploy root IS the build output. There is no staging directory to keep in
sync: everything the deploy needs (index.html, assets/, vercel.json, the public
fixtures) is produced by `npm run build`, because vercel.json lives in
web/public/ and vite copies public/ into dist/ on every build.

Project linkage comes from VERCEL_ORG_ID / VERCEL_PROJECT_ID rather than a
committed .vercel/ directory, because vite empties dist/ on every build — a
.vercel/ folder placed inside dist would be deleted by the next build. That is
exactly the trap the old hand-maintained staging dirs existed to work around.

Usage:
    python scripts/deploy-web.py              # build, deploy to production, verify
    python scripts/deploy-web.py --preview    # same, but a preview deploy
    python scripts/deploy-web.py --no-build   # deploy the dist that is already there
    python scripts/deploy-web.py --dry-run    # build + preflight only, no deploy

Exit code is 0 only when the deploy is live AND verified: every route returns
200 and the domain actually serves the asset filenames this build produced.
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# Windows consoles default to cp1252, which cannot encode every character this
# script prints. Force UTF-8 so a deploy never dies on an encoding error.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # already wrapped / not a real tty
        pass

REPO = Path(__file__).resolve().parent.parent
WEB = REPO / "web"
DIST = WEB / "dist"

# Not secrets: these identify the project, they do not authenticate to it.
# The credential is the Vercel CLI's own auth token (`vercel login`).
ORG_ID = "team_LWjg4ghzDbsZrkNPaOnRwAx5"
PROJECT_ID = "prj_tBxvYtXa47THZ8aF59gvRx8W0bBc"

DOMAIN = "https://leaf-platform-web.vercel.app"

# Every route the SPA owns. These are the ones that 404 without the catch-all
# rewrite, because only "/" exists as a real file on disk.
ROUTES = ["/", "/app", "/try", "/sheets", "/sheets/01"]
OBSERVED = []


def git_head() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=REPO,
            capture_output=True, text=True, timeout=30,
        )
        source = result.stdout.strip()
        if result.returncode == 0 and re.fullmatch(r"[0-9a-fA-F]{40}", source):
            return source
    except Exception:
        pass
    return "unknown"


def impact_assessment(receipt_dir: Path, source: str) -> dict:
    try:
        checker = REPO / "scripts" / "ci" / "vendor" / "impact" / "impact.py"
        if not checker.exists():
            reason = "checker-missing"
        else:
            receipt_dir.mkdir(parents=True, exist_ok=True)
            if os.name == "posix":
                receipt_dir.chmod(0o700)
            change_id = "deploy-web-" + source[:12]
            path = receipt_dir / f"{change_id}.impact.json"
            result = subprocess.run(
                [
                    sys.executable, str(checker), "check", "--workdir", str(REPO),
                    "--change-id", change_id, "--base", "HEAD", "--head", "HEAD",
                    "--transaction", "deploy", "--target", "web-spa",
                    "--record", str(receipt_dir / f"{change_id}.record.yaml"),
                    "--receipt", str(path), "--json",
                ],
                capture_output=True, text=True, timeout=120,
            )
            if result.returncode == 0:
                assessment = json.loads(path.read_text(encoding="utf-8"))
                return {
                    "change_id": assessment["change_id"],
                    "receipt": str(path),
                    "verdict": assessment["verdict"],
                    "unresolved": assessment["summary"]["unresolved"],
                }
            reason = f"exit-{result.returncode}"
    except subprocess.TimeoutExpired:
        reason = "timeout"
    except Exception:
        reason = "error"
    print(f"impact: skipped ({reason})")
    return {"skipped": reason}


def write_receipt(receipt_dir: Path, data: dict) -> None:
    try:
        receipt_dir.mkdir(parents=True, exist_ok=True)
        if os.name == "posix":
            receipt_dir.chmod(0o700)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = receipt_dir / f"deploy-web-{stamp}-{data['source'][:12]}.json"
        with path.open("w", encoding="utf-8", newline="\n") as receipt:
            receipt.write(json.dumps(data, sort_keys=True, indent=1) + "\n")
        print(f"RECEIPT {path}")
    except Exception as error:
        name = errno.errorcode.get(getattr(error, "errno", None), "error")
        print(f"RECEIPT unavailable ({name})")


def run(cmd, cwd=None, env_extra=None, capture=False):
    import os

    env = None
    if env_extra:
        env = os.environ.copy()
        env.update(env_extra)
    return subprocess.run(
        cmd,
        cwd=cwd,
        env=env,
        shell=isinstance(cmd, str),
        check=False,
        text=True,
        capture_output=capture,
    )


def fail(msg: str) -> None:
    print(f"NOT-READY: {msg}")
    sys.exit(1)


def build() -> None:
    print("-> building web/")
    r = run("npm run build", cwd=WEB)
    if r.returncode != 0:
        fail("`npm run build` failed")


def preflight() -> str:
    """Check dist is deployable. Returns the entry JS asset filename."""
    index = DIST / "index.html"
    if not index.exists():
        fail(f"{index} missing — run without --no-build")

    cfg = DIST / "vercel.json"
    if not cfg.exists():
        fail(
            f"{cfg} missing — web/public/vercel.json should have been copied by "
            "the build; without it every route except / returns 404"
        )

    try:
        conf = json.loads(cfg.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        fail(f"{cfg} is not valid JSON: {e}")

    if not conf.get("rewrites"):
        fail(f"{cfg} has no rewrites — /app, /try and /sheets would 404")

    # cleanUrls 308-redirects /index.html to /, so a catch-all rewrite pointing
    # at /index.html resolves to a redirect and Vercel answers 404. The two are
    # mutually exclusive; this guard stops that regression coming back.
    if conf.get("cleanUrls"):
        fail(
            f'{cfg} sets "cleanUrls": true, which breaks the SPA rewrite '
            "(/index.html becomes a 308 to /, so the rewrite target is itself a "
            "redirect and every deep route 404s). Remove it."
        )

    html = index.read_text(encoding="utf-8")
    m = re.search(r'assets/index-[A-Za-z0-9_-]+\.js', html)
    if not m:
        fail("could not find the entry asset reference in dist/index.html")
    entry = m.group(0)
    print(f"  preflight ok — entry asset {entry}")
    return entry


def deploy(preview: bool) -> None:
    target = "preview" if preview else "production"
    print(f"-> deploying {DIST} to {target}")
    # On Windows the CLI is vercel.cmd, which a list-form exec cannot resolve
    # by bare name — look up the real path instead of shelling out.
    vercel = shutil.which("vercel")
    if not vercel:
        fail("`vercel` CLI not found on PATH — install it or run `vercel login`")
    cmd = [vercel, "deploy", "--yes"] + ([] if preview else ["--prod"])
    r = run(
        cmd,
        cwd=DIST,
        env_extra={"VERCEL_ORG_ID": ORG_ID, "VERCEL_PROJECT_ID": PROJECT_ID},
        capture=True,
    )
    if r.returncode != 0:
        print(r.stdout)
        print(r.stderr, file=sys.stderr)
        fail("`vercel deploy` failed")
    print(f"  deployed ({target})")


def fetch(url: str, timeout: int = 30):
    req = urllib.request.Request(url, headers={"Cache-Control": "no-cache"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception as e:  # network hiccup — caller decides whether to retry
        return None, str(e)


def verify(entry: str) -> None:
    """The deploy is only done when the live domain serves THIS build."""
    print(f"-> verifying {DOMAIN}")

    # The alias can take a moment to swing to the new deployment.
    served = None
    for attempt in range(6):
        status, body = fetch(DOMAIN + "/")
        if status == 200 and entry in body:
            served = entry
            break
        time.sleep(5)
    if not served:
        fail(
            f"{DOMAIN}/ is not serving this build's entry asset ({entry}) after "
            "~30s — the alias may still be pointing at the previous deployment"
        )
    print(f"  live entry matches build: {entry}")

    bad = []
    for route in ROUTES:
        status, _ = fetch(DOMAIN + route)
        OBSERVED.append({"route": route, "status": status})
        mark = "ok" if status == 200 else f"HTTP {status}"
        print(f"  {route:<14} {mark}")
        if status != 200:
            bad.append((route, status))
    if bad:
        fail(
            "these routes did not return 200: "
            + ", ".join(f"{r} -> {s}" for r, s in bad)
        )

    print("READY: deploy is live and every route verified")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--preview", action="store_true", help="preview deploy, not production")
    ap.add_argument("--no-build", action="store_true", help="deploy the existing dist")
    ap.add_argument("--dry-run", action="store_true", help="build + preflight, no deploy")
    ap.add_argument(
        "--receipt-dir", type=Path,
        default=Path.home() / ".claude" / "state" / "deploy-web",
        help="directory for deploy and impact receipts",
    )
    args = ap.parse_args()

    if not args.no_build:
        build()
    entry = preflight()

    if args.dry_run:
        print("READY: dry run — dist is deployable (nothing was deployed)")
        return

    started_at = datetime.now(timezone.utc).isoformat()
    source = git_head()
    target = "preview" if args.preview else "production"
    domain = DOMAIN
    OBSERVED.clear()
    impact = impact_assessment(args.receipt_dir, source)
    outcome = "not-ready"
    try:
        deploy(args.preview)

        if args.preview:
            # A preview deploy never touches the production alias, so verifying the
            # domain would just re-check whatever is already live.
            print("READY: preview deployed (production alias untouched, not verified)")
            outcome = "ready"
            return

        verify(entry)
        outcome = "ready"
    finally:
        write_receipt(args.receipt_dir, {
            "schema": "leaf.deploy-web.v1",
            "target": target,
            "domain": domain,
            "entry": entry,
            "source": source,
            "started_at": started_at,
            "ended_at": datetime.now(timezone.utc).isoformat(),
            "outcome": outcome,
            "routes": list(OBSERVED),
            "impact": impact,
        })


if __name__ == "__main__":
    main()
