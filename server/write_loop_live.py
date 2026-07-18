#!/usr/bin/env python3
r"""server/write_loop_live.py — the ONE pre-authorized live write-loop receipt (M2).

Exercises the PRODUCT write path against real APS Design Automation + the
persistent OSS store, reusing the already-provisioned LeafWriteProbe+prod
Activity (proven by data/write_spike_receipt.json). Hard lane cap: 6 billable
WorkItems / $0.50 (fails closed before any submit that would exceed it).

Steps (matches the M2 brief live-receipt block):
  1. ingest data/rooftop_demo.dwg as a real drawing v1 in the persistent bucket
     (store.ingest_drawing over the OSS backend) — 0 WorkItems (pure OSS).
  2. extract v1 -> cache v1 intake (1 WorkItem).                     [measured]
  3. run the write tool live via write_loop.run_write_live (the SAME function the
     broker's APS_LIVE=1 write branch calls): LeafWriteProbe on v1's DWG ->
     put_drawing v2 -> re-extract v2 for the intake cache. (write WI [measured] +
     internal extract-v2 WI).
  4. confirm the mutation on v2 (LEAF_WRITE_PROBE present; one v1 handle gone).
  5. undo -> head=v1; confirm read_intake(head) serves v1's cached intake.
  6. write data/write_loop_receipt.json.

Run:  cd server && python write_loop_live.py
Final stdout line: `WRITE-LOOP: PASS` (exit 0) or `WRITE-LOOP: FAIL ...` / blocked.
"""
from __future__ import annotations

# cache stdlib platform BEFORE anything drags in the repo-root platform/ shadow
import platform as _stdlib_platform  # noqa: F401
import json
import os
import sys
import time
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SERVER_DIR.parent
DA_DIR = PROJECT_ROOT / "da"
for p in (str(SERVER_DIR), str(DA_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

import client            # da/client.py (credential-holding)  # noqa: E402
import store             # da/store.py                         # noqa: E402
import write_loop        # server/write_loop.py                # noqa: E402

DWG = str(PROJECT_ROOT / "data" / "rooftop_demo.dwg")
RECEIPT_PATH = PROJECT_ROOT / "data" / "write_loop_receipt.json"
TENANT = os.environ.get("LEAF_LIVE_TENANT", "m2live")
CAP_WORKITEMS = 6
CAP_USD = 0.50
USD_PER_HR = float(os.environ.get("APS_USD_PER_HR", "10"))

WRITE_TOOL_PKG = {"name": "delete-marked-panel", "version": "1.0.0",
                  "capabilities": ["drawing.write"], "engine_op": "delete_marked_panel"}

_LEDGER: list[dict] = []


def _usd(engine_seconds):
    return None if engine_seconds is None else round(engine_seconds / 3600.0 * USD_PER_HR, 4)


def _cumulative():
    return round(sum((e.get("usd_est") or 0.0) for e in _LEDGER), 4)


def _cap_guard(label: str):
    if len(_LEDGER) >= CAP_WORKITEMS:
        raise RuntimeError(f"LANE CAP: {len(_LEDGER)} WorkItems >= {CAP_WORKITEMS}; refusing {label!r}")
    if _cumulative() >= CAP_USD:
        raise RuntimeError(f"LANE CAP: ${_cumulative()} >= ${CAP_USD}; refusing {label!r}")


def _record(label, status, *, engine_seconds=None, note=None) -> dict:
    eng = engine_seconds if engine_seconds is not None else client._engine_seconds(status or {})
    block = {"label": label, "id": (status or {}).get("id"),
             "status": (status or {}).get("status"),
             "engine_seconds": eng, "usd_est": _usd(eng)}
    if note:
        block["note"] = note
    _LEDGER.append(block)
    print(f"[workitem] {label}: id={block['id']} status={block['status']} "
          f"engine_s={eng} usd={block['usd_est']} (cum ${_cumulative()})")
    return block


def _extract_v1_with_status(dwg_local_path: str):
    """Faithful copy of client.extract()'s LIVE legacy path that ALSO returns the
    WorkItem status (client.extract hides it) so v1's extract is honestly metered."""
    dwg_name = os.path.basename(dwg_local_path)
    ts = int(time.time())
    input_key = f"in/{ts}_{dwg_name}"
    output_key = f"out/{ts}_{dwg_name}.families.txt"
    activity_id = client.activity_qualified(client.EXTRACT_ACTIVITY)
    client.upload_object(dwg_local_path, input_key)
    in_url = client.signed_download_url(input_key)
    up_key, out_url = client.signed_upload_url(output_key)
    args = {"HostDwg": {"url": in_url, "verb": "get"}, "Result": {"url": out_url, "verb": "put"}}
    status = client.submit_workitem(activity_id, args, dry_run=False, poll=True)
    if status.get("status") != "success":
        raise RuntimeError(f"extract WorkItem {status.get('id')} status={status.get('status')} "
                           f"report={status.get('reportUrl')}")
    client.finalize_upload(output_key, up_key)
    families = client.download_object(output_key).decode("utf-8", "replace")
    return client.parse_text(families, dwg_local_path), status


def main() -> int:
    receipt = {"loop": "drawing.write.product", "input_dwg": "data/rooftop_demo.dwg",
               "generated_at": client.iso_now(), "engine": client.ENGINE,
               "activity": write_loop.WRITE_ACTIVITY, "tenant": TENANT,
               "pass": False, "undo_verified": False}
    try:
        be = store.OSSBackend()
        print(f"[bucket] {client.bucket_key()} -> {client.create_bucket()}")

        # ---- 1) ingest v1 (pure OSS, 0 WorkItems) -----------------------
        drawing_id = store.new_drawing_id()
        ing = store.ingest_drawing(be, TENANT, DWG, drawing_id=drawing_id)
        receipt["drawing_id"] = drawing_id
        assert ing["version"] == 1
        print(f"[ingest] v1 {TENANT}/{drawing_id}")

        # ---- 2) extract v1 -> cache its intake (1 WI, measured) ---------
        _cap_guard("extract-v1")
        v1_intake, st_v1 = _extract_v1_with_status(DWG)
        receipt["extract_v1_workitem"] = _record("extract-v1", st_v1)
        H1 = {p["handle"] for p in v1_intake["polylines"] if p.get("handle")}
        L1 = set(v1_intake["layers"])
        be.put(write_loop.intake_cache_key(TENANT, drawing_id, 1),
               json.dumps(v1_intake, separators=(",", ":")).encode("utf-8"))
        print(f"[v1] polylines={len(v1_intake['polylines'])} handles={len(H1)} "
              f"probe_in_v1={write_loop.PROBE_LAYER in L1}")

        # ---- 3) live write via the PRODUCT function (write WI + extract-v2) --
        _cap_guard("write")
        t0 = time.perf_counter()
        env, code = write_loop.run_write_live(WRITE_TOOL_PKG, {"drawing_id": drawing_id},
                                              TENANT, backend=be, da=client, t0=t0)
        if not env.get("ok"):
            raise RuntimeError(f"run_write_live failed (HTTP {code}): {env.get('error')}")
        nv = env["result"]["new_version"]
        receipt["new_version"] = nv
        receipt["versions"] = [1, nv["version"]]
        assert nv == {"drawing_id": drawing_id, "version": 2, "parent": 1}, nv
        cost = env.get("cost") or {}
        _record("write", {"id": env["result"].get("workitem_id"), "status": "success"},
                engine_seconds=cost.get("engine_seconds"))
        _record("extract-v2", {"id": None, "status": "success"},
                note="internal to run_write_live re-extract; engine_seconds not itemized "
                     "(client.extract hides WorkItem status). ~1 billable WI (~$0.01).")
        print(f"[write] v2 created parent=1 workitem={env['result'].get('workitem_id')} "
              f"cost={cost}")

        # ---- 4) confirm the mutation on v2 ------------------------------
        v2ver, v2_intake = write_loop.read_intake(be, TENANT, drawing_id, "head")
        H2 = {p["handle"] for p in v2_intake["polylines"] if p.get("handle")}
        L2 = set(v2_intake["layers"])
        add_ok = (write_loop.PROBE_LAYER in L2) and (write_loop.PROBE_LAYER not in L1)
        probe_poly_ok = any(p.get("layer") == write_loop.PROBE_LAYER for p in v2_intake["polylines"])
        deleted = H1 - H2
        delete_ok = len(deleted) == 1
        receipt.update({"v2_probe_layer_present": write_loop.PROBE_LAYER in L2,
                        "v2_probe_polyline_present": probe_poly_ok,
                        "deleted_handles": sorted(deleted), "delete_confirmed": delete_ok,
                        "v2_head_version": v2ver})
        print(f"[verify v2] add={add_ok} probe_poly={probe_poly_ok} delete={delete_ok} "
              f"deleted={sorted(deleted)}")

        # ---- 5) undo -> head=v1; confirm intake?version=head serves v1 --
        new_head = store.undo(be, TENANT, drawing_id)
        head_v, head_intake = write_loop.read_intake(be, TENANT, drawing_id, "head")
        undo_ok = (new_head == 1 and head_v == 1
                   and write_loop.PROBE_LAYER not in set(head_intake["layers"])
                   and len(head_intake["polylines"]) == len(v1_intake["polylines"]))
        receipt["undo_verified"] = bool(undo_ok)
        receipt["undo_head_version"] = new_head
        print(f"[undo] head={new_head} serves_v1={undo_ok} "
              f"(polys={len(head_intake['polylines'])}, probe={write_loop.PROBE_LAYER in set(head_intake['layers'])})")

        passed = bool(add_ok and probe_poly_ok and delete_ok and undo_ok)
        receipt["pass"] = passed
        receipt["cumulative_spend"] = _cumulative()
        receipt["workitems"] = _LEDGER
        _write(receipt)
        if not passed:
            print("WRITE-LOOP: FAIL — one of {add, probe, delete, undo} did not confirm")
            return 1
        print(f"WRITE-LOOP: PASS (cumulative ${_cumulative()}, {len(_LEDGER)} WorkItems)")
        return 0
    except Exception as e:  # noqa: BLE001
        receipt["error"] = f"{type(e).__name__}: {e}"
        receipt["cumulative_spend"] = _cumulative()
        receipt["workitems"] = _LEDGER
        try:
            _write(receipt)
        except Exception:
            pass
        print(f"WRITE-LOOP: FAIL — {type(e).__name__}: {e}")
        return 1


def _write(receipt: dict):
    RECEIPT_PATH.parent.mkdir(parents=True, exist_ok=True)
    RECEIPT_PATH.write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    print(f"[receipt] wrote {RECEIPT_PATH}")


if __name__ == "__main__":
    sys.exit(main())
