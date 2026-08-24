"""Broker-owned APS blank-DWG feasibility producer.

This module has no credential loader.  The broker injects its already-loaded
``da/client.py`` module, which remains the sole APS credential holder.

ONE RECIPE, TWO CONSUMERS
-------------------------
The .scr this activity runs is NOT written here.  It comes from
``da/blank_lisp.py``, the same pure recipe module the T3-02 CLI spike
(``da/blank_spike.py``) uses, which was proven live on 2026-08-24 (WorkItem
3649a0c7bea34c33bd1f0042b21a31bd, 32466-byte AC1032 DWG, read round-trip with
provenance asserted).  Two copies of a recipe are two chances to run a stale
one; this module is the second CONSUMER of one recipe, not a second recipe.

The copy that used to live here called
``(vla-SaveAs (vla-get-ActiveDocument (vlax-get-acad-object)) ...)``.  Measured
2026-08-24 against a real AutoCAD 2026 accoreconsole: headless there is no COM
application object, ``(vlax-get-acad-object)`` returns nil, the call dies with
"bad argument type: VLA-OBJECT nil", accoreconsole still exits 0, and NO
drawing is written.  That body never ran live (its workflow has zero runs), so
its first run would have burned a billable WorkItem to produce nothing.

PROVENANCE IS A HARD GATE
-------------------------
Every run mints a uuid-derived marker layer, and the paid read leg must report
that exact marker back or the run is ``unsupported``.  Without it a read that
returned some OTHER drawing's bytes would look like success.  The recipe draws
one witness POINT on the marker layer because this module's read witness,
``count-by-layer``, counts model-space ENTITIES per layer: a bare marker layer
is invisible to it (measured: counts={} without the point, counts={marker: 1}
with it).  See ``da/blank_lisp.WITNESS_POINT``.

The marker therefore has to be per-run, which is why the .scr is delivered as a
per-run ARGUMENT rather than a baked-in activity ``settings`` value: baking it
in would force a new activity VERSION for every single create.
"""
from __future__ import annotations

import hashlib
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Callable


CONTRACT = "leaf.aps-blank-dwg-feasibility.v1"
ACTIVITY_ID = "LeafBlankDwgFeasibility"
OUTPUT_NAME = "blank.dwg"
SCRIPT_NAME = "run.scr"
DWG_MAGIC = b"AC10"

_BLANK_LISP: Any = None


def _blank_lisp() -> Any:
    """Load the one blank-DWG recipe module, ``da/blank_lisp.py``, by path.

    By absolute path rather than ``import blank_lisp``: the broker loads THIS
    module by path under the name ``leaf_blank_dwg``, so it has no package
    context and must not depend on ``sys.path`` ordering.  ``blank_lisp`` is
    pure string construction with no network and no credential, so loading it
    here does not widen this module's no-credential contract.
    """
    global _BLANK_LISP
    if _BLANK_LISP is None:
        import importlib.util

        path = Path(__file__).resolve().parents[2] / "da" / "blank_lisp.py"
        spec = importlib.util.spec_from_file_location("leaf_blank_lisp", path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"blank DWG recipe is unavailable at {path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _BLANK_LISP = module
    return _BLANK_LISP


def activity_spec(engine: str) -> dict[str, Any]:
    """Return the exact no-input Design Automation activity definition.

    No ``HostDwg`` parameter and no ``/i`` on the command line: the engine opens
    its own ``acad.dwt``, so the create leg never references a customer object.
    The .scr is a per-run ``Script`` argument, never a baked-in setting.
    """
    return _blank_lisp().blank_activity_spec(
        ACTIVITY_ID,
        engine,
        out_localname=OUTPUT_NAME,
        script_localname=SCRIPT_NAME,
    ) | {
        "description":
            "Leaf protected feasibility probe for a true no-input blank DWG.",
    }


def _runs_the_same_thing(live: dict[str, Any], want: dict[str, Any]) -> bool:
    """Compare only the fields that decide what the engine actually RUNS.

    ``description`` and APS-assigned bookkeeping fields drift harmlessly;
    ``engine``, ``commandLine`` and ``parameters`` do not.
    """
    return all(
        live.get(field) == want.get(field)
        for field in ("engine", "commandLine", "parameters")
    )


def _aliased_matching_version(
    da: Any, spec: dict[str, Any], headers: dict[str, str]
) -> int:
    """Return a version whose LIVE body matches ``spec``, publishing one if not.

    A bare ``POST /activities`` plus "409 means it is already there" is NOT
    idempotent provisioning: it silently keeps whatever body was uploaded
    first.  The first body this module ever shipped could not produce a
    drawing at all (see the module docstring), so treating 409 as success is
    exactly how a paid WorkItem gets spent running known-broken code.  Read the
    aliased version back, and repoint the alias if it is not the recipe we
    intend to run.
    """
    alias = da.requests.get(
        f"{da.DA}/activities/{ACTIVITY_ID}/aliases/{da.ALIAS}",
        headers=headers,
        timeout=da._HTTP_TIMEOUT,
    )
    if alias.status_code == 200:
        current = alias.json().get("version")
        if isinstance(current, int) and current > 0:
            live = da.requests.get(
                f"{da.DA}/activities/{ACTIVITY_ID}/versions/{current}",
                headers=headers,
                timeout=da._HTTP_TIMEOUT,
            )
            live.raise_for_status()
            if _runs_the_same_thing(live.json(), spec):
                return current
    elif alias.status_code != 404:
        alias.raise_for_status()

    # The version body is the activity definition WITHOUT its id.
    body = {key: value for key, value in spec.items() if key != "id"}
    published = da.requests.post(
        f"{da.DA}/activities/{ACTIVITY_ID}/versions",
        headers=headers,
        data=da.json.dumps(body),
        timeout=da._HTTP_TIMEOUT,
    )
    published.raise_for_status()
    version = published.json().get("version")
    if not isinstance(version, int) or version < 1:
        raise RuntimeError(
            f"activity version publish returned an unusable version {version!r}"
        )
    return version


def _ensure_activity(da: Any) -> str:
    """Idempotently provision the one broker-owned activity and alias."""
    spec = activity_spec(da.ENGINE)
    headers = {**da._auth_headers(), "Content-Type": "application/json"}
    created = da.requests.post(
        f"{da.DA}/activities",
        headers=headers,
        data=da.json.dumps(spec),
        timeout=da._HTTP_TIMEOUT,
    )
    if created.status_code == 409:
        version = _aliased_matching_version(da, spec, headers)
    else:
        created.raise_for_status()
        version = 1
    alias = da.requests.post(
        f"{da.DA}/activities/{ACTIVITY_ID}/aliases",
        headers=headers,
        data=da.json.dumps({"id": da.ALIAS, "version": version}),
        timeout=da._HTTP_TIMEOUT,
    )
    if alias.status_code == 409:
        # The alias exists and may still point at an older version. PATCH is
        # the only way to move it, and skipping it is how stale code survives.
        moved = da.requests.patch(
            f"{da.DA}/activities/{ACTIVITY_ID}/aliases/{da.ALIAS}",
            headers=headers,
            data=da.json.dumps({"version": version}),
            timeout=da._HTTP_TIMEOUT,
        )
        moved.raise_for_status()
    elif alias.status_code not in (200, 201):
        alias.raise_for_status()
    return da.activity_qualified(ACTIVITY_ID)


def _cost(da: Any, status: dict[str, Any]) -> dict[str, float] | None:
    seconds = da._engine_seconds(status)
    if seconds is None:
        return None
    return {
        "engine_seconds": float(seconds),
        "usd_est": round(
            float(seconds) / 3600.0 * float(os.environ.get("APS_USD_PER_HR", "10")),
            4,
        ),
    }


def _combined_cost(*costs: dict[str, Any] | None) -> dict[str, float] | None:
    usable = [value for value in costs if isinstance(value, dict)]
    if not usable:
        return None
    return {
        "engine_seconds": round(
            sum(float(value.get("engine_seconds") or 0.0) for value in usable), 2
        ),
        "usd_est": round(sum(float(value.get("usd_est") or 0.0) for value in usable), 4),
    }


def marker_reported(result: Any, marker: str) -> bool:
    """True only when the read witness reports THIS run's marker layer.

    ``count-by-layer`` returns ``{"counts": {<layer>: <n>}}`` over model-space
    entities, which is why the recipe puts a witness POINT on the marker layer.
    Anything else - a missing key, a different shape, another drawing's layers -
    is a failed provenance check, never a pass.
    """
    if not isinstance(result, dict):
        return False
    counts = result.get("counts")
    if not isinstance(counts, dict):
        return False
    return marker in counts


def _unsupported(
    *,
    source_sha: str,
    marker_layer: str,
    reason: str,
    workitem_id: str | None,
    cost: dict[str, float] | None,
) -> dict[str, Any]:
    return {
        "contract": CONTRACT,
        "status": "unsupported",
        "source_sha": source_sha,
        "activity": ACTIVITY_ID,
        "marker_layer": marker_layer,
        "workitem_id": workitem_id,
        "output": None,
        "drawing": None,
        "read": None,
        "cost": cost,
        "fallback": "upload_only",
        "reason": reason,
        "degraded_mode": False,
    }


def run(
    da: Any,
    *,
    tenant_id: str,
    source_sha: str,
    read_tool: dict[str, Any],
    publish: Callable[[bytes, str], dict[str, Any]],
    on_submitted: Callable[[str | None], None] | None = None,
) -> dict[str, Any]:
    """Run one paid no-input create, validate/read it, then publish version 1.

    A terminal APS rejection, invalid DWG, failed re-extract, or a read whose
    layers do not carry this run's marker is a closed unsupported result.
    Credential, transport, provisioning, and publication failures raise, so an
    operational fault cannot masquerade as lack of APS product support.
    """
    lisp = _blank_lisp()
    marker = lisp.new_marker_layer()
    # witness=True: this module's read witness counts entities, not layers.
    script = lisp.build_blank_scr(marker, out_localname=OUTPUT_NAME, witness=True)

    activity = _ensure_activity(da)
    # Per-run entropy in the key, never a bare clock: da/client.py's legacy
    # scratch keys are 1-second granular and collide under concurrency.
    nonce = f"blank-dwg/{tenant_id}/{int(time.time())}-{os.urandom(6).hex()}"
    script_key = f"{nonce}/{SCRIPT_NAME}"
    output_key = f"{nonce}/{OUTPUT_NAME}"

    def _discard(key: str) -> None:
        try:
            da.delete_scratch_object(key)
        except Exception:  # noqa: BLE001 - cleanup must never mask the result
            pass

    try:
        with tempfile.TemporaryDirectory(prefix="leaf-blank-dwg-scr-") as tmp:
            local_script = Path(tmp) / SCRIPT_NAME
            # newline="" - on Windows the default rewrites LF to CRLF, and the
            # bytes uploaded here are the bytes the engine executes.
            local_script.write_text(script, encoding="utf-8", newline="")
            da.upload_scratch_object(str(local_script), script_key)
        script_url = da.scratch_signed_download_url(script_key)
        upload_key, output_url = da.scratch_signed_upload_url(output_key)
        status = da.submit_workitem(
            activity,
            {
                "Script": {"url": script_url, "verb": "get"},
                "Result": {"url": output_url, "verb": "put"},
            },
            dry_run=False,
            poll=True,
            tenant_id=tenant_id,
            on_submitted=on_submitted,
        )
        workitem_id = status.get("id") if isinstance(status, dict) else None
        create_cost = _cost(da, status)
        if status.get("status") != "success":
            return _unsupported(
                source_sha=source_sha,
                marker_layer=marker,
                reason="no_input_activity_rejected",
                workitem_id=workitem_id,
                cost=create_cost,
            )

        da.finalize_scratch_upload(output_key, upload_key)
        payload = da.download_scratch_object(output_key)
    finally:
        _discard(script_key)
        _discard(output_key)

    if len(payload) < lisp.MIN_PLAUSIBLE_DWG_BYTES or not payload.startswith(DWG_MAGIC):
        return _unsupported(
            source_sha=source_sha,
            marker_layer=marker,
            reason="invalid_dwg_output",
            workitem_id=workitem_id,
            cost=create_cost,
        )

    digest = hashlib.sha256(payload).hexdigest()
    with tempfile.TemporaryDirectory(prefix="leaf-blank-dwg-") as tmp:
        path = Path(tmp) / OUTPUT_NAME
        path.write_bytes(payload)
        read = da.run_tool(
            str(path),
            read_tool,
            {},
            tenant_id=tenant_id,
            on_submitted=on_submitted,
        )
    if not isinstance(read, dict) or not read.get("ok"):
        return _unsupported(
            source_sha=source_sha,
            marker_layer=marker,
            reason="read_tool_failed",
            workitem_id=workitem_id,
            cost=_combined_cost(create_cost, read.get("cost") if isinstance(read, dict) else None),
        )
    if not marker_reported(read.get("result"), marker):
        # A paid read that cannot show this run's own marker has not proven it
        # read this run's bytes. Fail closed rather than publish a drawing on
        # the strength of an unattributable 200.
        return _unsupported(
            source_sha=source_sha,
            marker_layer=marker,
            reason="provenance_mismatch",
            workitem_id=workitem_id,
            cost=_combined_cost(create_cost, read.get("cost")),
        )

    drawing = publish(payload, digest)
    combined = _combined_cost(create_cost, read.get("cost"))
    return {
        "contract": CONTRACT,
        "status": "supported",
        "source_sha": source_sha,
        "activity": ACTIVITY_ID,
        "marker_layer": marker,
        "workitem_id": workitem_id,
        "output": {"sha256": digest, "bytes": len(payload), "version": 1},
        "drawing": drawing,
        "read": {
            "tool": read_tool["name"],
            "ok": True,
            "result": read.get("result"),
        },
        "cost": combined,
        "fallback": None,
        "reason": None,
        "degraded_mode": False,
    }
