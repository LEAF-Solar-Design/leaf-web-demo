"""Bounded APS concurrency measurement, dry run by default.

Live mode spends money and is bounded by a spend cap. This tool never prints,
logs or writes a credential value and fails closed on malformed input.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import importlib
import json
import math
import os
from pathlib import Path
import subprocess
import threading
import time


ROOT = Path(__file__).resolve().parent.parent
ACTIVITY = "LeafTool_count_by_layer"
TENANT = "aps-ramp"
TIMES = ("timeQueued", "timeDownloadStarted", "timeInstructionsStarted",
         "timeInstructionsEnded", "timeFinished")


def _levels(value):
    try:
        values = [int(part) for part in value.split(",")]
        if not values or any(n < 1 or n > 100 for n in values):
            raise ValueError
        if any(a >= b for a, b in zip(values, values[1:])):
            raise ValueError
        return values
    except ValueError:
        raise argparse.ArgumentTypeError("levels must increase strictly within 1..100") from None


def _stamp(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat()


def _percentile(values, fraction):
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    index = (len(values) - 1) * fraction
    lo, hi = math.floor(index), math.ceil(index)
    return values[lo] + (values[hi] - values[lo]) * (index - lo)


def _timing(status, rate):
    stats = status.get("stats") or {}
    stamps = {}
    for key in TIMES:
        value = stats.get(key)
        stamps[key] = datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None
    def delta(end, start):
        if stamps[end] is None or stamps[start] is None:
            return None
        seconds = (stamps[end] - stamps[start]).total_seconds()
        if seconds < 0 or not math.isfinite(seconds):
            raise ValueError("invalid WorkItem timing")
        return seconds
    engine = delta("timeInstructionsEnded", "timeInstructionsStarted")
    return {**{k: v.isoformat() if v else None for k, v in stamps.items()},
            "queue_s": delta("timeInstructionsStarted", "timeQueued"),
            "engine_s": engine, "total_s": delta("timeFinished", "timeQueued"),
            "usd_est": None if engine is None else engine / 3600 * rate}


def _summarise(level):
    rows = level["workitems"]
    level.update(submitted=sum(r.get("id") is not None for r in rows),
                 succeeded=sum(r.get("status") == "success" and not r.get("poll_timeout") for r in rows),
                 failed=sum(r.get("id") is not None and r.get("status") != "success"
                            and not r.get("poll_timeout") for r in rows),
                 poll_timeouts=sum(bool(r.get("poll_timeout")) for r in rows))
    for field, quantiles in (("queue_s", ("p50", "p95", "max")),
                             ("total_s", ("p50", "p95"))):
        level[field] = {q: _percentile([r.get(field) for r in rows],
                                      {"p50": .5, "p95": .95, "max": 1}[q])
                        for q in quantiles}
    for field in ("engine_s", "usd_est"):
        level[field] = sum(r.get(field) or 0 for r in rows)


def _credentials(secret_id):
    if secret_id:
        import boto3
        result = boto3.client("secretsmanager", region_name="us-east-1").get_secret_value(SecretId=secret_id)
        os.environ["APS_CREDENTIALS_JSON"] = result["SecretString"]
    inline = os.environ.get("APS_CREDENTIALS_JSON", "").strip()
    path = Path(os.environ.get("APS_CRED", "~/.aps/credentials.json")).expanduser()
    if not inline and not path.is_file():
        return None
    creds = json.loads(inline if inline else path.read_text(encoding="utf-8"))
    if not isinstance(creds, dict) or any(
        not isinstance(creds.get(k), str) or not creds[k].strip() or creds[k] == "PASTE_ME"
        for k in ("client_id", "client_secret")
    ):
        raise ValueError("invalid credentials")
    return creds


def _write(path, document, forbidden=()):
    serialised = json.dumps(document, indent=2, allow_nan=False)
    if any(marker in serialised for marker in
           ("client_secret", "X-Amz-", "Signature=", "AWSAccessKeyId", "https://", "http://", *forbidden)
           if marker):
        return False
    path.write_text(serialised + "\n", encoding="utf-8")
    return True


def main(argv=None, client_module=None, now=time.time):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--levels", type=_levels, default="1,2,4,8,16,32")
    parser.add_argument("--spend-cap-usd", type=float, default=10.)
    parser.add_argument("--stop-error-rate", type=float, default=.10)
    parser.add_argument("--stop-queue-p95-s", type=float, default=300.)
    parser.add_argument("--poll-budget-per-min", type=int, default=60)
    parser.add_argument("--dwg", type=Path, default=ROOT / "data/rooftop_demo.dwg")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--aps-secret-id")
    try:
        args = parser.parse_args(argv)
        rate = float(os.environ.get("APS_USD_PER_HR", "10"))
        if not (0 < args.spend_cap_usd <= 50 and 0 < args.stop_error_rate <= 1
                and math.isfinite(args.stop_queue_p95_s) and args.stop_queue_p95_s > 0
                and 1 <= args.poll_budget_per_min <= 120 and math.isfinite(rate) and rate > 0):
            raise ValueError
        if args.live and (args.out is None or not args.dwg.is_file()):
            raise ValueError
        if args.out and (not args.out.parent.is_dir() or args.out.is_dir()
                         or args.out.resolve() == args.dwg.resolve()):
            raise ValueError
    except SystemExit as exc:
        return int(exc.code)
    except (ValueError, OSError):
        print("Invalid ramp input")
        return 2

    os.environ["APS_MAX_CONCURRENCY"] = str(max(args.levels))
    os.environ["APS_POLL_BUDGET_PER_MIN"] = str(args.poll_budget_per_min)
    if not args.live:
        activity = (client_module.activity_qualified(ACTIVITY) if client_module is not None
                    else "<nickname-placeholder>.LeafTool_count_by_layer+prod")
        plan = dict(levels=args.levels, total_workitems=sum(args.levels),
                    estimated_cost_usd=sum(args.levels) * .008, activity_id=activity)
        try:
            if args.out and not _write(args.out, plan):
                return 1
            print(json.dumps(plan, allow_nan=False))
            return 0
        except (ValueError, OSError):
            return 2

    try:
        creds = _credentials(args.aps_secret_id)
    except (ValueError, OSError, KeyError, TypeError):
        return 2
    except Exception:
        return 3
    if creds is None:
        return 3
    forbidden = (creds["client_id"], creds["client_secret"], args.aps_secret_id)
    receipt = dict(schema="leaf.aps-concurrency-ramp.v1", started_at=_stamp(now()),
                   ended_at=None, git_head=None, engine=None, activity_id=None,
                   levels_requested=args.levels, levels=[], stop_reason="unexpected_error",
                   cumulative_usd_est=0., spend_cap_usd=args.spend_cap_usd,
                   poll_budget_per_min=args.poll_budget_per_min)
    began = False
    code = 0
    try:
        client = client_module if client_module is not None else importlib.import_module(
            ".client" if __package__ else "client", package=__package__)
        receipt["engine"] = client.ENGINE
        receipt["activity_id"] = client.activity_qualified(ACTIVITY)
        try:
            receipt["git_head"] = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=ROOT, stderr=subprocess.DEVNULL,
                text=True, timeout=5).strip()
        except (OSError, subprocess.SubprocessError):
            pass
        registry = json.loads((ROOT / "da/registry_live.json").read_text(encoding="utf-8"))
        tool = next(t for t in registry["tools"] if t["engine_op"] == "count_by_layer")
        params = {k: v["default"] for k, v in tool["params"]["properties"].items() if "default" in v}
        nonce = client.run_nonce()
        key = client.ephemeral_input_key(args.dwg.name, tenant_id=TENANT, nonce=nonce)
        client.upload_object(str(args.dwg), key)
        in_url = client.signed_download_url(key, minutes=60)
        highest = None
        for position, n in enumerate(args.levels):
            if position == 0 and n * .01 > args.spend_cap_usd:
                receipt["stop_reason"] = "spend_cap"
                break
            level = dict(n=n, submit_errors=[], workitems=[])
            receipt["levels"].append(level)
            jobs = []
            for index in range(n):
                _, out_url = client.signed_upload_url(
                    f"t/{TENANT}/out/{nonce}_{n}_{index}.json")
                jobs.append(dict(HostDwg={"url": in_url, "verb": "get"},
                                 Params={"url": "data:application/json," + json.dumps(params, separators=(",", ":"))},
                                 Result={"url": out_url, "verb": "put"}))

            ready = threading.Barrier(n)

            def submit(job):
                ready.wait()
                start = now()
                row = dict(submitted_at=_stamp(start))
                try:
                    wi = client.submit_workitem(receipt["activity_id"], job, poll=False, tenant_id=TENANT)
                    row["id"] = wi["id"]
                except Exception as exc:
                    status = getattr(getattr(exc, "response", None), "status_code", None)
                    row["submit_error"] = status if isinstance(status, int) else None
                row["submit_wall_s"] = max(0., now() - start)
                return row

            began = True
            with ThreadPoolExecutor(max_workers=n) as pool:
                level["workitems"] = list(pool.map(submit, jobs))
            level["submit_errors"] = [r["submit_error"] for r in level["workitems"] if "submit_error" in r]

            def poll(row):
                if "id" not in row:
                    return
                status = client._poll_workitem(row["id"])
                row.update(status=status.get("status"), poll_timeout=bool(status.get("_timeout")))
                row.update(_timing(status, rate))

            with ThreadPoolExecutor(max_workers=n) as pool:
                list(pool.map(poll, level["workitems"]))
            _summarise(level)
            receipt["cumulative_usd_est"] += level["usd_est"]
            costs = [r["usd_est"] for r in level["workitems"] if r.get("usd_est") is not None]
            if costs:
                highest = max(costs + ([] if highest is None else [highest]))
            reason = None
            if level["submit_errors"]:
                reason = "submit_error"
            elif level["submitted"] and (level["failed"] + level["poll_timeouts"]) / level["submitted"] > args.stop_error_rate:
                reason = "error_rate"
            elif level["queue_s"]["p95"] is not None and level["queue_s"]["p95"] > args.stop_queue_p95_s:
                reason = "queue_p95"
            elif (receipt["cumulative_usd_est"] +
                  (args.levels[position + 1] * (highest if highest is not None else .01)
                   if position + 1 < len(args.levels) else 0)) > args.spend_cap_usd:
                reason = "spend_cap"
            if reason:
                receipt["stop_reason"] = reason
                break
        else:
            receipt["stop_reason"] = "completed"
    except Exception:
        code = 1 if began else 2
        # Preserve all rows collected before an unexpected polling/timing failure.
        for level in receipt["levels"]:
            _summarise(level)
        receipt["cumulative_usd_est"] = sum(l["usd_est"] for l in receipt["levels"])
    receipt["ended_at"] = _stamp(now())
    try:
        if not _write(args.out, receipt, forbidden):
            return 1
    except (OSError, ValueError, TypeError):
        return 1
    return code


if __name__ == "__main__":
    raise SystemExit(main())
