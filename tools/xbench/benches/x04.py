"""Raw customer DWG intake acceptance, using the Studio product routes."""

from pathlib import Path
import re
import subprocess
import time
from uuid import uuid4

from benches.x02 import _transport
from xbench_core import Step, utc_now, write_json_atomic
import xbench_session
from xbench_studio import StudioClient, StudioError, disposable_project


LIMITS = "The in-browser render check is not built; step 4 is graded from the extracted intake. When count-modules not live, step 5 uses count-by-layer (per-file limits in files.json)."


def build_multipart(filename, content):
    if any(char in filename for char in ('\r', '\n', '"')):
        raise ValueError("invalid multipart filename")
    boundary = "----xbench" + uuid4().hex
    body = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
            f'filename="{filename}"\r\nContent-Type: application/octet-stream\r\n\r\n'
            ).encode() + content + f"\r\n--{boundary}--\r\n".encode()
    return body, f"multipart/form-data; boundary={boundary}"


def find_sha(obj, sha, depth=4):
    if isinstance(obj, str):
        return bool(re.fullmatch(r"[0-9a-fA-F]{64}", obj)) and obj.lower() == sha.lower()
    if depth <= 0:
        return False
    values = obj.values() if isinstance(obj, dict) else obj if isinstance(obj, list) else ()
    return any(find_sha(value, sha, depth - 1) for value in values)


def find_dependency_report(*objs):
    for obj in objs:
        if isinstance(obj, dict):
            report = {key: value for key, value in obj.items() if any(
                word in key.lower() for word in ("xref", "dependenc", "font", "image", "underlay"))}
            if report:
                return report
    return None


def step_verdict(per_file):
    verdicts = set(per_file.values())
    return "FAIL" if "FAIL" in verdicts else (
        "UNREADABLE" if not verdicts or "UNREADABLE" in verdicts else "PASS")


def forbidden_tokens(fixture):
    explicit = "forbidden_tokens" in fixture
    candidates = (fixture["forbidden_tokens"] if explicit else
                  [fixture["id"], *fixture.get("module_blocks", [])])
    tokens, seen = [], set()
    for token in candidates:
        if ((explicit or (len(token) >= 4 and not token.upper().startswith("*U")))
                and token.lower() not in seen):
            tokens.append(token)
            seen.add(token.lower())
    return tokens


def _dependency_present(report, dep):
    """Require the inventory name and a disposition in the same report record."""
    dispositions = {"bound", "detached", "font remapped", "image omitted"}

    def visit(obj):
        if isinstance(obj, dict):
            disposition = str(obj.get("disposition", "")).lower().replace("_", " ")
            if (obj.get("name") == dep["name"] and disposition in dispositions
                    and obj.get("kind", dep["kind"]) == dep["kind"]):
                return True
            value = obj.get(dep["name"])
            if isinstance(value, str) and value.lower().replace("_", " ") in dispositions:
                return True
            if isinstance(value, dict):
                if visit({"name": dep["name"], **value}):
                    return True
            return any(visit(value) for value in obj.values())
        return isinstance(obj, list) and any(visit(value) for value in obj)

    return visit(report)


def _module_catalog(transport):
    status, body = transport("GET", "/api/tools")
    if status != 200:
        raise ValueError("catalog HTTP status")
    tools = body.get("tools") if isinstance(body, dict) else None
    if not isinstance(tools, list) or any(
            not isinstance(tool, dict) or not isinstance(tool.get("name"), str)
            or not tool["name"].strip() for tool in tools):
        raise ValueError("catalog tools")
    matches = [tool for tool in tools if tool["name"] == "count-modules"]
    if not matches:
        return None
    if len(matches) != 1:
        raise ValueError("catalog tools.name duplicate count-modules")
    tool = matches[0]
    if not isinstance(tool.get("version"), str) or not tool["version"].strip():
        raise ValueError("catalog version")
    if not isinstance(tool.get("catalog_digest"), str) or not re.fullmatch(
            r"(?:sha256:)?[0-9a-fA-F]{64}", tool["catalog_digest"]):
        raise ValueError("catalog catalog_digest")
    return {key: tool[key] for key in ("name", "version", "catalog_digest")}


def _module_sets(value, field):
    if not isinstance(value, list):
        raise ValueError(field)
    sets = {}
    for item in value:
        if not isinstance(item, dict):
            raise ValueError(field + " item")
        for key in ("set_id", "layer", "block", "entity_type"):
            if not isinstance(item.get(key), str):
                raise ValueError(field + "." + key)
        if not item["set_id"].strip() or item["set_id"] in sets:
            raise ValueError(field + ".set_id")
        if item["entity_type"] not in ("INSERT", "LWPOLYLINE"):
            raise ValueError(field + ".entity_type")
        if type(item.get("count")) is not int or not 0 <= item["count"] <= 2**53 - 1:
            raise ValueError(field + ".count")
        sets[item["set_id"]] = {key: item[key] for key in (
            "set_id", "layer", "block", "entity_type", "count")}
    return sets


def _module_result(transport, project, adoption, tool, selector):
    # The picker reruns the same tool with set_ids; wait=1 returns its envelope.
    status, body = transport(
        "POST", "/api/run?wait=1",
        headers={"X-Org-Id": project["org_id"], "X-Project-Id": project["project_id"]},
        json_body={"tool": tool["name"], "catalog_digest": tool["catalog_digest"],
                   "dwg": adoption["version_id"],
                   "params": {"drawing_id": adoption["drawing_id"], **selector}})
    if status in (404, 405, 501) and not selector:
        raise ValueError("module-set picker not live")
    if status != 200:
        raise ValueError("count-modules HTTP status")
    if not isinstance(body, dict) or body.get("ok") is not True:
        raise ValueError("count-modules ok")
    if "error" not in body or body["error"] is not None:
        raise ValueError("count-modules error")
    for key in ("tool", "version"):
        if body.get(key) != tool["name" if key == "tool" else key]:
            raise ValueError("count-modules " + key)
    result = body.get("result")
    if not isinstance(result, dict):
        raise ValueError("count-modules result")
    return result


def _grade_modules(transport, project, adoption, fixture, tool, row, mark):
    field = "count-modules candidate response"
    try:
        listing = _module_result(transport, project, adoption, tool, {})
        candidates = _module_sets(listing.get("sets"), "sets")
        if not candidates:
            raise ValueError("sets empty")
        for key, expected in (("selection_required", True), ("selected_sets", []),
                              ("selected_set", None), ("count", None)):
            if key not in listing or type(listing[key]) is not type(expected) or listing[key] != expected:
                raise ValueError("candidate " + key)
        row["candidate_sets"] = sorted(
            candidates.values(), key=lambda item: (-item["count"], item["set_id"]))[:50]
        selector = fixture.get("module_set")
        if selector is None:
            top = "; ".join(
                f"{item['set_id']} ({item['layer']}/{item['block']}, {item['count']})"
                for item in row["candidate_sets"][:3])
            reason = f"no module_set bound in manifest; {len(candidates)} candidates; top three: {top}"
            mark(5, "UNREADABLE", reason)
            mark(6, "UNREADABLE" if len(candidates) > 1 else "PASS",
                 reason if len(candidates) > 1 else "nothing to pick")
            return
        field = "manifest module_set"
        if not isinstance(selector, dict) or not selector or set(selector) - {"set_id", "set_ids"}:
            raise ValueError(field)
        if "set_id" in selector and not isinstance(selector["set_id"], str):
            raise ValueError("module_set.set_id")
        if "set_ids" in selector and (not isinstance(selector["set_ids"], list) or any(
                not isinstance(value, str) or not value.strip() for value in selector["set_ids"])):
            raise ValueError("module_set.set_ids")
        ids = selector.get("set_ids") or ([selector["set_id"]] if selector.get("set_id") else [])
        if not ids or any(value not in candidates for value in ids):
            raise ValueError("module_set set_id absent from sets")
        # Match the picker callback even for the schema's singular set_id form.
        ids = list(dict.fromkeys(ids))
        field = "count-modules selection response"
        result = _module_result(transport, project, adoption, tool, {"set_ids": ids})
        if _module_sets(result.get("sets"), "sets") != candidates:
            raise ValueError("sets changed during selection")
        selected = _module_sets(result.get("selected_sets"), "selected_sets")
        if selected != {key: candidates[key] for key in ids}:
            raise ValueError("selected_sets do not match module_set")
        expected_single = next(iter(selected.values())) if len(selected) == 1 else None
        if "selected_set" not in result:
            raise ValueError("selected_set")
        if expected_single is None:
            if result["selected_set"] is not None:
                raise ValueError("selected_set")
        elif _module_sets([result["selected_set"]], "selected_set") != selected:
            raise ValueError("selected_set")
        if result.get("selection_required") is not False:
            raise ValueError("selection_required")
        count = result.get("count")
        if type(count) is not int or not 0 <= count <= 2**53 - 1:
            raise ValueError("count")
        # Sets can overlap; the total is a union, not necessarily their sum.
        if not max(item["count"] for item in selected.values()) <= count <= sum(
                item["count"] for item in selected.values()):
            raise ValueError("count inconsistent with selected_sets")
        row["counts"].append(count)
        row["module_set"] = {"set_ids": ids}
        mark(6, "PASS", "module_set selected" if len(candidates) > 1 else "nothing to pick")
        mark(5, "PASS" if count == fixture["modules"] else "FAIL",
             f"count {count}, oracle {fixture['modules']}")
    except Exception as exc:
        reason = str(exc) if isinstance(exc, ValueError) else field + " unavailable"
        mark(5, "UNREADABLE", reason)
        mark(6, "UNREADABLE", reason)


def grade_files(client, transport, *, project, fixtures, clock=time.monotonic):
    rows = {}
    for fixture in fixtures:
        fid = fixture["id"]
        row = {"id": fid, "timings": {}, "statuses": {}, "counts": [], "limits": [],
               "steps": {str(n): "UNREADABLE" for n in range(1, 7)},
               "evidence": {str(n): "upload unavailable" for n in range(1, 7)},
               "insert_fixture": fixture.get("module_entity") == "INSERT" and
                                 bool(fixture.get("module_blocks"))}
        rows[fid] = row

        def mark(n, verdict, evidence):
            row["steps"][str(n)] = verdict
            row["evidence"][str(n)] = evidence

        started = clock()
        try:
            body, content_type = build_multipart(fid + ".dwg", Path(fixture["path"]).read_bytes())
            status, uploaded = transport("POST", "/api/drawings/upload", data=body,
                                         headers={"Content-Type": content_type}, timeout_s=300)
            row["statuses"]["upload"] = status
            if not 200 <= status < 300:
                mark(1, "FAIL", f"upload HTTP {status}")
                continue
            upload = client.wait_upload(uploaded["drawing_id"], timeout_s=300, poll_s=3)
            row["statuses"]["terminal"] = "ready" if upload.get("status") == "ready" else "not ready"
            if upload.get("status") != "ready" or clock() - started > 300:
                mark(1, "FAIL", "upload not ready within 300 seconds")
                continue
            mark(1, "PASS", "ready within deadline")
        except Exception as exc:
            if isinstance(exc, StudioError):
                row["statuses"]["upload_error"] = exc.status
            mark(1, "FAIL", "upload failed or timed out")
            continue
        finally:
            row["timings"]["upload_s"] = round(clock() - started, 3)

        adoption, intake = {}, None
        try:
            adoption = client.adopt_drawing(project["project_id"], uploaded["drawing_id"], name=fid)
            intake = client.drawing_context(project["project_id"], adoption["version_id"],
                                            adoption["drawing_id"])["intake"]
        except Exception as exc:
            message = xbench_session.redact(str(exc))
            if isinstance(exc, StudioError):
                code = exc.reason_code or exc.error_code
                if isinstance(code, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{1,63}", code):
                    message = f"{code}: {xbench_session.redact(exc.message)}"
            evidence = f"adoption or intake unavailable: {type(exc).__name__}: {message[:300]}"
            for n in range(2, 7):
                mark(n, "UNREADABLE", evidence)
            continue

        provenance = adoption.get("provenance", {})
        report = find_dependency_report(upload, provenance, intake)
        if report is None:
            mark(2, "FAIL", "no dependency report exposed")
        elif "deps" not in fixture:
            mark(2, "UNREADABLE", "dependency inventory unavailable")
        else:
            ok = all(_dependency_present(report, dep) for dep in fixture["deps"])
            mark(2, "PASS" if ok else "FAIL", "dependency dispositions complete" if ok else
                 "dependency missing or without disposition")
        retained = find_sha(provenance, fixture["sha256"]) or find_sha(upload, fixture["sha256"])
        mark(3, "PASS" if retained else "FAIL", "original SHA-256 exposed" if retained else
             "original SHA-256 not exposed")
        whole = fixture.get("design_is_whole_layer") is True
        mark(6, "PASS" if whole else "UNREADABLE", "nothing to pick" if whole else
             "module-set picker not live")
        if intake is None:
            continue
        if not row["insert_fixture"]:
            mark(4, "PASS", "not applicable")
        else:
            inserts = intake.get("inserts", [])
            names = {insert.get("name") for insert in inserts}
            blocks = intake.get("blocks", {})

            def has_geometry(name):
                return any(child.get("kind") in ("LINE", "LWPOLYLINE", "CIRCLE", "ARC")
                           for child in blocks.get(name, {}).get("children", []))

            matched = list(dict.fromkeys(
                insert["name"] for insert in inserts
                if insert.get("layer") in fixture.get("module_layers", [])
                and has_geometry(insert.get("name"))))
            missing = [name for name in fixture["module_blocks"]
                       if not (matched if name.startswith("*") else
                               name in names and has_geometry(name))]
            evidence = ("missing insert or geometry: " + ", ".join(missing)
                        if missing else "module inserts have block geometry")
            if matched and any(name.startswith("*") for name in fixture["module_blocks"]):
                evidence += "; matched blocks: " + ", ".join(matched[:5])
            mark(4, "FAIL" if missing else "PASS", evidence)
        try:
            tool = _module_catalog(transport)
        except Exception as exc:
            reason = str(exc) if isinstance(exc, ValueError) else "catalog tools unavailable"
            mark(5, "UNREADABLE", reason)
            mark(6, "UNREADABLE", reason)
            continue
        if tool is not None:
            row["tool"] = tool
            _grade_modules(transport, project, adoption, fixture, tool, row, mark)
            continue
        row["limits"].append("count-modules not live")
        layers = list(dict.fromkeys(fixture.get("module_layers", [])))
        if not layers:
            mark(5, "UNREADABLE", "module layers unavailable")
            continue
        try:
            for layer in layers:
                status, body = client.run("count-by-layer", org_id=project["org_id"],
                                          project_id=project["project_id"], version_id=adoption["version_id"],
                                          params={"drawing_id": adoption["drawing_id"], "layer": layer})
                row["statuses"].setdefault("count", []).append(status)
                if status != 200:
                    raise ValueError("count not inline")
                count = body["result"]["counts"][layer]
                if type(count) is not int or count < 0:
                    raise ValueError("invalid count")
                row["counts"].append(count)
            total = sum(row["counts"])
            verdict = "PASS" if total == fixture["modules"] else "FAIL" if whole else "UNREADABLE"
            mark(5, verdict, f"count {total}, oracle {fixture['modules']}" if whole or verdict == "PASS"
                 else "competing-set pick needs the UI, not built")
        except Exception:
            mark(5, "UNREADABLE", "layer count unavailable")
    return rows


def score(rows, step7):
    steps = []
    for n in range(1, 7):
        verdicts = {fid: row["steps"][str(n)] for fid, row in rows.items()}
        verdict = step_verdict(verdicts)
        if n == 4 and verdict != "FAIL" and not any(row["insert_fixture"] for row in rows.values()):
            verdict = "UNREADABLE"
        evidence = "; ".join(f"{fid}: {verdicts[fid]} ({row['evidence'][str(n)][:130]})"
                             for fid, row in rows.items()) or "no fixtures"
        if n == 4 and verdict == "UNREADABLE" and not any(row["insert_fixture"] for row in rows.values()):
            evidence += "; no INSERT fixtures"
        steps.append(Step(n, verdict, evidence[:1999]))
    steps.append(Step(7, step7[0], step7[1][:1999]))
    return steps


def scan_source(repo, sha, tokens, runner=subprocess.run):
    def git(args, timeout):
        return runner(["git", *args], cwd=repo, capture_output=True, text=True, timeout=timeout)

    try:
        if git(["cat-file", "-e", sha + "^{commit}"], 120).returncode != 0:
            git(["fetch", "--quiet", "https://forge.leafdesign.ai/LEAF-Solar-Design/leaf-web-demo.git", sha], 300)
            if git(["cat-file", "-e", sha + "^{commit}"], 120).returncode != 0:
                return "UNREADABLE", "source commit unavailable after fetch"
        tokens = list(dict.fromkeys(tokens))
        if not tokens:
            return "PASS", "no forbidden tokens"
        result = git(["grep", "-n", "-I", "-F", "-i", *[arg for token in tokens for arg in ("-e", token)],
                      sha, "--", "server", "web", "platform", "engine", "da", "executor"], 120)
        if result.returncode == 1:
            return "PASS", "no forbidden tokens in deployed source"
        if result.returncode != 0:
            return "UNREADABLE", "source search failed"
        hits = []
        for line in result.stdout.splitlines():
            parts = line.split(":", 3)
            if len(parts) != 4 or parts[0] != sha or not parts[2].isdigit():
                return "UNREADABLE", "source search output unreadable"
            for token in tokens:
                if token.lower() in parts[3].lower():
                    hit = f"{token}: {parts[1]}"
                    if hit not in hits:
                        hits.append(hit)
        return ("FAIL", "; ".join(hits)[:1999]) if hits else ("UNREADABLE", "source hits unreadable")
    except (OSError, subprocess.TimeoutExpired):
        return "UNREADABLE", "source inspection unavailable or timed out"


def run(ctx):
    repo = Path(__file__).resolve().parents[3]
    # ctx.fixtures contains verification receipts; the manifest retains oracle fields.
    fixtures = ctx.manifest["fixtures"]
    with xbench_session.signed_in(repo, ctx.source_sha, origin=ctx.base) as (page, _):
        transport = _transport(page, origin=ctx.base)
        client = StudioClient(transport)
        with disposable_project(client, f"XBench X04 {ctx.source_sha[:8]} {utc_now()}") as project:
            rows = grade_files(client, transport, project=project, fixtures=fixtures)
            write_json_atomic(Path(ctx.out_dir) / "files.json", rows)
    tokens = [token for fixture in fixtures for token in forbidden_tokens(fixture)]
    step7 = scan_source(ctx.manifest.get("source_repo", "D:/GitHub/leaf-web-demo"), ctx.source_sha, tokens)
    return score(rows, step7)
