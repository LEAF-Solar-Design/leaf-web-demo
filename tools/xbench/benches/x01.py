"""X01 slice 1: grade Studio's converted original against its first solar export."""

import hashlib
from pathlib import Path
import re

from benches.x02 import _transport
from xbench_core import Step, utc_now, write_json_atomic
import x01_geometry as geometry
import xbench_session
from xbench_studio import StudioClient, StudioError, disposable_project, new_idempotency_key, parse_error


LIMITS = "X01 slice 1: original and export both come from Studio's own DWG conversion (version 1 vs head after panels import); stringing and tags are not driven yet, so step 3 is UNREADABLE until they are; the plot is an ezdxf render, not an AutoCAD plot."


def _row(fixture):
    return {"id": fixture["id"], "drawing_id": None, "original_version_id": None,
            "export_version_id": None, "dxf_sha256": {}, "counts": {},
            "observed_insunits": None, "expected_insunits": fixture.get("insunits"), "failures": {},
            "steps": {str(n): "UNREADABLE" for n in range(1, 6)},
            "evidence": {str(n): "fixture not graded" for n in range(1, 6)}}


def _mark(row, n, verdict, evidence, failures=None):
    row["steps"][str(n)] = verdict
    row["evidence"][str(n)] = evidence
    if failures:
        row["failures"][str(n)] = failures


def _item(doc, handle, **extra):
    entity = doc.entitydb.get(handle)
    return {"handle": handle, "layer": entity.dxf.layer if entity is not None else None, **extra}


def grade_pair(fixture, original_bytes, export_bytes, *, row=None):
    """Grade only the two DXFs; source-file integrity is checked by grade_files."""
    row = _row(fixture) if row is None else row
    row["dxf_sha256"] = {"original": hashlib.sha256(original_bytes).hexdigest(),
                         "export": hashlib.sha256(export_bytes).hexdigest()}
    try:
        original, exported = geometry.load(original_bytes), geometry.load(export_bytes)
    except Exception as exc:
        for n in range(2, 6):
            _mark(row, n, "UNREADABLE", f"DXF analysis unavailable: {type(exc).__name__}")
        return row
    row["observed_insunits"] = geometry.insunits(exported)
    row["original_insunits"] = geometry.insunits(original)
    row["units_mismatch"] = (row["observed_insunits"] != fixture["insunits"] or
                             row["original_insunits"] != fixture["insunits"])
    layers, blocks = fixture["module_layers"], fixture.get("module_blocks", [])
    points = None
    try:
        before = geometry.module_points(original, layers, blocks)
        points = geometry.module_points(exported, layers, blocks)
        tol = fixture.get("tolerance", 0.0127 if fixture["insunits"] == 6 else 0.5)
        matched = geometry.match_positions(before, points, tol)
        row["counts"] = {"original": len(before), "export": len(points),
                         "oracle": fixture["modules"], "matched": matched["matched"]}
        row["positions"] = matched
        failures = [_item(original, handle, reason="missing module") for handle in matched["missing"]]
        failures.extend(_item(original, handle, reason="moved module", distance=distance)
                        for handle, distance in matched["moved"])
        failed = (len(before) != fixture["modules"] or len(points) != fixture["modules"] or
                  matched["matched"] != len(before) or matched["extra"] != 0)
        _mark(row, 2, "FAIL" if failed else "PASS",
              f"counts {row['counts']}; missing/moved {failures}; extra {matched['extra']}", failures)
    except Exception as exc:
        _mark(row, 2, "UNREADABLE", f"module positions unavailable: {type(exc).__name__}")
    try:
        tag_layers = fixture.get("tag_layers", [])
        tags = list(geometry._tag_entities(exported, tag_layers))
        scale = geometry.sheet_scale(exported)
        row["tag_count"], row["sheet_scale"] = len(tags), scale
        if not tags:
            _mark(row, 3, "UNREADABLE", "no text tags on manifest tag layers; stringing/tags not driven in slice 1")
        elif scale is None:
            _mark(row, 3, "UNREADABLE", "tags present, but no paper-space model viewport scale")
        else:
            checks = geometry.tag_checks(exported, tag_layers, scale)
            failures = [_item(exported, handle, reason="tag below 1.8 mm", paper_mm=height)
                        for handle, height in checks["too_small"]]
            failures.extend(_item(exported, handle, reason="tag overlap exceeds 10%",
                                  other=_item(exported, other), fraction=fraction)
                            for handle, other, fraction in checks["overlaps"])
            _mark(row, 3, "FAIL" if failures else "PASS",
                  f"tags {checks['count']}; scale {scale}; failures {failures}", failures)
    except Exception as exc:
        _mark(row, 3, "UNREADABLE", f"tag analysis unavailable: {type(exc).__name__}")
    try:
        if points is None:
            raise ValueError("module selection unavailable")
        handles = [handle for handle, _, _ in points]
        handles.extend(entity.dxf.handle for entity in geometry._tag_entities(exported, fixture.get("tag_layers", [])))
        failures = [{"handle": handle, "layer": layer, "reason": reason}
                    for handle, layer, reason in geometry.entities_on_bad_layers(exported, handles)]
        _mark(row, 4, "FAIL" if failures else "PASS", f"bad layers {failures}", failures)
    except Exception as exc:
        _mark(row, 4, "UNREADABLE", f"layer analysis unavailable: {type(exc).__name__}")
    try:
        footprints = geometry.module_footprints(exported, layers, blocks)
        failures = [_item(exported, handle, reason="fill covers more than 5%",
                          fill=_item(exported, fill), fraction=fraction)
                    for handle, fill, fraction in geometry.fill_cover(exported, footprints)]
        _mark(row, 5, "FAIL" if failures else "PASS", f"covering fills {failures}", failures)
    except Exception as exc:
        _mark(row, 5, "UNREADABLE", f"fill analysis unavailable: {type(exc).__name__}")
    return row


def _product_refusal(exc):
    return (isinstance(exc, StudioError) and 400 <= exc.status < 500
            and exc.status not in (401, 403, 408, 429)
            and exc.error_code != "AUTH_DENIED")


def grade_files(client, *, project, fixtures, out_dir=None):
    rows = {}
    for fixture in fixtures:
        row = _row(fixture)
        rows[fixture["id"]] = row
        capability, did, source = None, None, None
        pid = project["project_id"]
        try:
            source = Path(fixture["path"]).read_bytes()
            row["source_sha256_before"] = hashlib.sha256(source).hexdigest()
            if (len(source) != fixture["bytes"] or row["source_sha256_before"] != fixture["sha256"]):
                raise ValueError("fixture differs from manifest")
            upload = client.upload_drawing(Path(fixture["path"]).name, source)
            client.wait_upload(upload)
            drawing = client.adopt_drawing(pid, upload, fixture["id"])
            did, version = drawing["drawing_id"], drawing["version_id"]
            row["drawing_id"], row["original_version_id"] = did, version
            original = client.dxf_bytes(pid, did, version)
            row["dxf_sha256"]["original"] = hashlib.sha256(original).hexdigest()
            capability, _ = client.checkout(pid, did, ttl_s=900)
            context = client.drawing_context(pid, version, did)
            rev = context.get("intake", {}).get("solar_design_graph", {}).get("rev", 0)

            def call(tool, params):
                nonlocal version, rev, context
                status, body = client.run(tool, org_id=project["org_id"], project_id=pid,
                                          version_id=version, params=params, capability=capability,
                                          idempotency_key=new_idempotency_key("x01"))
                if not 200 <= status < 300:
                    raise parse_error(status, body)
                if status == 202:
                    if not isinstance(body, dict) or not body.get("job_id"):
                        raise StudioError(502, "INVALID_JOB_RESPONSE", "Write accepted without a job ID")
                    body = client.wait_job(body["job_id"], timeout_s=600)
                if isinstance(body, dict) and (body.get("status") == "failed" or body.get("ok") is False
                                               or isinstance(body.get("error"), dict)):
                    raise parse_error(422, body)
                context = client.drawing_context(pid, version, did)
                version = context["context"]["head_version_id"]
                rev = context.get("intake", {}).get("solar_design_graph", {}).get("rev", rev)

            seed = {"schema_version": 1,
                    "source_intake_sha256": context["context"]["intake_sha256"],
                    "units": {"drawing_units": "m" if fixture["insunits"] == 6 else "in",
                              "wcs_to_ucs": [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1],
                              "elevation_datum": "xbench", "crs": None}}
            call("solar-settings", {"drawing_id": did, "expected_rev": rev,
                                    "changes": {"panel_layer_contains": fixture.get(
                                        "panel_layer_contains", fixture["module_layers"][0])},
                                    "initialize": seed})
            call("solar-panels-from-drawing", {"drawing_id": did, "expected_rev": rev})
            row["export_version_id"] = version
            exported = client.dxf_bytes(pid, did, version)
            grade_pair(fixture, original, exported, row=row)
            if out_dir is not None:
                try:
                    # IDs are manifest data, never allow them to escape the evidence directory.
                    safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", fixture["id"])
                    plot = Path(out_dir) / f"plot-{safe_id}.pdf"
                    plot.parent.mkdir(parents=True, exist_ok=True)
                    geometry.render_pdf(geometry.load(exported), plot)
                    row["plot"] = plot.name
                except Exception as exc:
                    row["plot_note"] = f"evidence plot unavailable: {type(exc).__name__}"
        except Exception as exc:
            verdict = "FAIL" if _product_refusal(exc) else "UNREADABLE"
            if isinstance(exc, StudioError):
                code = exc.reason_code or exc.error_code or "STUDIO_REFUSAL"
                message = str(exc.message).replace(capability, "[redacted]") if capability else str(exc.message)
                if isinstance(code, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{1,63}", code):
                    note = f"{code}: {xbench_session.redact(message)}"
                else:
                    note = xbench_session.redact(f"{code}: {message}")
                row["product_error"] = {"status": exc.status, "code": code, "message": note}
            else:
                note = f"fixture unavailable: {type(exc).__name__}"
            for n in range(2, 6):
                _mark(row, n, verdict, note)
            _mark(row, 1, "UNREADABLE", note)
        finally:
            if capability is not None:
                try:
                    client.release(pid, did, capability)
                except Exception as exc:
                    row["release_note"] = f"checkout release unavailable: {type(exc).__name__}"
            if source is not None:
                try:
                    after = Path(fixture["path"]).read_bytes()
                    row["source_sha256_after"] = hashlib.sha256(after).hexdigest()
                    unchanged = after == source
                    fetched = set(row["dxf_sha256"]) == {"original", "export"}
                    _mark(row, 1, "FAIL" if not unchanged else "PASS" if fetched else row["steps"]["1"],
                          "source fixture changed" if not unchanged else
                          "normal-route original/export SHA-256 recorded; source unchanged" if fetched else
                          row["evidence"]["1"] + "; source unchanged, export unavailable")
                except OSError:
                    _mark(row, 1, "FAIL", "source fixture unavailable after run")
    return rows


def _verdict(values):
    values = set(values)
    return "FAIL" if "FAIL" in values else "UNREADABLE" if not values or "UNREADABLE" in values else "PASS"


def score(rows):
    rows = list(rows.values()) if isinstance(rows, dict) else list(rows)
    steps = []
    for n in range(1, 6):
        key = str(n)
        evidence = "; ".join(f"{row['id']}: {row['steps'][key]} ({row['evidence'][key]})" for row in rows)
        steps.append(Step(n, _verdict(row["steps"][key] for row in rows), evidence or "no fixtures"))
    units = {row.get("observed_insunits") for row in rows}
    expected_mismatch = any(row.get("units_mismatch", False) for row in rows)
    mismatch_ids = [row["id"] for row in rows if row.get("units_mismatch", False)]
    checks = [row["steps"][str(n)] for row in rows for n in range(2, 6)]
    if "FAIL" in checks or expected_mismatch:
        six = "FAIL"
    elif not rows or None in units or "UNREADABLE" in checks:
        six = "UNREADABLE"
    else:
        six = "PASS" if units == {0, 1, 6} else "FAIL"
    steps.append(Step(6, six, f"observed export INSUNITS {sorted(unit for unit in units if unit is not None)}; "
                      f"manifest unit mismatches {mismatch_ids}; checks 2 to 5 {_verdict(checks)}; see fixtures.json",
                      expected=[0, 1, 6]))
    violations = [row for row in rows if row["steps"]["2"] == "PASS" and
                  any(row["steps"][str(n)] == "FAIL" for n in (3, 4, 5))]
    visual_failures = any(row["steps"][str(n)] == "FAIL" for row in rows for n in (3, 4, 5))
    if violations:
        seven = "FAIL"
        evidence = "; ".join(f"{row['id']}: step {n}: {row['evidence'][str(n)]}"
                             for row in violations for n in (3, 4, 5) if row["steps"][str(n)] == "FAIL")
    elif not rows or visual_failures:
        seven, evidence = "UNREADABLE", "visual failures without numeric PASS, or no fixtures; see fixtures.json"
    else:
        seven, evidence = "PASS", "no numeric PASS hides a failing tag, module or layer"
    steps.append(Step(7, seven, evidence))
    return steps


def run(ctx):
    repo = Path(__file__).resolve().parents[3]
    with xbench_session.signed_in(repo, ctx.source_sha, origin=ctx.base) as (page, _):
        client = StudioClient(_transport(page, origin=ctx.base))
        with disposable_project(client, f"XBench X01 {ctx.source_sha[:8]} {utc_now()}") as project:
            rows = grade_files(client, project=project, fixtures=ctx.manifest["fixtures"], out_dir=ctx.out_dir)
            write_json_atomic(Path(ctx.out_dir) / "fixtures.json", rows)
            return score(rows)
