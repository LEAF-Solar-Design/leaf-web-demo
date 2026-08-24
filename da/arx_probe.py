#!/usr/bin/env python3
r"""da/arx_probe.py — prove the APS COMPILED (.NET/ObjectARX) execution path.

Closes the last open MISSION-ledger bet: that our OWN compiled AutoCAD plugin
code (a C# DA AppBundle) executes on APS Design Automation — not just headless
LISP. The LISP write path is already proven (da/write_spike.py +
data/write_spike_receipt.json). This driver does the ONE new thing: upload the
compiled LeafWriteTools AppBundle, create an Activity that loads it, run a
WorkItem invoking its LEAFWRITEPROBE command, and verify the compiled code ran.

Payload: engine/appbundle-write/LeafWriteTools.zip (built here — net8.0-windows,
AutoCAD 2026 managed refs Private=false, engine Autodesk.AutoCAD+26_0). Locally
proven in accoreconsole 2026 via NETLOAD (deleted a polyline + added one on
LEAF_WRITE_PROBE + SaveAs output.dwg); DA loads it via /al + PackageContents
LoadOnCommandInvocation autoload.

Oracle (identical to the LISP spike, now via compiled code): re-extract output.dwg
and assert LEAF_WRITE_PROBE present (absent from input) with a polyline on it
(the ADD), and exactly one original polyline handle gone (the DELETE).

Reuses da/client.py's FROZEN public helpers (never edits them):
  _auth_headers, DA, ENGINE, ALIAS, HOSTDWG_LOCALNAME, EXTRACT_ACTIVITY,
  nickname, activity_qualified, create_bucket, bucket_key, upload_object,
  signed_download_url, signed_upload_url, finalize_upload, download_object,
  submit_workitem, parse_text, _engine_seconds, iso_now.

Live cost: 3 billable WorkItems per PASS (extract-input, arx-run, extract-output)
~= $0.02. AppBundle/Activity/alias POSTs are NOT billable. Hard lane cap
(8 WorkItems / $0.50) aborts before any submit that would exceed it.

Usage:
  python da/arx_probe.py --dry-run data/rooftop_demo.dwg   # zero live calls
  python da/arx_probe.py data/rooftop_demo.dwg             # 3 billable WorkItems

Final stdout line is exactly `ARX-PATH: PASS` (exit 0) or `ARX-PATH: FAIL ...`
(exit 1). A JSON receipt is written to data/arx_probe_receipt.json.
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
sys.path.insert(0, _HERE)  # resolve sibling da/ imports (client, ...)

APPBUNDLE_ID = "LeafWriteTools"          # POST /appbundles id == PackageContents Name
ACTIVITY_ID = "LeafArxProbe"             # NEW Activity (distinct from LeafWriteProbe/LeafExtract)
PROBE_LAYER = "LEAF_WRITE_PROBE"
OUT_LOCALNAME = "output.dwg"             # matches LeafWriteTools.cs Database.SaveAs("output.dwg")
ZIP_PATH = os.path.join(_REPO, "engine", "appbundle-write", "LeafWriteTools.zip")
RECEIPT_PATH = os.path.join(_REPO, "data", "arx_probe_receipt.json")
TARGET_FRAMEWORK = "net8.0-windows"

# .scr the Activity runs: invoke the compiled command, then QUIT (CRLF, proven
# byte-for-byte in the local accoreconsole 2026 NETLOAD test).
ARX_SCRIPT = 'LEAFWRITEPROBE\r\n(command "_.QUIT" "_Y")\r\n'

# Hard lane cap (executor authorized for ~3 billables; fails closed).
CAP_WORKITEMS = 8
CAP_USD = 0.50
USD_PER_HR = float(os.environ.get("APS_USD_PER_HR", "10"))

_LEDGER: list[dict] = []


# --------------------------------------------------------------------------- #
# Sibling-safe client import (mirror write_spike.py)
# --------------------------------------------------------------------------- #
def _import_client(retries: int = 4, wait_s: float = 10.0):
    last = None
    for attempt in range(retries):
        try:
            sys.modules.pop("client", None)
            import client  # noqa: E402
            for sym in ("DA", "ENGINE", "ALIAS", "HOSTDWG_LOCALNAME", "EXTRACT_ACTIVITY",
                        "nickname", "activity_qualified", "create_bucket", "bucket_key",
                        "upload_object", "signed_download_url", "signed_upload_url",
                        "finalize_upload", "download_object", "submit_workitem",
                        "parse_text", "_engine_seconds", "_auth_headers", "iso_now"):
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
import redact  # noqa: E402  (our sibling, safe) -- credential stripper
import requests  # noqa: E402


# --------------------------------------------------------------------------- #
# Cost helpers (mirror write_spike.py)
# --------------------------------------------------------------------------- #
def _usd_est(engine_seconds):
    if engine_seconds is None:
        return None
    return round(engine_seconds / 3600.0 * USD_PER_HR, 4)


def _cumulative_usd() -> float:
    return round(sum((e.get("usd_est") or 0.0) for e in _LEDGER), 4)


def _cap_guard(next_label: str):
    if len(_LEDGER) >= CAP_WORKITEMS:
        raise RuntimeError(f"LANE CAP: {len(_LEDGER)} billable WorkItems already "
                           f">= {CAP_WORKITEMS}; refusing '{next_label}'")
    if _cumulative_usd() >= CAP_USD:
        raise RuntimeError(f"LANE CAP: cumulative ${_cumulative_usd()} >= ${CAP_USD}; "
                           f"refusing '{next_label}'")


def _record(label: str, status: dict) -> dict:
    eng = client._engine_seconds(status)
    block = {
        "label": label,
        "id": status.get("id"),
        "status": status.get("status"),
        # Redacted at WRITE time: a raw reportUrl is a presigned S3 url whose
        # query string carries a LIVE one-hour AWS credential. See da/redact.py.
        **redact.report_url_fields(status),
        "engine_seconds": eng,
        "usd_est": _usd_est(eng),
    }
    _LEDGER.append(block)
    print(f"[workitem] {label}: id={block['id']} status={block['status']} "
          f"engine_s={eng} usd={block['usd_est']} (cum ${_cumulative_usd()})")
    return block


# --------------------------------------------------------------------------- #
# HTTP helpers on the DA host (Activity/AppBundle POSTs are NOT billable)
# --------------------------------------------------------------------------- #
def _post(path: str, body: dict):
    return requests.post(f"{client.DA}{path}",
                         headers={**client._auth_headers(), "Content-Type": "application/json"},
                         data=json.dumps(body), timeout=60)


def _patch(path: str, body: dict):
    return requests.patch(f"{client.DA}{path}",
                          headers={**client._auth_headers(), "Content-Type": "application/json"},
                          data=json.dumps(body), timeout=60)


# --------------------------------------------------------------------------- #
# AppBundle spec + provisioning (upload the compiled .zip)
# --------------------------------------------------------------------------- #
def appbundle_spec() -> dict:
    return {
        "id": APPBUNDLE_ID,
        "engine": client.ENGINE,
        "description": ("Leaf drawing.write COMPILED probe (C#/ObjectARX): delete one "
                        "polyline + add probe polyline on LEAF_WRITE_PROBE + SaveAs "
                        "output.dwg. net8.0-windows, AutoCAD 2026 refs."),
    }


def _upload_bundle_zip(upload_params: dict, zip_path: str) -> int:
    """Form-POST the AppBundle .zip to the signed S3 endpoint returned by DA.

    The formData fields MUST precede the `file` field (S3 policy), so build an
    ordered list of tuples with the file LAST.
    """
    endpoint = upload_params["endpointURL"]
    form = upload_params["formData"]
    fields = [(k, (None, str(v))) for k, v in form.items()]
    with open(zip_path, "rb") as fh:
        data = fh.read()
    fields.append(("file", (os.path.basename(zip_path), data, "application/octet-stream")))
    r = requests.post(endpoint, files=fields, timeout=300)
    if r.status_code not in (200, 201, 204):
        raise RuntimeError(f"S3 appbundle upload failed {r.status_code}: {r.text[:300]}")
    return r.status_code


def provision_appbundle() -> tuple[str, int]:
    """Create/advance the LeafWriteTools AppBundle, upload the current zip, and
    point the prod alias at that version. Returns (qualified_id, version)."""
    if not os.path.exists(ZIP_PATH):
        raise RuntimeError(f"AppBundle zip missing: {ZIP_PATH} (build engine/appbundle-write first)")
    spec = appbundle_spec()
    r = _post("/appbundles", spec)
    if r.status_code in (200, 201):
        j = r.json()
        version = j.get("version", 1)
        _upload_bundle_zip(j["uploadParameters"], ZIP_PATH)
        print(f"[appbundle] created {APPBUNDLE_ID} v{version}; uploaded {os.path.basename(ZIP_PATH)}")
    elif r.status_code == 409:
        rv = _post(f"/appbundles/{APPBUNDLE_ID}/versions",
                   {"engine": client.ENGINE, "description": spec["description"]})
        if rv.status_code not in (200, 201):
            raise RuntimeError(f"appbundle version failed {rv.status_code}: {rv.text[:300]}")
        j = rv.json()
        version = j.get("version")
        _upload_bundle_zip(j["uploadParameters"], ZIP_PATH)
        print(f"[appbundle] {APPBUNDLE_ID} exists (409); created v{version}; uploaded zip")
    else:
        raise RuntimeError(f"appbundle POST failed {r.status_code}: {r.text[:300]}")

    a = _post(f"/appbundles/{APPBUNDLE_ID}/aliases", {"id": client.ALIAS, "version": version})
    if a.status_code in (200, 201):
        print(f"[alias] {APPBUNDLE_ID}+{client.ALIAS} -> v{version}")
    elif a.status_code == 409:
        p = _patch(f"/appbundles/{APPBUNDLE_ID}/aliases/{client.ALIAS}", {"version": version})
        if p.status_code not in (200, 201):
            raise RuntimeError(f"appbundle alias PATCH failed {p.status_code}: {p.text[:300]}")
        print(f"[alias] {APPBUNDLE_ID}+{client.ALIAS} PATCHed -> v{version}")
    else:
        raise RuntimeError(f"appbundle alias failed {a.status_code}: {a.text[:300]}")

    return f"{client.nickname()}.{APPBUNDLE_ID}+{client.ALIAS}", version


# --------------------------------------------------------------------------- #
# Activity spec + provisioning (loads the AppBundle, runs LEAFWRITEPROBE)
# --------------------------------------------------------------------------- #
def activity_spec() -> dict:
    qualified_bundle = f"{client.nickname()}.{APPBUNDLE_ID}+{client.ALIAS}"
    command_line = (
        r'$(engine.path)\accoreconsole.exe /i "$(args[HostDwg].path)" '
        r'/al "$(appbundles[' + APPBUNDLE_ID + r'].path)" '
        r'/s "$(settings[script].path)"'
    )
    return {
        "id": ACTIVITY_ID,
        "engine": client.ENGINE,
        "appbundles": [qualified_bundle],
        "commandLine": [command_line],
        "parameters": {
            "HostDwg": {"verb": "get", "required": True, "localName": client.HOSTDWG_LOCALNAME},
            "Result": {"verb": "put", "required": True, "localName": OUT_LOCALNAME},
        },
        "settings": {"script": {"value": ARX_SCRIPT}},
        "description": ("Leaf drawing.write COMPILED probe: load LeafWriteTools AppBundle "
                        "(/al + PackageContents autoload) and run LEAFWRITEPROBE; save output.dwg."),
    }


def provision_activity() -> str:
    """Create/advance the LeafArxProbe Activity and point prod at that version."""
    spec = activity_spec()
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
            raise RuntimeError(f"activity alias PATCH {name} failed {p.status_code}: {p.text[:300]}")
        print(f"[alias] {name}+{client.ALIAS} PATCHed -> v{version}")
    else:
        raise RuntimeError(f"activity alias {name} failed {a.status_code}: {a.text[:300]}")
    return client.activity_qualified(name)


# --------------------------------------------------------------------------- #
# Extract that ALSO returns the WorkItem status (faithful copy of the LIVE path
# in write_spike._extract_with_status — for honest per-WorkItem engine_seconds).
# --------------------------------------------------------------------------- #
def _extract_with_status(dwg_local_path: str):
    dwg_name = os.path.basename(dwg_local_path)
    ts = int(time.time())
    input_key = f"in/{ts}_{dwg_name}"
    output_key = f"out/{ts}_{dwg_name}.families.txt"
    activity_id = client.activity_qualified(client.EXTRACT_ACTIVITY)
    client.upload_object(dwg_local_path, input_key)
    in_url = client.signed_download_url(input_key)
    up_key, out_url = client.signed_upload_url(output_key)
    arguments = {"HostDwg": {"url": in_url, "verb": "get"},
                 "Result": {"url": out_url, "verb": "put"}}
    status = client.submit_workitem(activity_id, arguments, dry_run=False, poll=True)
    if status.get("status") != "success":
        raise RuntimeError(f"extract WorkItem {status.get('id')} status={status.get('status')} "
                           f"report={redact.redact_url(status.get('reportUrl'))}")
    client.finalize_upload(output_key, up_key)
    families = client.download_object(output_key).decode("utf-8", "replace")
    return client.parse_text(families, dwg_local_path), status


# --------------------------------------------------------------------------- #
# Dry-run (zero live calls)
# --------------------------------------------------------------------------- #
def dry_run(dwg: str) -> int:
    ab_spec = appbundle_spec()
    act_spec = activity_spec()
    placeholder_args = {
        "HostDwg": {"url": "https://<signed-s3-GET-url minted at run time>", "verb": "get"},
        "Result": {"url": "https://<signed-s3-PUT-url minted at run time>", "verb": "put"},
    }
    wi_body = {"activityId": f"<owner>.{ACTIVITY_ID}+{client.ALIAS}",
               "arguments": placeholder_args}

    print("=" * 72)
    print("DRY-RUN — no bucket / upload / appbundle / activity / workitem calls performed")
    print("=" * 72)
    print(f"\ninput dwg           : {dwg}")
    print(f"engine              : {client.ENGINE}")
    print(f"target framework    : {TARGET_FRAMEWORK}")
    print(f"appbundle zip       : {ZIP_PATH} (exists={os.path.exists(ZIP_PATH)})")
    print(f"appbundle id        : <owner>.{APPBUNDLE_ID}+{client.ALIAS}")
    print(f"arx activity id     : <owner>.{ACTIVITY_ID}+{client.ALIAS}")
    print(f"extract activity id : <owner>.{client.EXTRACT_ACTIVITY}+{client.ALIAS}")
    print(f"probe layer         : {PROBE_LAYER}")
    print(f"result localName    : {OUT_LOCALNAME}")
    print("\n--- POST /appbundles body ---")
    print(json.dumps(ab_spec, indent=2))
    print("\n--- POST /activities body (LeafArxProbe; owner in appbundles resolved at run time) ---")
    print(json.dumps(act_spec, indent=2))
    print("\n--- WorkItem POST body (arx run; urls redacted) ---")
    print(json.dumps(wi_body, indent=2))
    print("\n--- Activity settings[script].value (\\r\\n shown as lines) ---")
    for i, line in enumerate(ARX_SCRIPT.split("\r\n"), 1):
        if line:
            print(f"{i:>2}| {line}")
    print("\nplanned billable WorkItems: extract(input), arx-run, extract(output) = 3 "
          f"(lane cap {CAP_WORKITEMS} / ${CAP_USD})")
    print("\nDRY-RUN complete. Re-run WITHOUT --dry-run to execute the live probe.")
    return 0


# --------------------------------------------------------------------------- #
# Live run
# --------------------------------------------------------------------------- #
def run(dwg: str) -> int:
    receipt = {
        "spike": "drawing.write.compiled",
        "input_dwg": os.path.relpath(dwg, _REPO).replace("\\", "/"),
        "probe_layer": PROBE_LAYER,
        "generated_at": client.iso_now(),
        "engine": client.ENGINE,
        "target_framework": TARGET_FRAMEWORK,
        "appbundle_id": None,
        "activity_id": None,
        "pass": False,
        "build_toolchain_notes": (
            "dotnet SDK 8.0.417; TargetFramework net8.0-windows; refs "
            "acdbmgd.dll+accoremgd.dll from C:/Program Files/Autodesk/AutoCAD 2026 "
            "(Private=false, not shipped in bundle); bundle = LeafWriteTools.bundle/"
            "{PackageContents.xml,Contents/LeafWriteTools.dll} zipped with forward-slash "
            "entries (python zipfile); locally proven in accoreconsole 2026 via NETLOAD "
            "(LeafWriteTools loaded -> deleted a polyline -> added one on LEAF_WRITE_PROBE "
            "-> SaveAs output.dwg). DA loads via /al + PackageContents LoadOnCommandInvocation."
        ),
    }
    try:
        bres = client.create_bucket()
        print(f"[bucket] {client.bucket_key()} -> {bres}")

        # ---- provision the compiled AppBundle (NOT billable) --------------
        qualified_bundle, ab_version = provision_appbundle()
        receipt["appbundle_id"] = qualified_bundle
        receipt["appbundle_version"] = ab_version

        # ---- provision the Activity that loads it (NOT billable) ----------
        activity_id = provision_activity()
        receipt["activity_id"] = activity_id
        receipt["activity_commandLine"] = activity_spec()["commandLine"][0]

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
        ts = int(time.time())
        in_key = f"in/{ts}_{os.path.basename(dwg)}"
        out_key = f"out/{ts}_output.dwg"
        client.upload_object(dwg, in_key)
        in_url = client.signed_download_url(in_key)
        up_key, out_url = client.signed_upload_url(out_key)

        # ---- billable #2: the COMPILED ARX WorkItem ----------------------
        _cap_guard("arx-run")
        w_args = {"HostDwg": {"url": in_url, "verb": "get"},
                  "Result": {"url": out_url, "verb": "put"}}
        st_w = client.submit_workitem(activity_id, w_args, dry_run=False, poll=True)
        receipt["arx_workitem"] = _record("arx-run", st_w)
        if st_w.get("status") != "success":
            raise RuntimeError(f"arx WorkItem {st_w.get('id')} status={st_w.get('status')} "
                               f"report={redact.redact_url(st_w.get('reportUrl'))}")
        client.finalize_upload(out_key, up_key)
        out_bytes = client.download_object(out_key)
        if not out_bytes:
            raise RuntimeError("arx-run produced 0-byte output.dwg")
        tmp = tempfile.NamedTemporaryFile(prefix="leaf_arx_out_", suffix=".dwg", delete=False)
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

        # ---- assertions (same oracle as the LISP spike) ------------------
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
            print(f"ARX-PATH: FAIL — {'; '.join(reasons)}")
            return 1

        print("ARX-PATH: PASS")
        return 0

    except Exception as e:
        receipt["error"] = f"{type(e).__name__}: {e}"
        receipt["cumulative_usd_est"] = _cumulative_usd()
        receipt["billable_workitem_count"] = len(_LEDGER)
        try:
            _write_receipt(receipt)
        except Exception:
            pass
        print(f"ARX-PATH: FAIL — {type(e).__name__}: {e}")
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
        print("usage: python da/arx_probe.py [--dry-run] <dwg>", file=sys.stderr)
        return 2
    dwg = args[0]
    if not is_dry and not os.path.exists(dwg):
        print(f"ARX-PATH: FAIL — input dwg not found: {dwg}")
        return 1
    return dry_run(dwg) if is_dry else run(dwg)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
