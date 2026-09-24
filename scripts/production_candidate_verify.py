"""Read-only V-01 candidate report. Offline probes use manifest main/staging fields.

main: {sha, older_open_prs[, frozen, candidate_is_ancestor, drift_commits]};
staging: {source_sha, identity_status}.
Receipt paths resolve beside the manifest, including the self-contained fixture.
"""

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request


SCHEMA = "w4g-align/candidate-manifest.v1"
STAGING = "https://platform-staging.leafdesign.ai"
PRODUCTION = "https://platform.leafdesign.ai"
USER_AGENT = "leaf-production-candidate-verify/1.0"
SHA = re.compile(r"[0-9a-fA-F]{40}\Z")
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
ROLES = ("app", "broker", "canonical-worker", "harness", "web")
MERGED_SHA = re.compile(r"[0-9a-f]{40}\Z")
HOST = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\Z")
# Worst status wins per title; a flaky row counts as passed.
PROOF_RANK = {"passed": 0, "flaky": 0, "skipped": 1, "failed": 2}
PROD_SMOKE_ROWS = (
    "production-like unified route is reachable without mutation",
    "production app health reports its served source",
    "production web health reports its served source",
    "production deployment identity answers without a bearer",
    "production auth ladder rejects absent and invalid bearers",
    "production app displays its served build stamp",
    "production app renders the studio shell",
    "production app and web serve the expected candidate",
)


def command(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True,
                          timeout=15).stdout.strip()


def probe_main_sha():
    return command("git", "ls-remote", "origin", "main").split()[0]


def probe_gh_open_prs(candidate):
    if not shutil.which("gh"):
        raise FileNotFoundError("gh")
    created = command("git", "show", "-s", "--format=%cI", candidate)
    cutoff = datetime.fromisoformat(created.replace("Z", "+00:00"))
    # Paginate explicitly: gh's default list limit must not hide older PRs.
    limit = 100
    while True:
        rows = json.loads(command("gh", "pr", "list", "--state", "open",
                                  "--json", "number,createdAt,isDraft", "--limit", str(limit)))
        if len(rows) < limit:
            # A draft is held by design (line 13 requires one), so it is not counted.
            return sum(datetime.fromisoformat(row["createdAt"].replace("Z", "+00:00"))
                       < cutoff for row in rows if row.get("isDraft") is not True)
        limit *= 2


def get_json(url):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=15) as response:
        if response.status != 200:
            raise urllib.error.HTTPError(url, response.status, "unexpected status", {}, None)
        return json.load(response)


def probe_health_sha(origin):
    health = get_json(origin + "/api/health")
    if not isinstance(health.get("source_sha"), str) or not SHA.fullmatch(health["source_sha"]):
        raise ValueError()
    result = {"source_sha": health["source_sha"]}
    if origin == STAGING:
        try:
            get_json(origin + "/api/deployment-identity")
            result["identity_status"] = "HTTP 200"
        except urllib.error.HTTPError as error:
            result["identity_status"] = 401 if error.code == 401 else f"HTTP {error.code}"
        except Exception as error:
            result["identity_status"] = type(error).__name__
    return result


def title(value):
    return re.sub(r":\d+:\d+(?=\s*›)", "", ANSI.sub("", value)).strip()


def clean(value):
    value = ANSI.sub("", str(value))
    return re.sub(r"[\x00-\x1f\x7f-\x9f]", "", value)


class PathRefused(ValueError):
    pass


def local_path(value, directory):
    def validate(text, absolute=False):
        if text.startswith(("\\\\", "//")) or "://" in text:
            raise PathRefused()
        if absolute and not (re.fullmatch(r"[A-Za-z]:[\\/].*", text) if os.name == "nt"
                             else text.startswith("/")):
            raise PathRefused()

    validate(str(value))
    resolved = Path(value).expanduser()
    validate(str(resolved))
    if not resolved.is_absolute():
        resolved = directory / resolved
    validate(str(resolved.absolute()), absolute=True)
    resolved = resolved.resolve()
    validate(str(resolved), absolute=True)
    return resolved


def report(manifest, directory, live=True):
    results = []
    secrets = set()
    lines = []

    def collect(value, sensitive=False):
        if isinstance(value, dict):
            for key, item in value.items():
                collect(item, sensitive or bool(re.search(
                    r"token|jwt|password|secret|credential|authorization|bearer|cookie", key, re.I)))
        elif isinstance(value, list):
            for item in value:
                collect(item, sensitive)
        elif sensitive and isinstance(value, str) and len(clean(value)) >= 4:
            secrets.add(clean(value))

    collect(manifest)

    def safe(value):
        base = clean(value)
        spans = []
        for secret in sorted(secrets, key=len, reverse=True):
            start = base.find(secret)
            while start != -1:
                spans.append((start, start + len(secret), False))
                start = base.find(secret, start + 1)
        for pattern, is_url in (
            (r"(?i)[a-z][a-z0-9+.-]*://[^\s/?#]*@[^\s]*", True),
            (r"(?i)Bearer\s+\S+", False),
            (r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+(?![A-Za-z0-9_-])", False),
        ):
            spans.extend((match.start(), match.end(), is_url)
                         for match in re.finditer(pattern, base))
        merged = []
        for start, end, is_url in sorted(spans):
            if merged and start <= merged[-1][1]:
                previous = merged[-1]
                merged[-1] = (previous[0], max(previous[1], end), previous[2] or is_url)
            else:
                merged.append((start, end, is_url))
        parts = []
        cursor = 0
        for start, end, is_url in merged:
            parts.extend((base[cursor:start], "[redacted-url]" if is_url else "[redacted]"))
            cursor = end
        parts.append(base[cursor:])
        return " ".join("".join(parts).split())[:400]

    def path(value):
        return local_path(value, directory)

    def receipt(section):
        with path(manifest[section]["receipt"]).open(encoding="utf-8") as stream:
            data = json.load(stream)
        collect(data)
        return data

    def check(number, name, operation):
        try:
            outcome, evidence = operation()
            status = outcome if isinstance(outcome, str) else "PASS" if outcome else "FAIL"
        except PathRefused:
            status, evidence = "PENDING", "path refused"
        except urllib.error.HTTPError as error:
            status, evidence = "PENDING", f"HTTP {error.code}"
        except Exception as error:
            status, evidence = "PENDING", type(error).__name__
        results.append(status)
        lines.append((number, status, name, evidence))

    candidate = manifest["candidate"]

    def main_check():
        sha = probe_main_sha() if live else manifest["main"]["sha"]
        try:
            count = probe_gh_open_prs(candidate) if live else manifest["main"]["older_open_prs"]
        except Exception as error:
            return "PENDING", f"main={sha} candidate={candidate}; gh {type(error).__name__}"
        pinned = manifest.get("main") if isinstance(manifest.get("main"), dict) else {}
        if sha != candidate and pinned.get("frozen") is True:
            return frozen_main(sha, count, pinned)
        if live and (sha != candidate or type(count) is not int or count != 0):
            return "PENDING", f"main={sha} candidate={candidate}; older_open_prs={count}"
        return sha == candidate and type(count) is int and count == 0, f"main={sha} candidate={candidate}; older_open_prs={count}"

    # A frozen candidate stays valid while main only gains later commits on top of it.
    def frozen_main(sha, count, pinned):
        evidence = f"main={sha} candidate={candidate}; older_open_prs={count}; frozen candidate; main moved"
        if "drift_commits" in pinned:
            evidence += f"; drift_commits={pinned['drift_commits']}"
        if live:
            # Both are argv to git: only a full sha may reach it, never an option.
            if not all(isinstance(value, str) and SHA.fullmatch(value) for value in (sha, candidate)):
                return "PENDING", evidence + "; ancestry unreadable"
            try:
                code = subprocess.run(["git", "merge-base", "--is-ancestor", candidate, sha],
                                      capture_output=True, text=True, timeout=15).returncode
            except Exception as error:
                return "PENDING", evidence + f"; ancestry {type(error).__name__}"
            if type(code) is not int or code not in (0, 1):
                return "PENDING", evidence + f"; ancestry exit {code}"
            ancestor = code == 0
        else:
            ancestor = pinned.get("candidate_is_ancestor") is True
        evidence += f"; candidate_is_ancestor={ancestor}"
        counted = type(count) is int and count == 0
        if live and ancestor and not counted:
            return "PENDING", evidence
        return ancestor and counted, evidence

    def staging_check():
        data = probe_health_sha(STAGING) if live else manifest["staging"]
        sha = data["source_sha"] if isinstance(data, dict) else data
        identity = data.get("identity_status", "unreported") if isinstance(data, dict) else "unreported"
        if identity != 401:
            detail = f"HTTP {identity}" if isinstance(identity, int) else identity
            return "PENDING", f"served={sha} candidate={candidate}; identity={detail}"
        if live and sha != candidate:
            return "PENDING", f"served={sha} candidate={candidate}; identity={identity}"
        return sha == candidate, f"served={sha} candidate={candidate}; identity={identity}"

    def proof_check():
        baseline, current = manifest["baseline_proof"], manifest["candidate_proof"]
        if "source_sha" in current and current["source_sha"] != candidate:
            return False, f"stale proof: {current['source_sha']}"
        if "rows" not in baseline or "rows" not in current:
            new = sorted(set(map(title, current["reds"])) - set(map(title, baseline["reds"])))
            return not new, f"new_reds={len(new)}; baseline={baseline['run']}; candidate={current['run']}; rows={', '.join(new)}; legacy reds-only"
        before, after = proof_rows(baseline["rows"]), proof_rows(current["rows"])
        # Reds still listed beside rows keep their own set comparison.
        new = sorted(set(map(title, current.get("reds", []))) - set(map(title, baseline.get("reds", []))))
        failed = sorted(name for name, status in after.items()
                        if status == "failed" and before.get(name) != "failed")
        skipped = sorted(name for name, status in after.items()
                         if status == "skipped" and before.get(name) == "passed")
        missing = sorted(set(before) - set(after))
        named = list(dict.fromkeys(new + failed + skipped + missing))[:5]
        ok = not (new or failed or skipped or missing)
        return ok, (f"new_reds={len(new)}; new_failed={len(failed)}; new_skipped={len(skipped)}; "
                    f"missing={len(missing)}; baseline={baseline['run']}; candidate={current['run']}; "
                    f"rows={', '.join(named)}")

    def board_check():
        rows = manifest["start_board"]["rows"]
        failing = [row["name"] for row in rows if row["pass"] is not True]
        return bool(rows) and not failing, f"rows={len(rows)}; failing={', '.join(failing)}"

    def surfaces_check():
        data = manifest["surfaces"]
        names = ("browser", "cad", "solar", "ios")
        ok = all(data[name]["cockpit"] is True for name in names)
        ok = ok and data["solar_home"] == "solar" and data["ios_contract"] in ("served", "setup-required")
        return ok, "; ".join(f"{name}={data[name]['cockpit']} {data[name]['evidence']}" for name in names) + f"; solar_home={data['solar_home']}; ios_contract={data['ios_contract']}"

    def count_check(section, field):
        data = manifest[section]
        count, total = data[field], data["total"]
        return type(count) is int and type(total) is int and count == total > 0, f"{field}={count}/{total}; {data['evidence']}"

    def tokens_check():
        prs, disposition = manifest["ssd3"]["prs"], manifest["ssd5"]["disposition"]
        return bool(prs) and all(pr["merged"] is True for pr in prs) and nonempty(disposition), f"PRs={[(pr['number'], pr['merged']) for pr in prs]}; SSD5={disposition}"

    def plan_check():
        data = receipt("production_plan")
        source = data["source_revision"]
        # an int 0 exactly: JSON false compares equal to 0 in Python and is not a passing exit
        ok = type(data["exit"]) is int and data["exit"] == 0 and type(data["planned_operations"]) is int and data["planned_operations"] >= 1
        ok = ok and nonempty(data["promotion"]) and source == candidate
        ok = ok and all(re.fullmatch(r"sha256:[0-9a-fA-F]{64}", data["images"].get(role, "")) for role in ROLES)
        return ok, f"source_revision={source} candidate={candidate}; exit={data['exit']}; planned_operations={data['planned_operations']}; promotion={data['promotion']}; five image digests={'valid' if ok else 'see receipt'}"

    def smoke_check():
        data = receipt("prod_smoke")
        if "rows" not in data:
            return "PENDING", "legacy count receipt; rows required"
        expected = data["served_source_sha"]
        if expected != candidate:
            # The smoke of the production live before promotion is a baseline, never terminal.
            return "PENDING", f"baseline smoke of {expected}"
        rows = data["rows"]
        if not isinstance(rows, list):
            raise ValueError()
        seen = {}
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("name"), str):
                raise ValueError()
            seen.setdefault(row["name"], []).append(row.get("status"))
        # Every occurrence of a required name must pass; a duplicate cannot mask a failure.
        # name=status keeps "bearer" out of the sanitizer's "Bearer <token>" pattern.
        failing = [f"{name}={'/'.join(map(str, seen[name])) if name in seen else 'missing'}"
                   for name in PROD_SMOKE_ROWS
                   if not seen.get(name) or any(status != "passed" for status in seen[name])]
        actual = expected
        if live:
            health = probe_health_sha(PRODUCTION)
            actual = health["source_sha"] if isinstance(health, dict) else health
        ok = not failing and nonempty(data["staging_refusal"])
        return ok and actual == expected, f"required_rows={len(PROD_SMOKE_ROWS) - len(failing)}/{len(PROD_SMOKE_ROWS)} passed; failing={', '.join(failing)}; staging_refusal={data['staging_refusal']}; receipt_sha={expected}; served_sha={actual}; {'live' if live else 'offline receipt only'}"

    def auth_check():
        data = receipt("auth_ladder")
        staging = data.get("staging", {})
        if statuses(staging.get("ladder")) != [401, 200, 403]:
            return "PENDING", f"staging ladder incomplete; {staging.get('note', 'missing staging ladder')}"
        production = statuses(data["ladder"])
        return data["pass"] is True and production == [401, 200, 403], f"production={production}; staging=[401, 200, 403]"

    # Mirrors staging leaf_platform.tf's authored-activation comment and
    # leaf_platform_runtime_posture.py _assert_broker: idle or coherent active.
    def hardening_check():
        data = manifest["hardening"]
        window = path(data["aps_window_receipt"]).is_file()
        staging = data.get("authored_execution_staging")
        activation = data.get("staging_authored_activation")
        idle = type(staging) is int and staging == 0
        active = (type(staging) is int and staging == 1 and isinstance(activation, dict)
                  and activation.get("tool_sandbox_provider") == "e2b"
                  and activation.get("customization_r5_mode") == "all"
                  and activation.get("customization_r6_mode") == "all"
                  and activation.get("customization_store") == "postgres"
                  and activation.get("e2b_api_key_secret") is True
                  and type(activation.get("tenant_cap_usd")) in (int, float)
                  and activation["tenant_cap_usd"] > 0)
        posture = "idle" if idle else f"invalid ({staging})"
        if active:
            posture = (f"active ({activation['tool_sandbox_provider']}, "
                       f"r5 {activation['customization_r5_mode']}, "
                       f"r6 {activation['customization_r6_mode']}, "
                       f"{activation['customization_store']}, cap {activation['tenant_cap_usd']})")
        ok = data["enforcement_fail_closed_pr"]["merged"] is True
        ok = ok and (idle or active) and type(data["authored_execution_production"]) is int and data["authored_execution_production"] == 0
        ok = ok and type(data["aps_max_concurrency_staging"]) is int and data["aps_max_concurrency_staging"] == 10 and window
        return ok, f"PR={data['enforcement_fail_closed_pr']['number']} merged={data['enforcement_fail_closed_pr']['merged']}; authored staging={posture} production={data['authored_execution_production']}; APS staging={data['aps_max_concurrency_staging']} production={data['aps_max_concurrency_production']}; window_exists={window}"

    def door_check():
        data = manifest["door_pr"]
        if data["state"] != "completed":
            return data["state"] == "draft" and nonempty(data["rollback"]), f"PR={data['number']}; state={data['state']}; rollback={data['rollback']}"
        # A repeat release: the door merged and deployed; its deploy receipt must agree.
        target, merged = data.get("target"), data.get("merged_sha")
        evidence = f"PR={data['number']}; state=completed; target={target}; merged_sha={merged}; rollback={data.get('rollback')}"
        ok = isinstance(merged, str) and MERGED_SHA.fullmatch(merged) is not None
        ok = ok and nonempty(target) and HOST.fullmatch(target) is not None
        ok = ok and nonempty(data.get("rollback")) and nonempty(data.get("deploy_receipt"))
        if not ok:
            return False, evidence
        source = path(data["deploy_receipt"])
        if not source.is_file():
            return False, evidence + "; deploy_receipt missing"
        try:
            with source.open(encoding="utf-8") as stream:
                deployed = json.load(stream)
        except ValueError:  # JSONDecodeError and UnicodeDecodeError
            return False, evidence + "; deploy_receipt unparseable"
        if not isinstance(deployed, dict):
            return False, evidence + "; deploy_receipt not an object"
        collect(deployed)
        if "target" in deployed and deployed["target"] != target:
            return False, evidence + f"; deploy_receipt target={deployed['target']}"
        return True, evidence + "; deploy_receipt present"

    check(1, "main == " + candidate, main_check)
    check(2, "staging", staging_check)
    check(3, "managed proof", proof_check)
    check(4, "Start board", board_check)
    check(5, "surfaces", surfaces_check)
    check(6, "SSD2", lambda: count_check("ssd2", "served"))
    check(7, "SSD1", lambda: count_check("ssd1", "proven"))
    check(8, "SSD3 and SSD5", tokens_check)
    check(9, "production plan", plan_check)
    check(10, "production smoke", smoke_check)
    check(11, "auth ladder", auth_check)
    check(12, "hardening", hardening_check)
    check(13, "door PR", door_check)
    # Collect all receipt secrets before emitting even the earliest evidence.
    for number, status, name, evidence in lines:
        print(f"{number:02d} {status} {safe(name)} :: {safe(evidence)}")
    items = manifest.get("operator_items", [])
    items = items if isinstance(items, list) else []
    print(f"14 OPERATOR operator items :: {len(items)} items")
    for item in items:
        if isinstance(item, dict):
            print(f"   - {safe(item.get('id', 'missing'))} ({safe(item.get('owner', 'operator'))}): {safe(item.get('text', 'missing'))}")
    return 0 if all(status == "PASS" for status in results) else 1


def nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def statuses(ladder):
    if not isinstance(ladder, list):
        return None
    return [entry.get("status") if isinstance(entry, dict) else entry for entry in ladder]


def proof_rows(rows):
    """Title -> worst status, flaky folded into passed; fails closed on any malformed row."""
    if not isinstance(rows, list):
        raise ValueError()
    result = {}
    for row in rows:
        if (not isinstance(row, dict) or not isinstance(row.get("title"), str)
                or not isinstance(row.get("status"), str) or row["status"] not in PROOF_RANK):
            raise ValueError()
        name = title(row["title"])
        status = "passed" if row["status"] == "flaky" else row["status"]
        if name not in result or PROOF_RANK[status] > PROOF_RANK[result[name]]:
            result[name] = status
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True)
    live = parser.add_mutually_exclusive_group()
    live.add_argument("--live", dest="live", action="store_true")
    live.add_argument("--no-live", dest="live", action="store_false")
    parser.set_defaults(live=True)
    args = parser.parse_args(argv)
    try:
        source = local_path(args.manifest, Path.cwd())
        with source.open(encoding="utf-8") as stream:
            manifest = json.load(stream)
        if not isinstance(manifest, dict) or manifest.get("schema") != SCHEMA:
            raise ValueError()
        if not isinstance(manifest.get("candidate"), str) or not SHA.fullmatch(manifest["candidate"]):
            raise ValueError()
    except Exception as error:
        print(f"Manifest refused ({type(error).__name__}).", file=sys.stderr)
        return 2
    return report(manifest, source.parent, args.live)


if __name__ == "__main__":
    raise SystemExit(main())
