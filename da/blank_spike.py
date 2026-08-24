#!/usr/bin/env python3
r"""da/blank_spike.py - T3-02: prove APS blank-DWG CREATION end to end.

The protected, BOUNDED spike named by the scope table (T3-02, IN-PATH after
both skeptics refuted the off-path claim). It proves, with receipts:

  1. CREATE  - a DWG is created by APS Design Automation from NOTHING. No
               customer upload, no HostDwg parameter, no OSS input drawing.
  2. VERSION - those bytes are registered as immutable drawing VERSION 1 in the
               real per-tenant versioned store (da/store.py).
  3. READ    - one read-tool round trip (client.extract) runs against that
               stored version and returns Intake JSON.
  4. RECEIPT - one immutable, sha256-stamped job receipt.
  5. COST    - exact per-WorkItem and total cost, attributed to the project
               (tenant + drawing + version) that incurred it.

Fallback discipline (SS15.6): the upload-only fallback is a branch INSIDE this
item and is available ONLY after a proven spike failure. This script therefore
NEVER degrades to upload on error - it fails loudly and writes a FAIL receipt,
so a scope cut stays an operator decision made on evidence.

WHY THE CREATE LEG NEEDS NO INPUT (measured, not assumed)
---------------------------------------------------------
`accoreconsole.exe /s <script>` with no `/i` boots on the engine's own acad.dwt.
Confirmed 2026-08-24 against real AutoCAD 2026 accoreconsole (the pinned engine
family): banner "Drawing created using acad.dwt", SAVEAS produced a valid DWG,
and re-extracting it returned the run's marker layer. See da/blank_lisp.py.

CORRECTNESS GUARD - the scratch-key race (verified at source 2026-08-24)
------------------------------------------------------------------------
da/client.py:509 `extract()` takes its tenant-aware branch ONLY when BOTH
tenant_id and drawing_id are given. Otherwise (:564) the scratch input key is
`in/<int(time.time())>_<basename>` with `_ephemeral_prefix(None) == ""` - bucket
global, 1-SECOND granularity, no uuid. Two runs in the same wall-clock second
with the same basename collide, and OSS PUTs are last-write-wins, so a caller
can download ANOTHER drawing's bytes and get coherent-but-unrelated geometry.
(Reported by session c6477dd5; independently confirmed against client.py here.)

This spike is immune three ways, and does not rely on being run alone:
  (a) the CREATE leg has no input drawing object at all;
  (b) the READ leg passes tenant_id AND drawing_id, so it lands on the
      version-aware branch and references the persistent store key directly;
  (c) every scratch key this script creates carries a per-run uuid, so even the
      legacy key SHAPE could not collide.
And it never trusts a 200: the read result must contain THIS run's unique
marker layer, or the run FAILS. Wrong-drawing bytes cannot pass as success.

Usage:
  python da/blank_spike.py --dry-run          # zero live calls, zero dollars
  python da/blank_spike.py                    # 2 billable WorkItems (~$0.02)

Final stdout line is exactly `BLANK-DWG: PASS` (exit 0) or `BLANK-DWG: FAIL ...`
(exit 1). A JSON receipt is written either way.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import time
import uuid

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
sys.path.insert(0, _HERE)

import blank_lisp  # noqa: E402  (pure sibling: no network, no creds)
import redact      # noqa: E402  (pure sibling: credential stripper)

BLANK_ACTIVITY = os.environ.get("APS_BLANK_ACTIVITY", "LeafBlankCreate")
RECEIPT_PATH = os.path.join(_REPO, "data", "blank_spike_receipt.json")
SCR_REFERENCE_PATH = os.path.join(_REPO, "engine", "blank_create.scr")
DEFAULT_TENANT = os.environ.get("LEAF_SPIKE_TENANT", "leaf-spike-t302")

# Hard lane cap. The spike is authorized for 2 billable WorkItems; this fails
# CLOSED before any submit that would exceed it, so a retry loop or a bad edit
# cannot quietly run up an APS bill.
CAP_WORKITEMS = 4
CAP_USD = 0.25

USD_PER_HR = float(os.environ.get("APS_USD_PER_HR", "10"))

_LEDGER: list[dict] = []


def _import_client(retries: int = 4, wait_s: float = 10.0):
    """Import da/client with retry - sibling lanes edit client.py ADDITIVELY and
    a concurrent write can briefly break the import. The symbols touched here
    are the contract-frozen section-5 set."""
    last = None
    for attempt in range(retries):
        try:
            sys.modules.pop("client", None)
            import client  # noqa: E402
            for sym in ("DA", "ENGINE", "ALIAS", "activity_qualified", "upload_object",
                        "signed_download_url", "signed_upload_url", "finalize_upload",
                        "download_object", "submit_workitem", "extract", "create_bucket",
                        "_engine_seconds", "_auth_headers", "_ephemeral_prefix"):
                getattr(client, sym)
            return client
        except (ImportError, AttributeError, SyntaxError) as e:  # pragma: no cover
            last = e
            if attempt < retries - 1:
                print("[client-import] transient " + type(e).__name__ + ": " + str(e)
                      + "; retry in " + str(wait_s) + "s", file=sys.stderr)
                time.sleep(wait_s)
    raise RuntimeError("could not import da/client after " + str(retries)
                       + " tries: " + str(last))


# --------------------------------------------------------------------------- #
# Cost accounting (exact per WorkItem, attributed to the project)
# --------------------------------------------------------------------------- #
def _usd_est(engine_seconds):
    if engine_seconds is None:
        return None
    return round(engine_seconds / 3600.0 * USD_PER_HR, 4)


def _cumulative_usd() -> float:
    return round(sum((e.get("usd_est") or 0.0) for e in _LEDGER), 4)


def _cap_guard(next_label: str) -> None:
    """Refuse a billable submit BEFORE it happens if the cap would be exceeded."""
    if len(_LEDGER) >= CAP_WORKITEMS:
        raise RuntimeError("LANE CAP: " + str(len(_LEDGER)) + " billable WorkItems "
                           "already >= " + str(CAP_WORKITEMS) + "; refusing "
                           + repr(next_label))
    if _cumulative_usd() >= CAP_USD:
        raise RuntimeError("LANE CAP: cumulative $" + str(_cumulative_usd())
                           + " >= $" + str(CAP_USD) + "; refusing " + repr(next_label))


def redact_report_url(url):
    """Drop the query string from a WorkItem reportUrl before it is recorded.

    APS hands back a PRESIGNED S3 url whose query carries a temporary AWS
    credential (X-Amz-Security-Token, X-Amz-Signature, ...). This receipt is
    committed to the repo, so the query must never land in git. The path is
    kept because it still identifies the report object exactly (owner + work
    item id), and the credential expires within the hour anyway, so nothing of
    diagnostic value is lost.
    """
    if not url or not isinstance(url, str):
        return url
    base, sep, _query = url.partition("?")
    return base + ("?<redacted-presigned>" if sep else "")


def _record(client, label: str, status: dict) -> dict:
    eng = client._engine_seconds(status)
    block = {
        "label": label,
        "id": status.get("id"),
        "status": status.get("status"),
        "reportUrl": redact_report_url(status.get("reportUrl")),
        "engine_seconds": eng,
        "usd_est": _usd_est(eng),
    }
    _LEDGER.append(block)
    return block


class metered_submit:
    """Meter every WorkItem a frozen helper submits, without editing the helper.

    `client.extract()` polls its WorkItem internally and returns only the parsed
    Intake, so a caller cannot see the id or the engine seconds it just paid
    for. Leaving that leg uncosted would make the receipt's cost attribution
    approximate, and T3-02 requires it EXACT.

    So we wrap the `submit_workitem` name in client's own module globals for the
    duration of one call. `extract()` resolves that name at call time, so the
    wrapper intercepts it, records the terminal status, and returns it
    untouched. Nothing in da/client.py changes, and the original is restored in
    a finally block even if the call raises.

    In-process and single-threaded by construction: this is a one-shot CLI, and
    the wrapper is installed only around the spike's own read call.
    """

    def __init__(self, client, label: str):
        self._client = client
        self._label = label
        self._original = None
        self.blocks: list = []

    def __enter__(self):
        client = self._client
        self._original = client.submit_workitem
        original = self._original
        label = self._label
        blocks = self.blocks

        def _wrapped(*args, **kwargs):
            status = original(*args, **kwargs)
            # dry_run bodies are not billable and carry no engine time
            if isinstance(status, dict) and not status.get("_dry_run"):
                blocks.append(_record(client, label, status))
            return status

        client.submit_workitem = _wrapped
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._original is not None:
            self._client.submit_workitem = self._original
        return False


# --------------------------------------------------------------------------- #
# Activity provisioning
# --------------------------------------------------------------------------- #
def _aliased_matching_version(client, requests, spec: dict, headers: dict) -> int:
    """Return a version whose LIVE body matches `spec`, publishing one if not.

    A bare 409 on POST /activities means the Activity NAME already exists - it
    does NOT mean the VERSION the alias points at still runs this spec. Read
    the aliased version back and compare with blank_lisp.activity_body_matches
    (the same comparison server/da/blank_dwg.py's _aliased_matching_version
    uses, imported rather than reimplemented, so the CLI spike and the broker
    producer never disagree on what counts as drift for this one recipe).
    """
    alias = requests.get(
        client.DA + "/activities/" + BLANK_ACTIVITY + "/aliases/" + client.ALIAS,
        headers=headers, timeout=client._HTTP_TIMEOUT)
    if alias.status_code == 200:
        current = alias.json().get("version")
        if isinstance(current, int) and current > 0:
            live = requests.get(
                client.DA + "/activities/" + BLANK_ACTIVITY + "/versions/" + str(current),
                headers=headers, timeout=client._HTTP_TIMEOUT)
            live.raise_for_status()
            if blank_lisp.activity_body_matches(live.json(), spec):
                return current
    elif alias.status_code != 404:
        alias.raise_for_status()

    # No matching live version: publish one. The version body is the activity
    # definition WITHOUT its id (id is the path segment, not a version field).
    body = {k: v for k, v in spec.items() if k != "id"}
    published = requests.post(
        client.DA + "/activities/" + BLANK_ACTIVITY + "/versions",
        headers=headers, data=json.dumps(body), timeout=client._HTTP_TIMEOUT)
    published.raise_for_status()
    version = published.json().get("version")
    if not isinstance(version, int) or version < 1:
        raise RuntimeError("activity version publish returned an unusable version "
                           + repr(version))
    return version


def ensure_blank_activity(client, dry_run: bool = False) -> dict:
    """Idempotently provision the blank-CREATE Activity + its alias.

    409 on POST /activities means the Activity NAME already exists, not that
    the aliased VERSION is still correct. A caller who treated 409 as "done"
    would keep running whatever body was uploaded first, forever - if that
    body's commandLine or parameters ever changed (the per-run .scr argument
    delivery means this is the only drift surface left; see da/blank_lisp.py's
    activity_body_matches docstring for why `settings` changes can't be
    detected), this Activity would silently keep executing the OLD recipe.
    So on 409, read the aliased version's LIVE body back and compare it; only
    a match makes the 409 a no-op, otherwise publish a new version and repoint
    the alias, mirroring server/da/blank_dwg.py's _ensure_activity.
    """
    spec = blank_lisp.blank_activity_spec(BLANK_ACTIVITY, client.ENGINE)
    if dry_run:
        return {"_dry_run": True, "endpoint": "POST " + client.DA + "/activities",
                "activity": BLANK_ACTIVITY, "alias": client.ALIAS, "body": spec}
    import requests
    headers = dict(client._auth_headers())
    headers["Content-Type"] = "application/json"
    r = requests.post(client.DA + "/activities", headers=headers,
                      data=json.dumps(spec), timeout=client._HTTP_TIMEOUT)
    if r.status_code == 409:
        created = False
        version = _aliased_matching_version(client, requests, spec, headers)
    else:
        r.raise_for_status()
        created = True
        version = 1
    a = requests.post(client.DA + "/activities/" + BLANK_ACTIVITY + "/aliases",
                      headers=headers,
                      data=json.dumps({"id": client.ALIAS, "version": version}),
                      timeout=client._HTTP_TIMEOUT)
    if a.status_code == 409:
        # The alias exists and may still point at a stale version. PATCH is the
        # only way to move it - skipping this is how drift survives a repair.
        moved = requests.patch(
            client.DA + "/activities/" + BLANK_ACTIVITY + "/aliases/" + client.ALIAS,
            headers=headers, data=json.dumps({"version": version}),
            timeout=client._HTTP_TIMEOUT)
        moved.raise_for_status()
    elif a.status_code not in (200, 201):
        a.raise_for_status()
    return {"activity": BLANK_ACTIVITY, "created": created, "version": version,
            "alias": client.ALIAS, "alias_ok": True}


# --------------------------------------------------------------------------- #
# Validation of the produced bytes (fail closed, never register rubbish)
# --------------------------------------------------------------------------- #
def validate_dwg_bytes(data: bytes) -> str:
    """Return the DWG version tag, or raise. Checked BEFORE anything is stored.

    A DWG begins with the 6-byte ASCII version tag `AC10xx` (e.g. AC1032). An
    error page, a truncated download, or an empty PUT does not, so this catches
    "the WorkItem said success but the Result object is not a drawing" without
    needing a full parse.
    """
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError("expected bytes, got " + type(data).__name__)
    if len(data) < blank_lisp.MIN_PLAUSIBLE_DWG_BYTES:
        raise ValueError("implausible DWG: " + str(len(data)) + " bytes < "
                         + str(blank_lisp.MIN_PLAUSIBLE_DWG_BYTES) + " floor")
    tag = bytes(data[:6])
    if not tag.startswith(b"AC10"):
        raise ValueError("not a DWG: leading bytes " + repr(tag)
                         + " lack the AC10xx signature")
    return tag.decode("ascii", "replace")


def layer_names(intake: dict) -> list:
    """Every layer name the intake reports, across the shapes extract() returns."""
    intake = intake or {}
    layers = intake.get("layers")
    if layers is None:
        families = intake.get("families") or {}
        layers = families.get("layers") or []
    names = []
    for item in layers or []:
        if isinstance(item, str):
            names.append(item)
        elif isinstance(item, dict):
            n = item.get("name") or item.get("layer")
            if n:
                names.append(str(n))
    return names


def assert_provenance(intake: dict, marker: str) -> list:
    """The read result MUST carry THIS run's marker layer, or the run failed.

    This is the check that makes a 200 meaningless on its own: if the scratch-key
    race (see module docstring) ever returned another drawing's bytes, its layer
    set would not contain this run's uuid-derived marker, and the spike fails
    instead of reporting a confident, wrong PASS.
    """
    names = layer_names(intake)
    if marker not in names:
        raise RuntimeError(
            "PROVENANCE FAIL: marker layer " + repr(marker) + " absent from the read "
            "result (layers=" + repr(names) + "). The bytes read back are NOT the "
            "bytes this run created - suspect the un-scoped scratch-key collision "
            "in da/client.py's legacy extract branch before suspecting new code.")
    return names


# --------------------------------------------------------------------------- #
# Receipt
# --------------------------------------------------------------------------- #
def stamp_receipt(receipt: dict) -> dict:
    """Attach a sha256 over the canonical body - the immutability stamp.

    The digest is computed over the receipt WITHOUT the digest field, so anyone
    can recompute and check it later.
    """
    body = {k: v for k, v in receipt.items() if k != "sha256"}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    receipt["sha256"] = hashlib.sha256(canonical).hexdigest()
    return receipt


def write_receipt(receipt: dict, path: str) -> str:
    stamp_receipt(receipt)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    # newline="" - on Windows the default translates LF to CRLF and churns the
    # whole file in git for a one-field change.
    with open(path, "w", encoding="utf-8", newline="") as fh:
        json.dump(receipt, fh, indent=2, sort_keys=True)
        fh.write("\n")
    return path


def _iso_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
# The spike
# --------------------------------------------------------------------------- #
def run(tenant_id: str, dry_run: bool, receipt_path: str) -> dict:
    client = _import_client()
    import store  # noqa: E402  (sibling da/store.py)

    run_id = uuid.uuid4().hex[:12]
    marker = blank_lisp.new_marker_layer()
    scr = blank_lisp.build_blank_scr(marker)

    receipt = {
        "spike": "drawing.blank_create",
        "scope_item": "T3-02",
        "plan_version_ack": "MASTER-PLAN v9 (2026-08-24)",
        "generated_at": None,
        "dry_run": bool(dry_run),
        "run_id": run_id,
        "marker_layer": marker,
        "engine": client.ENGINE,
        "activity": None,
        "create_workitem": None,
        "read_workitem": None,
        "drawing": None,
        "provenance": None,
        "cost": None,
        "pass": False,
        "failure": None,
    }

    # Per-run scratch keys. Every one carries `run_id`, so the 1-second-
    # granularity collision in the legacy key shape cannot reach this script.
    prefix = client._ephemeral_prefix(tenant_id)
    script_key = prefix + "blank/" + run_id + "/run.scr"
    output_key = prefix + "blank/" + run_id + "/output.dwg"

    activity_id = client.activity_qualified(BLANK_ACTIVITY)
    receipt["activity"] = {
        "name": BLANK_ACTIVITY, "qualified": activity_id, "alias": client.ALIAS,
        "script_object": script_key, "result_object": output_key,
    }

    if dry_run:
        receipt["generated_at"] = _iso_now()
        receipt["activity"]["spec"] = blank_lisp.blank_activity_spec(
            BLANK_ACTIVITY, client.ENGINE)
        receipt["create_workitem"] = {
            "_dry_run": True,
            "activityId": activity_id,
            "arguments": {"Script": {"verb": "get", "object": script_key},
                          "Result": {"verb": "put", "object": output_key}},
        }
        receipt["scr"] = scr
        receipt["cost"] = {"billable_workitems": 0, "usd_total": 0.0,
                           "note": "dry-run: no live call, no spend"}
        receipt["pass"] = True
        write_receipt(receipt, receipt_path)
        return receipt

    tmpdir = tempfile.mkdtemp(prefix="leaf-blank-" + run_id + "-")
    try:
        # ---- provision (idempotent; not billable) --------------------------
        client.create_bucket()
        receipt["activity"]["provision"] = ensure_blank_activity(client)

        # ---- CREATE leg: 1 billable WorkItem, NO input drawing -------------
        scr_local = os.path.join(tmpdir, "run.scr")
        with open(scr_local, "w", encoding="utf-8", newline="") as fh:
            fh.write(scr)
        client.upload_object(scr_local, script_key)

        script_url = client.signed_download_url(script_key)
        up_key, out_url = client.signed_upload_url(output_key)
        arguments = {"Script": {"url": script_url, "verb": "get"},
                     "Result": {"url": out_url, "verb": "put"}}

        _cap_guard("create")
        status = client.submit_workitem(activity_id, arguments, dry_run=False, poll=True,
                                        tenant_id=tenant_id)
        receipt["create_workitem"] = _record(client, "create", status)
        if status.get("status") != "success":
            # Redacted: this exception text reaches logs and stderr, and a raw
            # reportUrl is a presigned S3 url carrying a live AWS credential.
            raise RuntimeError("create WorkItem " + str(status.get("id"))
                               + " status=" + str(status.get("status"))
                               + " report=" + redact.redact_url(status.get("reportUrl")))

        client.finalize_upload(output_key, up_key)
        data = client.download_object(output_key)
        dwg_tag = validate_dwg_bytes(data)
        dwg_sha = hashlib.sha256(data).hexdigest()

        dwg_local = os.path.join(tmpdir, "blank.dwg")
        with open(dwg_local, "wb") as fh:
            fh.write(data)
        receipt["create_workitem"]["output_bytes"] = len(data)
        receipt["create_workitem"]["dwg_version_tag"] = dwg_tag
        receipt["create_workitem"]["sha256"] = dwg_sha

        # ---- VERSION: register as immutable drawing v1 ---------------------
        backend = store.OSSBackend()
        ingested = store.ingest_drawing(backend, tenant_id, dwg_local)
        drawing_id = ingested["drawing_id"]
        version = ingested["version"]
        receipt["drawing"] = {
            "tenant_id": tenant_id, "drawing_id": drawing_id, "version": version,
            "bytes": len(data), "sha256": dwg_sha,
            "version_key": store.drawing_version_key(tenant_id, drawing_id, version),
        }

        # ---- READ leg: 1 billable WorkItem, version-aware (race-immune) ----
        _cap_guard("read")
        # The read leg goes through the PRODUCT path, client.extract(), so this
        # proves the shipped read tool - not a bespoke re-implementation of it.
        # metered_submit captures the WorkItem that call submits so the leg is
        # costed EXACTLY rather than reported as unknown.
        with metered_submit(client, "read") as metered:
            intake = client.extract(dwg_local, dry_run=False, tenant_id=tenant_id,
                                    drawing_id=drawing_id, version=version,
                                    backend=backend)
        if metered.blocks:
            read_block = metered.blocks[-1]
        else:
            # extract() returned without submitting anything we could see. Record
            # it as unknown rather than silently costing it at zero.
            read_block = {"label": "read", "status": "success",
                          "engine_seconds": None, "usd_est": None,
                          "note": "no WorkItem observed for the read leg; "
                                  "cost is UNKNOWN, not zero"}
            _LEDGER.append(read_block)
        receipt["read_workitem"] = read_block

        names = assert_provenance(intake, marker)
        receipt["provenance"] = {
            "marker_layer": marker, "layers_seen": names, "asserted": True,
            "geometry": (intake or {}).get("geometry"),
        }

        # ---- COST: exact, attributed to the project ------------------------
        receipt["cost"] = {
            "usd_per_hr": USD_PER_HR,
            "billable_workitems": len(_LEDGER),
            "per_workitem": list(_LEDGER),
            "usd_total": _cumulative_usd(),
            "attribution": {
                "tenant_id": tenant_id, "drawing_id": drawing_id,
                "version": version, "run_id": run_id,
                "activity": activity_id,
            },
            "cap": {"max_workitems": CAP_WORKITEMS, "max_usd": CAP_USD},
        }
        receipt["pass"] = True
        return receipt
    finally:
        receipt["generated_at"] = _iso_now()
        if receipt.get("cost") is None:
            receipt["cost"] = {
                "usd_per_hr": USD_PER_HR,
                "billable_workitems": len(_LEDGER),
                "per_workitem": list(_LEDGER),
                "usd_total": _cumulative_usd(),
                "attribution": {"tenant_id": tenant_id, "run_id": run_id},
                "cap": {"max_workitems": CAP_WORKITEMS, "max_usd": CAP_USD},
            }
        write_receipt(receipt, receipt_path)
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)


def write_scr_reference(path: str = SCR_REFERENCE_PATH) -> str:
    """Emit the readable .scr reference with a FIXED placeholder marker.

    The live script's marker is per-run; this file exists so a human can read
    the recipe in the repo. It is documentation, never the executed artifact.
    """
    scr = blank_lisp.build_blank_scr("LEAF_BLANK_XXXXXXXXXXXX")
    header = (
        "; =========================================================================\n"
        "; blank_create.scr  -  Leaf blank-DWG CREATE recipe (READABLE REFERENCE)\n"
        "; -------------------------------------------------------------------------\n"
        "; Generated by da/blank_spike.py from da/blank_lisp.build_blank_scr().\n"
        "; The DA Activity LeafBlankCreate runs the per-run version of these lines\n"
        "; headless via:  accoreconsole /s <this script>   -- note NO /i: there is\n"
        "; no input drawing, the engine opens its own acad.dwt.\n"
        "; LEAF_BLANK_XXXXXXXXXXXX below is a PLACEHOLDER. The live script carries a\n"
        "; per-run uuid-derived marker layer, which the read round-trip asserts on so\n"
        "; another drawing's bytes can never be mistaken for this run's output.\n"
        "; Do not edit by hand.\n"
        "; =========================================================================\n"
    )
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(header + scr)
    return path


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="T3-02 APS blank-DWG creation spike")
    ap.add_argument("--dry-run", action="store_true",
                    help="zero live calls, zero dollars")
    ap.add_argument("--tenant-id", default=DEFAULT_TENANT)
    ap.add_argument("--receipt", default=RECEIPT_PATH)
    ap.add_argument("--write-scr-reference", action="store_true",
                    help="regenerate engine/blank_create.scr and exit")
    args = ap.parse_args(argv)

    if args.write_scr_reference:
        print(write_scr_reference())
        return 0

    try:
        receipt = run(args.tenant_id, args.dry_run, args.receipt)
    except Exception as exc:  # noqa: BLE001 - a spike reports, it never dies silently
        fail = {
            "spike": "drawing.blank_create", "scope_item": "T3-02",
            "plan_version_ack": "MASTER-PLAN v9 (2026-08-24)",
            "generated_at": _iso_now(), "dry_run": bool(args.dry_run),
            "pass": False, "failure": type(exc).__name__ + ": " + str(exc),
            "cost": {"billable_workitems": len(_LEDGER),
                     "per_workitem": list(_LEDGER),
                     "usd_total": _cumulative_usd()},
        }
        try:
            write_receipt(fail, args.receipt)
        except Exception:  # pragma: no cover
            pass
        print("BLANK-DWG: FAIL " + type(exc).__name__ + ": " + str(exc))
        return 1

    if receipt.get("pass"):
        print("BLANK-DWG: PASS")
        return 0
    print("BLANK-DWG: FAIL " + str(receipt.get("failure")))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
