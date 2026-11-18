"""Grade Studio's converted original against its manifest-driven solar export."""

from contextlib import nullcontext
from copy import deepcopy
import hashlib
from pathlib import Path
import re

from benches.x02 import _transport
from xbench_core import EXIT_USAGE, Step, utc_now, write_json_atomic
import x01_geometry as geometry
import xbench_session
from xbench_studio import StudioClient, StudioError, disposable_project, new_idempotency_key, parse_error


LIMITS = "X01: original and export both come from Studio's own DWG conversion (version 1 vs head after manifest-driven tools); step 3 requires recorded stringing settings; the plot is an ezdxf render, not an AutoCAD plot."
_RESUMABLE_TOOLS = frozenset({"solar-settings", "solar-panels-from-drawing",
                              "solar-panel-groups", "solar-string-add", "xbench-string-tags"})


def _row(fixture):
    return {"id": fixture["id"], "drawing_id": None, "original_version_id": None,
            "upload_drawing_id": None, "import_provenance": None, "failed_stage": None,
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
        if row.get("stringing_error"):
            _mark(row, 3, "UNREADABLE", row["stringing_error"])
        elif fixture.get("stringing") is None:
            _mark(row, 3, "UNREADABLE", "no recorded stringing settings in manifest")
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
        tag_layers = row.get("tag_layers", fixture.get("tag_layers", []))
        tags = list(geometry._tag_entities(exported, tag_layers))
        recorded_scale = fixture.get("sheet_scale")
        scale = recorded_scale if recorded_scale is not None else geometry.sheet_scale(exported)
        row["tag_count"], row["sheet_scale"] = len(tags), scale
        row["sheet_scale_source"] = "manifest" if recorded_scale is not None else "viewport"
        if fixture.get("stringing") is None:
            _mark(row, 3, "UNREADABLE", "no recorded stringing settings in manifest")
        elif row.get("stringing_error"):
            _mark(row, 3, "UNREADABLE", row["stringing_error"])
        elif not tags:
            _mark(row, 3, "FAIL", "stringing ran but no tags exported")
        elif scale is None:
            _mark(row, 3, "UNREADABLE", "tags present, but no paper-space model viewport scale")
        else:
            checks = geometry.tag_checks(exported, tag_layers, scale,
                                         model_units_per_paper_mm=recorded_scale)
            failures = [_item(exported, handle, reason="tag below 1.8 mm", paper_mm=height)
                        for handle, height in checks["too_small"]]
            failures.extend(_item(exported, handle, reason="tag overlap exceeds 10%",
                                  other=_item(exported, other), fraction=fraction)
                            for handle, other, fraction in checks["overlaps"])
            _mark(row, 3, "FAIL" if failures else "PASS",
                  f"tags {checks['count']}; scale {scale}; failures {failures}", failures)
    except Exception as exc:
        note = ("no recorded stringing settings in manifest" if fixture.get("stringing") is None else
                row.get("stringing_error") or f"tag analysis unavailable: {type(exc).__name__}")
        _mark(row, 3, "UNREADABLE", note)
    try:
        if points is None:
            raise ValueError("module selection unavailable")
        handles = [handle for handle, _, _ in points]
        handles.extend(entity.dxf.handle for entity in geometry._tag_entities(
            exported, row.get("tag_layers", fixture.get("tag_layers", []))))
        failures = [{"handle": handle, "layer": layer, "reason": reason}
                    for handle, layer, reason in geometry.entities_on_bad_layers(exported, handles)]
        _mark(row, 4, "FAIL" if failures else "PASS", f"bad layers {failures}", failures)
    except Exception as exc:
        _mark(row, 4, "UNREADABLE", f"layer analysis unavailable: {type(exc).__name__}")
    try:
        footprints = geometry.module_footprints(exported, layers, blocks)
        covering, unanalysable = geometry.fill_cover(exported, footprints)
        failures = [_item(exported, handle, reason="fill covers more than 5%",
                          fill=_item(exported, fill), fraction=fraction)
                    for handle, fill, fraction in covering]
        verdict = "FAIL" if failures else "PASS"
        evidence = f"covering fills {failures}"
        if unanalysable:
            evidence += f"; unanalysable fills {len(unanalysable)}; handles {unanalysable[:10]}"
            if not failures and len(unanalysable) == len(exported.modelspace().query("HATCH SOLID TRACE")):
                verdict = "UNREADABLE"
        _mark(row, 5, verdict, evidence, failures)
    except Exception as exc:
        _mark(row, 5, "UNREADABLE", f"fill analysis unavailable: {type(exc).__name__}")
    return row


def _product_refusal(exc):
    return (isinstance(exc, StudioError) and 400 <= exc.status < 500
            and exc.status not in (401, 403, 408, 429)
            and exc.error_code != "AUTH_DENIED")


def _tool_result(body):
    """Unwrap the job record and normal result envelope, checking both for refusal."""
    if not isinstance(body, dict):
        raise StudioError(502, "INVALID_TOOL_RESPONSE", "Expected a tool result object")
    while True:
        state = body.get("status")
        if (state in ("failed", "refused", "cancelled", "canceled") or body.get("ok") is False
                or body.get("error")):
            error = parse_error(422, body)
            error.reason_code = error.reason_code or body.get("reason_code")
            if not (error.reason_code or error.error_code):
                error = StudioError(422, body.get("error_code") or "TOOL_FAILED",
                                    body.get("message", "Tool did not complete"),
                                    reason_code=body.get("reason_code"), body=body)
            error.result_status = state if state in ("failed", "refused", "cancelled", "canceled") else "failed"
            raise error
        if state in ("submitted", "queued", "running", "pending"):
            raise StudioError(502, "TOOL_NOT_SETTLED", "Tool did not return a terminal result")
        if not isinstance(body.get("result"), dict):
            return body
        body = body["result"]


def _source_hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class _StoppedAfter(Exception):
    """Leave the fixture after an admitted tool succeeds, retaining cleanup."""


def grade_files(client, *, project, fixtures, out_dir=None, resume=None):
    rows = {}
    for fixture in fixtures:
        if resume is not None and fixture["id"] != resume["fixture"]:
            continue
        row = _row(fixture)
        stop_after = resume.get("stop_after") if resume is not None else None
        completed = []
        if stop_after is not None:
            row["completed_calls"] = completed
        rows[fixture["id"]] = row
        capability, did, source = None, None, None
        stage = "upload"
        pid = project["project_id"]
        try:
            if resume is not None:
                did, version = resume["drawing"], resume["version"]
                row["drawing_id"], row["original_version_id"] = did, version
                row["resumed"] = True
                stage = "source_hash"
                source = _source_hash(fixture["path"])
                row["source_sha256_before"] = source
                if Path(fixture["path"]).stat().st_size != fixture["bytes"] or source != fixture["sha256"]:
                    raise ValueError("fixture differs from manifest")
            else:
                source = Path(fixture["path"]).read_bytes()
                row["source_sha256_before"] = hashlib.sha256(source).hexdigest()
                if (len(source) != fixture["bytes"] or row["source_sha256_before"] != fixture["sha256"]):
                    raise ValueError("fixture differs from manifest")
                stage = "upload"
                upload = client.upload_drawing(Path(fixture["path"]).name, source)
                row["upload_drawing_id"] = upload
                stage = "wait_upload"
                client.wait_upload(upload)
                stage = "adopt"
                drawing = client.adopt_drawing(pid, upload, fixture["id"])
                provenance = drawing.get("provenance")
                source_provenance = provenance.get("source") if isinstance(provenance, dict) else None
                if isinstance(source_provenance, dict):
                    row["import_provenance"] = {key: source_provenance[key]
                                                for key in ("drawing_id", "version") if key in source_provenance}
                    intake = source_provenance.get("intake")
                    if isinstance(intake, dict):
                        row["import_provenance"]["intake"] = {key: intake[key]
                                                             for key in ("ref", "sha256") if key in intake}
                did, version = drawing["drawing_id"], drawing["version_id"]
                row["drawing_id"], row["original_version_id"] = did, version
                stage = "original_dxf"
                original = client.dxf_bytes(pid, did, version)
                row["dxf_sha256"]["original"] = hashlib.sha256(original).hexdigest()
            stage = "checkout"
            capability, _ = client.checkout(pid, did, ttl_s=900)
            completed.append(stage)
            stage = "context"
            context = client.drawing_context(pid, version, did)
            completed.append(stage)
            rev = context.get("intake", {}).get("solar_design_graph", {}).get("rev", 0)

            def refresh():
                nonlocal version, rev, context, stage
                stage = "refresh_context"
                context = client.drawing_context(pid, version, did)
                completed.append(stage)
                version = context["context"]["head_version_id"]
                stage = "refresh_context"
                context = client.drawing_context(pid, version, did)
                completed.append(stage)
                rev = context.get("intake", {}).get("solar_design_graph", {}).get("rev", rev + 1)

            def execute(tool, params, *, receipt=None):
                nonlocal stage
                stage = f"run:{tool}"
                status, body = client.run(tool, org_id=project["org_id"], project_id=pid,
                                          version_id=version, params=params, capability=capability,
                                          idempotency_key=new_idempotency_key("x01"))
                if receipt is not None:
                    receipt["http_status"] = status
                    if isinstance(body, dict):
                        receipt["run_id"] = body.get("run_id") or body.get("job_id")
                if not 200 <= status < 300:
                    raise parse_error(status, body)
                if status == 202:
                    if not isinstance(body, dict) or not body.get("job_id"):
                        raise StudioError(502, "INVALID_JOB_RESPONSE", "Write accepted without a job ID")
                    stage = f"run:{tool}"
                    body = client.wait_job(body["job_id"], timeout_s=600)
                if receipt is not None and isinstance(body, dict):
                    receipt["status"] = body.get("status", "complete")
                    receipt["run_id"] = receipt["run_id"] or body.get("run_id") or body.get("job_id")
                result = _tool_result(body)
                return result

            def call(tool, params, *, receipt=None):
                nonlocal stage
                if stop_after is not None and (tool != stop_after or "stopped_after" in row):
                    stage = f"run:{tool}"
                    raise ValueError(f"unadmitted tool {tool}: scope ends at {stop_after}")
                tracked = receipt if receipt is not None else {"run_id": None}
                try:
                    result = execute(tool, params, receipt=tracked if stop_after is not None else receipt)
                except Exception as exc:
                    if tool == stop_after:
                        row["stopped_after"] = {
                            "tool": tool, "outcome": "failure",
                            "http_status": tracked.get("http_status", getattr(exc, "status", None)),
                            "job_status": tracked.get("status") if tracked.get("http_status") == 202 else None}
                    raise
                completed.append(stage)
                if tool == stop_after:
                    row["stopped_after"] = {
                        "tool": tool, "outcome": "success", "http_status": tracked["http_status"],
                        "job_status": tracked.get("status") if tracked["http_status"] == 202 else None}
                    raise _StoppedAfter()
                refresh()
                return result

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
            if fixture.get("stringing") is not None:
                row["stringing_runs"] = []
                receipt = None
                try:
                    settings = fixture["stringing"]
                    tools = [*settings.get("tools", []), settings["tag_tool"]]
                    for index, tool in enumerate(tools):
                        params = deepcopy(tool.get("params", {}))
                        receipt = {"name": tool["name"], "params": params,
                                   "run_id": None, "status": "not_started"}
                        row["stringing_runs"].append(receipt)
                        # Bind session-specific fields only when the catalog accepts them.
                        stage = f"run:{tool['name']}"
                        catalog = next((entry for entry in client.tools() if entry.get("name") == tool["name"]), {})
                        properties = catalog.get("params", {}).get("properties", {})
                        for key, value in (("drawing_id", did), ("expected_rev", rev)):
                            if key in properties or key in params:
                                params[key] = value
                        result = call(tool["name"], params, receipt=receipt)
                        if index == len(tools) - 1:
                            reported = result.get("tag_layers")
                            if reported is None:
                                reported = result.get("tag_layer", result.get("layers", result.get("layer")))
                            if isinstance(reported, str):
                                reported = [reported]
                            if isinstance(reported, list) and all(isinstance(layer, str) for layer in reported):
                                row["tag_layers"] = reported
                except _StoppedAfter:
                    raise
                except Exception as exc:
                    if "stopped_after" in row:
                        raise
                    name = receipt["name"] if receipt else "stringing configuration"
                    error_body = exc.body if isinstance(exc, StudioError) and isinstance(exc.body, dict) else {}
                    code = (exc.reason_code or error_body.get("reason_code") or exc.error_code or "TOOL_FAILED"
                            if isinstance(exc, StudioError) else type(exc).__name__)
                    message = str(exc.message) if isinstance(exc, StudioError) else str(exc)
                    message = xbench_session.redact(message.replace(capability, "[redacted]"))
                    safe_code = (code if isinstance(code, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{1,63}", code)
                                 else xbench_session.redact(str(code)))
                    row["stringing_error"] = f"{name}: {safe_code}: {message[:300]}"
                    if receipt is not None:
                        outcome = getattr(exc, "result_status", "refused" if isinstance(exc, StudioError)
                                          and 400 <= exc.status < 500 else "failed")
                        receipt.update(status=outcome, reason_code=safe_code, message=message[:300])
                        if isinstance(exc, StudioError):
                            receipt["http_status"] = exc.status
                            if isinstance(exc.body, dict):
                                receipt["run_id"] = receipt["run_id"] or exc.body.get("run_id") or exc.body.get("job_id")
                    # A refused run may still have published a drawing version.
                    refresh()
            row["export_version_id"] = version
            stage = "export_dxf"
            exported = client.dxf_bytes(pid, did, version)
            if resume is None:
                grade_pair(fixture, original, exported, row=row)
            else:
                row["dxf_sha256"]["export"] = hashlib.sha256(exported).hexdigest()
                _mark(row, 1, "UNREADABLE", "resume export SHA-256 recorded; original DXF skipped")
                for n in range(2, 6):
                    _mark(row, n, "UNREADABLE", "resume skipped original DXF; pair comparison unavailable")
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
        except _StoppedAfter:
            for n in range(1, 6):
                _mark(row, n, "UNREADABLE", f"not run: stopped after {stop_after} per admitted scope")
        except Exception as exc:
            row["failed_stage"] = stage
            verdict = "FAIL" if _product_refusal(exc) else "UNREADABLE"
            if isinstance(exc, StudioError):
                code = exc.reason_code or exc.error_code or "STUDIO_REFUSAL"
                message = str(exc.message).replace(capability, "[redacted]") if capability else str(exc.message)
                if isinstance(code, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{1,63}", code):
                    note = f"{code}: {xbench_session.redact(message)}"
                else:
                    note = xbench_session.redact(f"{code}: {message}")
                row["product_error"] = {"status": exc.status, "code": code, "message": note, "stage": stage}
            else:
                note = f"fixture unavailable: {type(exc).__name__}: {xbench_session.redact(str(exc))[:300]}"
            for n in range(2, 6):
                _mark(row, n, verdict, note)
            _mark(row, 1, "UNREADABLE", note)
        finally:
            if capability is not None:
                try:
                    stage = "release"
                    client.release(pid, did, capability)
                except Exception as exc:
                    row["release_note"] = f"checkout release unavailable: {type(exc).__name__}"
            if source is not None:
                try:
                    after = (_source_hash(fixture["path"]) if resume is not None else
                             Path(fixture["path"]).read_bytes())
                    row["source_sha256_after"] = (after if resume is not None else
                                                  hashlib.sha256(after).hexdigest())
                    unchanged = after == source
                    if resume is not None:
                        _mark(row, 1, row["steps"]["1"] if unchanged else "FAIL",
                              row["evidence"]["1"] + "; source unchanged" if unchanged else
                              "source fixture changed")
                    else:
                        fetched = set(row["dxf_sha256"]) == {"original", "export"}
                        _mark(row, 1, "FAIL" if not unchanged else "PASS" if fetched else row["steps"]["1"],
                              "source fixture changed" if not unchanged else
                              "normal-route original/export SHA-256 recorded; source unchanged" if fetched else
                              row["evidence"]["1"] + "; source unchanged, export unavailable")
                except OSError:
                    _mark(row, 1, "FAIL", "source fixture unavailable after run")
        if stop_after is not None and "stopped_after" not in row:
            row["stopped_after"] = {"tool": stop_after, "outcome": "not_reached",
                                    "http_status": None, "job_status": None}
            _mark(row, 1, "FAIL", "stop_after tool not reached")
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
    options = getattr(ctx, "options", {})
    resume_keys = ("resume_project", "resume_drawing", "resume_version", "resume_fixture")
    resume = None
    if "resume_stop_after" in options:
        stop_after = options["resume_stop_after"]
        if (not isinstance(stop_after, str) or
                re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", stop_after) is None or
                stop_after not in _RESUMABLE_TOOLS or
                not all(options.get(key) for key in resume_keys)):
            raise SystemExit(EXIT_USAGE)
    if any(key in options for key in resume_keys):
        if not all(options.get(key) for key in resume_keys):
            raise SystemExit(EXIT_USAGE)
        resume = {key.removeprefix("resume_"): options[key] for key in resume_keys}
        if "resume_stop_after" in options:
            resume["stop_after"] = options["resume_stop_after"]
        if not any(fixture["id"] == resume["fixture"] for fixture in ctx.manifest["fixtures"]):
            raise SystemExit(EXIT_USAGE)
    repo = Path(__file__).resolve().parents[3]
    with xbench_session.signed_in(repo, ctx.source_sha, origin=ctx.base) as (page, _):
        client = StudioClient(_transport(page, origin=ctx.base))
        keep_project = getattr(ctx, "options", {}).get("keep_project") == "1"
        name = f"XBench X01 {ctx.source_sha[:8]} {utc_now()}"
        project_scope = (nullcontext(client.project(resume["project"])) if resume is not None else
                         nullcontext(client.create_project(name)) if keep_project else
                         disposable_project(client, name))
        with project_scope as project:
            rows = grade_files(client, project=project, fixtures=ctx.manifest["fixtures"], out_dir=ctx.out_dir,
                               resume=resume)
            if keep_project:
                for row in rows.values():
                    row["kept_project_id"] = project["project_id"]
            write_json_atomic(Path(ctx.out_dir) / "fixtures.json", rows)
            return score(rows)
