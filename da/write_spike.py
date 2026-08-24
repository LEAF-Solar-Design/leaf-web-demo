#!/usr/bin/env python3
r"""da/write_spike.py — prove the APS drawing.write path (add + delete an entity).

De-risk spike (NOT productization). Proves — cheaply, with LISP only — that an
Autodesk APS Design Automation WorkItem can INPUT a DWG, MUTATE its geometry
(add one entity, delete one entity), and OUTPUT a new DWG, verified by
re-extracting the output and confirming both the ADD and the DELETE.

Reuses da/client.py's FROZEN public helpers (never edits them):
  auth_token, activity_qualified, upload_object, signed_download_url,
  signed_upload_url, finalize_upload, download_object, submit_workitem,
  extract, parse_text, _engine_seconds, create_bucket, and constants
  DA / ENGINE / ALIAS / HOSTDWG_LOCALNAME / EXTRACT_ACTIVITY.

Live cost: exactly 3 billable WorkItems per PASS run (extract-input, write,
extract-output) ~= $0.02 total. A hard lane cap (10 WorkItems / $0.50) aborts
before any submit that would exceed it.

Usage:
  python da/write_spike.py --dry-run data/rooftop_demo.dwg   # zero live calls
  python da/write_spike.py data/rooftop_demo.dwg             # 3 billable WorkItems

Final stdout line is exactly `WRITE-PATH: PASS` (exit 0) or `WRITE-PATH: FAIL ...`
(exit 1). A JSON receipt is written to data/write_spike_receipt.json.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time

# --------------------------------------------------------------------------- #
# Paths / constants
# --------------------------------------------------------------------------- #
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
sys.path.insert(0, _HERE)  # resolve sibling da/ imports (client, write_lisp, ...)

WRITE_ACTIVITY = "LeafWriteProbe"
PROBE_LAYER = "LEAF_WRITE_PROBE"
RECEIPT_PATH = os.path.join(_REPO, "data", "write_spike_receipt.json")
SCR_REFERENCE_PATH = os.path.join(_REPO, "engine", "write_probe.scr")

# Hard lane cap (the executor is authorized for ~3 billables; this fails closed).
CAP_WORKITEMS = 10
CAP_USD = 0.50

# usd estimate model (mirrors client.run_tool): engine-seconds * $/hr.
USD_PER_HR = float(os.environ.get("APS_USD_PER_HR", "10"))

# Billable-WorkItem ledger (in-process). Each entry: dict with id/status/... .
_LEDGER: list[dict] = []


# --------------------------------------------------------------------------- #
# Sibling-safe client import (another lane edits client.py ADDITIVELY)
# --------------------------------------------------------------------------- #
def _import_client(retries: int = 4, wait_s: float = 10.0):
    """Import da/client with retry — a concurrent additive edit can briefly break
    the import; the legacy no-kwarg interface we use is contract-frozen."""
    last = None
    for attempt in range(retries):
        try:
            sys.modules.pop("client", None)
            import client  # noqa: E402
            # touch the frozen symbols we depend on so a partial file is caught here
            for sym in ("DA", "ENGINE", "ALIAS", "HOSTDWG_LOCALNAME", "EXTRACT_ACTIVITY",
                        "activity_qualified", "upload_object", "signed_download_url",
                        "signed_upload_url", "finalize_upload", "download_object",
                        "submit_workitem", "extract", "parse_text", "_engine_seconds",
                        "create_bucket", "_auth_headers",
                        # collision-proof scratch keys (PR #782) — this driver
                        # bypassed them and built `in/<ts>_<base>` by hand.
                        "run_nonce", "ephemeral_input_key", "ephemeral_output_key"):
                getattr(client, sym)
            return client
        except (ImportError, AttributeError, SyntaxError) as e:  # pragma: no cover
            last = e
            if attempt < retries - 1:
                print(f"[client-import] transient {type(e).__name__}: {e}; retry in {wait_s}s",
                      file=sys.stderr)
                time.sleep(wait_s)
    raise RuntimeError(f"could not import da/client after {retries} tries: {last}")


client = _import_client()
import write_lisp  # noqa: E402  (our sibling, safe)
# Presigned-url redaction, shared with da/blank_spike.py. data/write_spike_receipt.json
# is COMMITTED to a public repo, so every reportUrl must lose its query BEFORE it
# is written, never in a later cleanup pass.
from redact import redact_report_url  # noqa: E402  (pure sibling: no network, no creds)

import requests  # noqa: E402


# --------------------------------------------------------------------------- #
# Cost helpers
# --------------------------------------------------------------------------- #
def _usd_est(engine_seconds):
    if engine_seconds is None:
        return None
    return round(engine_seconds / 3600.0 * USD_PER_HR, 4)


def _cumulative_usd() -> float:
    return round(sum((e.get("usd_est") or 0.0) for e in _LEDGER), 4)


def _cap_guard(next_label: str):
    """Fail closed BEFORE a billable submit if the lane cap would be exceeded."""
    if len(_LEDGER) >= CAP_WORKITEMS:
        raise RuntimeError(f"LANE CAP: {len(_LEDGER)} billable WorkItems already "
                           f">= {CAP_WORKITEMS}; refusing '{next_label}'")
    if _cumulative_usd() >= CAP_USD:
        raise RuntimeError(f"LANE CAP: cumulative ${_cumulative_usd()} >= ${CAP_USD}; "
                           f"refusing '{next_label}'")


def _record(label: str, status: dict) -> dict:
    """Append a billable WorkItem to the ledger and return its receipt block."""
    eng = client._engine_seconds(status)
    block = {
        "label": label,
        "id": status.get("id"),
        "status": status.get("status"),
        # REDACTED AT WRITE TIME: this block goes straight into the committed
        # receipt, so the presigned query never gets a chance to reach git.
        "reportUrl": redact_report_url(status.get("reportUrl")),
        "engine_seconds": eng,
        "usd_est": _usd_est(eng),
    }
    _LEDGER.append(block)
    print(f"[workitem] {label}: id={block['id']} status={block['status']} "
          f"engine_s={eng} usd={block['usd_est']} (cum ${_cumulative_usd()})")
    return block


# --------------------------------------------------------------------------- #
# Activity spec + provisioning (Activity/alias POSTs are NOT billable)
# --------------------------------------------------------------------------- #
def write_activity_spec() -> dict:
    """POST /activities body for the LeafWriteProbe write Activity (pure-LISP)."""
    return {
        "id": WRITE_ACTIVITY,
        "engine": client.ENGINE,
        "commandLine": [
            r'$(engine.path)\accoreconsole.exe /i "$(args[HostDwg].path)" '
            r'/s "$(settings[script].path)"'
        ],
        "parameters": {
            "HostDwg": {"verb": "get", "required": True,
                        "localName": client.HOSTDWG_LOCALNAME},           # input.dwg
            "Result": {"verb": "put", "required": True,
                       "localName": write_lisp.OUT_LOCALNAME},            # output.dwg
        },
        "settings": {"script": {"value": write_lisp.build_write_scr()}},
        "description": "Leaf drawing.write spike: delete one LWPOLYLINE + add a "
                       "probe LWPOLYLINE on LEAF_WRITE_PROBE, save output.dwg.",
    }


def _post(path: str, body: dict):
    return requests.post(f"{client.DA}{path}",
                         headers={**client._auth_headers(), "Content-Type": "application/json"},
                         data=json.dumps(body), timeout=60)


def _patch(path: str, body: dict):
    return requests.patch(f"{client.DA}{path}",
                          headers={**client._auth_headers(), "Content-Type": "application/json"},
                          data=json.dumps(body), timeout=60)


def provision() -> str:
    """Create/advance the LeafWriteProbe Activity carrying the CURRENT script, and
    point the prod alias at that version (closing provision_live's 409-alias gap:
    on re-runs we POST a new version + PATCH the alias, so an edited script runs)."""
    spec = write_activity_spec()
    name = spec["id"]
    r = _post("/activities", spec)
    if r.status_code in (200, 201):
        version = r.json().get("version", 1)
        print(f"[activity] created {name} v{version}")
    elif r.status_code == 409:
        vbody = {k: v for k, v in spec.items() if k != "id"}
        rv = _post(f"/activities/{name}/versions", vbody)
        if rv.status_code not in (200, 201):
            raise RuntimeError(f"activity version {name} failed {rv.status_code}: {rv.text[:300]}")
        version = rv.json().get("version")
        print(f"[activity] {name} exists (409); created v{version}")
    else:
        raise RuntimeError(f"activity {name} failed {r.status_code}: {r.text[:300]}")

    a = _post(f"/activities/{name}/aliases", {"id": client.ALIAS, "version": version})
    if a.status_code in (200, 201):
        print(f"[alias] {name}+{client.ALIAS} -> v{version}")
    elif a.status_code == 409:
        p = _patch(f"/activities/{name}/aliases/{client.ALIAS}", {"version": version})
        if p.status_code not in (200, 201):
            raise RuntimeError(f"alias PATCH {name} failed {p.status_code}: {p.text[:300]}")
        print(f"[alias] {name}+{client.ALIAS} PATCHed -> v{version}")
    else:
        raise RuntimeError(f"alias {name} failed {a.status_code}: {a.text[:300]}")
    return client.activity_qualified(name)


# --------------------------------------------------------------------------- #
# Extract that ALSO returns the WorkItem status (for honest engine_seconds).
# Faithful copy of client.extract()'s LIVE path (client.py ~364-376) using only
# client's frozen public primitives — client.extract() itself hides the status,
# and the receipt must carry per-WorkItem engine_seconds/usd_est.
# --------------------------------------------------------------------------- #
def _extract_with_status(dwg_local_path: str):
    dwg_name = os.path.basename(dwg_local_path)
    # COLLISION-PROOF scratch keys (PR #782). The old shape was
    # `in/<int(time.time())>_<base>`, built by hand and bypassing client's
    # helpers: ONE-SECOND resolution, so two runs of the same basename in the
    # same second wrote the same OSS object, and OSS PUTs are last-write-wins.
    # The loser then downloaded the winner's bytes — a paid WorkItem returning
    # coherent-but-unrelated geometry. `run_nonce()` is uuid4-derived, NEVER
    # time-derived, because a clock cannot fix a clock-resolution collision.
    # One nonce for the whole extraction keeps its input and output keys
    # greppable together in OSS.
    nonce = client.run_nonce()
    input_key = client.ephemeral_input_key(dwg_name, nonce=nonce)
    output_key = client.ephemeral_output_key(dwg_name, nonce=nonce,
                                             suffix=".families.txt")
    activity_id = client.activity_qualified(client.EXTRACT_ACTIVITY)
    client.upload_object(dwg_local_path, input_key)
    in_url = client.signed_download_url(input_key)
    up_key, out_url = client.signed_upload_url(output_key)
    arguments = {"HostDwg": {"url": in_url, "verb": "get"},
                 "Result": {"url": out_url, "verb": "put"}}
    status = client.submit_workitem(activity_id, arguments, dry_run=False, poll=True)
    if status.get("status") != "success":
        # redacted: run() copies str(e) into receipt["error"], so an unredacted
        # reportUrl here is the same committed-credential leak by another route.
        raise RuntimeError(f"extract WorkItem {status.get('id')} status={status.get('status')} "
                           f"report={redact_report_url(status.get('reportUrl'))}")
    client.finalize_upload(output_key, up_key)
    families = client.download_object(output_key).decode("utf-8", "replace")
    return client.parse_text(families, dwg_local_path), status


# --------------------------------------------------------------------------- #
# Dry-run (zero live calls) — brief mandates running this FIRST.
# --------------------------------------------------------------------------- #
def dry_run(dwg: str) -> int:
    scr = write_lisp.build_write_scr()
    _write_scr_reference(scr)
    spec = write_activity_spec()
    # Redacted WorkItem body with placeholder (never-minted) signed urls.
    placeholder_args = {
        "HostDwg": {"url": "https://<signed-s3-GET-url minted at run time>", "verb": "get"},
        "Result": {"url": "https://<signed-s3-PUT-url minted at run time>", "verb": "put"},
    }
    wi_body = {"activityId": f"<owner>.{WRITE_ACTIVITY}+{client.ALIAS}",
               "arguments": placeholder_args}

    print("=" * 72)
    print("DRY-RUN — no bucket / upload / activity / workitem calls performed")
    print("=" * 72)
    print(f"\ninput dwg           : {dwg}")
    print(f"engine              : {client.ENGINE}")
    print(f"write activity id   : <owner>.{WRITE_ACTIVITY}+{client.ALIAS}")
    print(f"extract activity id : <owner>.{client.EXTRACT_ACTIVITY}+{client.ALIAS}")
    print(f"probe layer         : {PROBE_LAYER}")
    print(f"result localName    : {write_lisp.OUT_LOCALNAME}")
    print(f"scr reference dumped: {SCR_REFERENCE_PATH}")
    print("\n--- POST /activities body (LeafWriteProbe) ---")
    print(json.dumps(spec, indent=2))
    print("\n--- WorkItem POST body (write; urls redacted) ---")
    print(json.dumps(wi_body, indent=2))
    print("\n--- built mutate .scr (settings[script].value; \\r\\n shown as lines) ---")
    for i, line in enumerate(scr.split("\r\n"), 1):
        if line:
            print(f"{i:>2}| {line}")
    print("\nplanned billable WorkItems: extract(input), write, extract(output) = 3 "
          f"(lane cap {CAP_WORKITEMS} / ${CAP_USD})")
    print("\nDRY-RUN complete. Re-run WITHOUT --dry-run to execute the live spike.")
    return 0


def _write_scr_reference(scr: str):
    """Dump the built mutate script to engine/write_probe.scr (readable reference).

    Header comments document intent; the operative (progn ...) lines are the exact
    output of write_lisp.build_write_scr() (what the Activity actually runs)."""
    header = (
        "; ==========================================================================\r\n"
        "; write_probe.scr  -  Leaf drawing.write spike mutate recipe (READABLE REF)\r\n"
        "; --------------------------------------------------------------------------\r\n"
        "; Generated by da/write_spike.py from da/write_lisp.build_write_scr(). The DA\r\n"
        "; Activity LeafWriteProbe runs these (progn ...) lines headless via\r\n"
        ";   accoreconsole /i input.dwg /s <this script>\r\n"
        "; Order is load-bearing: DELETE one LWPOLYLINE, MAKE layer LEAF_WRITE_PROBE,\r\n"
        "; ADD a closed LWPOLYLINE on it, SAVEAS output.dwg, QUIT. Re-extracting\r\n"
        "; output.dwg proves the add (probe layer + polyline present) and the delete\r\n"
        "; (exactly one original polyline handle gone).  Do not edit by hand.\r\n"
        "; ==========================================================================\r\n"
    )
    with open(SCR_REFERENCE_PATH, "w", newline="") as fh:
        fh.write(header + scr)


# --------------------------------------------------------------------------- #
# Live run
# --------------------------------------------------------------------------- #
def run(dwg: str) -> int:
    receipt = {
        "spike": "drawing.write",
        "input_dwg": os.path.relpath(dwg, _REPO).replace("\\", "/"),
        "probe_layer": PROBE_LAYER,
        "generated_at": client.iso_now(),
        "engine": client.ENGINE,
        "pass": False,
    }
    try:
        # Ensure the bucket the imported client resolves EXISTS (sibling may have
        # changed the default bucket stem); create_bucket handles 409 = exists.
        bres = client.create_bucket()
        print(f"[bucket] {client.bucket_key()} -> {bres}")

        activity_id = provision()
        receipt["activity_id"] = activity_id

        # ---- billable #1: extract INPUT ----------------------------------
        _cap_guard("extract(input)")
        base, st_in = _extract_with_status(dwg)
        receipt["extract_input_workitem"] = _record("extract(input)", st_in)
        H_in = {p["handle"] for p in base["polylines"] if p.get("handle")}
        L_in = set(base["layers"])
        receipt["len_H_in"] = len(H_in)
        print(f"[input ] polylines={len(base['polylines'])} unique_handles={len(H_in)} "
              f"layers={len(L_in)} probe_in_input={PROBE_LAYER in L_in}")

        # ---- upload input + reserve output url ---------------------------
        # Same collision-proof keys as _extract_with_status: one uuid4-derived
        # per-run nonce, never a bare `int(time.time())` stamp.
        nonce = client.run_nonce()
        in_key = client.ephemeral_input_key(os.path.basename(dwg), nonce=nonce)
        out_key = client.ephemeral_output_key("output", nonce=nonce, suffix=".dwg")
        client.upload_object(dwg, in_key)
        in_url = client.signed_download_url(in_key)
        up_key, out_url = client.signed_upload_url(out_key)

        # ---- billable #2: the WRITE WorkItem -----------------------------
        _cap_guard("write")
        w_args = {"HostDwg": {"url": in_url, "verb": "get"},
                  "Result": {"url": out_url, "verb": "put"}}
        st_w = client.submit_workitem(activity_id, w_args, dry_run=False, poll=True)
        receipt["write_workitem"] = _record("write", st_w)
        if st_w.get("status") != "success":
            raise RuntimeError(f"write WorkItem {st_w.get('id')} status={st_w.get('status')} "
                               f"report={redact_report_url(st_w.get('reportUrl'))}")
        client.finalize_upload(out_key, up_key)
        out_bytes = client.download_object(out_key)
        if not out_bytes:
            raise RuntimeError("write produced 0-byte output.dwg")
        tmp = tempfile.NamedTemporaryFile(prefix="leaf_write_out_", suffix=".dwg", delete=False)
        tmp.write(out_bytes)
        tmp.close()
        print(f"[output] downloaded {len(out_bytes)} bytes -> {tmp.name}")
        receipt["output_dwg_bytes"] = len(out_bytes)

        # ---- billable #3: extract OUTPUT ---------------------------------
        _cap_guard("extract(output)")
        outx, st_out = _extract_with_status(tmp.name)
        receipt["extract_output_workitem"] = _record("extract(output)", st_out)
        H_out = {p["handle"] for p in outx["polylines"] if p.get("handle")}
        L_out = set(outx["layers"])
        receipt["len_H_out"] = len(H_out)

        # ---- assertions --------------------------------------------------
        add_ok = (PROBE_LAYER in L_out) and (PROBE_LAYER not in L_in)
        probe_poly_ok = any(p.get("layer") == PROBE_LAYER for p in outx["polylines"])
        deleted = H_in - H_out
        delete_ok = (len(deleted) == 1)
        deleted_handle = next(iter(deleted)) if deleted else None

        receipt.update({
            "add_confirmed": bool(add_ok and probe_poly_ok),
            "add_probe_layer_in_output": PROBE_LAYER in L_out,
            "add_probe_layer_absent_from_input": PROBE_LAYER not in L_in,
            "add_probe_polyline_present": probe_poly_ok,
            "delete_confirmed": bool(delete_ok),
            "deleted_handle": deleted_handle,
            "deleted_count": len(deleted),
            "cumulative_usd_est": _cumulative_usd(),
            "billable_workitem_count": len(_LEDGER),
            "output_extract_polylines": len(outx["polylines"]),
        })

        print(f"[verify] add={receipt['add_confirmed']} "
              f"(probe_in_out={PROBE_LAYER in L_out}, probe_not_in_in={PROBE_LAYER not in L_in}, "
              f"probe_poly={probe_poly_ok}) delete={delete_ok} "
              f"deleted_handle={deleted_handle} |H_in-H_out|={len(deleted)}")

        passed = bool(add_ok and probe_poly_ok and delete_ok)
        receipt["pass"] = passed
        _write_receipt(receipt)

        if not passed:
            reasons = []
            if not add_ok:
                reasons.append(f"ADD not confirmed (probe_in_out={PROBE_LAYER in L_out}, "
                               f"probe_absent_in={PROBE_LAYER not in L_in})")
            if not probe_poly_ok:
                reasons.append("no polyline on probe layer in output extract")
            if not delete_ok:
                reasons.append(f"DELETE not confirmed (|H_in-H_out|={len(deleted)}, expected 1)")
            print(f"WRITE-PATH: FAIL — {'; '.join(reasons)}")
            return 1

        print("WRITE-PATH: PASS")
        return 0

    except Exception as e:
        receipt["error"] = f"{type(e).__name__}: {e}"
        receipt["cumulative_usd_est"] = _cumulative_usd()
        receipt["billable_workitem_count"] = len(_LEDGER)
        try:
            _write_receipt(receipt)
        except Exception:
            pass
        print(f"WRITE-PATH: FAIL — {type(e).__name__}: {e}")
        return 1


def _write_receipt(receipt: dict):
    os.makedirs(os.path.dirname(RECEIPT_PATH), exist_ok=True)
    with open(RECEIPT_PATH, "w") as fh:
        json.dump(receipt, fh, indent=2)
    print(f"[receipt] wrote {RECEIPT_PATH}")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv) -> int:
    args = [a for a in argv if a not in ("--dry-run",)]
    is_dry = "--dry-run" in argv
    if len(args) < 1:
        print("usage: python da/write_spike.py [--dry-run] <dwg>", file=sys.stderr)
        return 2
    dwg = args[0]
    if not is_dry and not os.path.exists(dwg):
        print(f"WRITE-PATH: FAIL — input dwg not found: {dwg}")
        return 1
    return dry_run(dwg) if is_dry else run(dwg)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
