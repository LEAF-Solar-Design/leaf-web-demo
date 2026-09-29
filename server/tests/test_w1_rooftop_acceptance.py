"""One hermetic rooftop HTTP scenario; six independent checkpoint assertions.

Cloud responses are recordings; upload, checkout, admission, broker execution,
proposal validation and intake-embedded version publication remain real.
"""
import copy
import hashlib
import io
import json
import math
import sys
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path

import pytest

SERVER = Path(__file__).resolve().parents[1]
ROOT = SERVER.parent
sys.path.insert(0, str(SERVER))
sys.path.insert(0, str(SERVER / "tests"))
sys.path.insert(0, str(ROOT / "scripts"))

import deps
import entitlements
import jobs
import leaf_cloud_client as cloud
import solar_sizing_client as sizing
import solar_tools
import write_loop  # noqa: F401  (puts ../da on sys.path for store)
import store
from intake_dxf import intake_to_dxf
from leaf_cloud_grants import CloudGrant
from solar_equipment import equipment_ready
from solar_solve_request import build_stringer_request
from solar_w1_studio_solve import matching_response, normalized_handle, replay_responses
from test_w1_seed_product_path import HEADERS, TENANT, checkout, product
from test_w1_local_graph_jobs import isolated_jobs, no_network

INTAKE_SHA256 = "52506008e5cde459c8d3f695eb787a8021f2cacf4a02f499ea390cdc4d891a9a"
UNITS = {
    "drawing_units": "in",
    "wcs_to_ucs": [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1],
    "elevation_datum": "unrecorded", "crs": None,
}
PANEL_COUNTS = [111, 104, 99, 137, 134, 123, 174]
STRING_COUNTS = [8, 8, 8, 10, 10, 9, 13]
MATRIX_SHAPES = [(9, 15), (18, 7), (13, 10), (10, 15), (16, 13), (21, 13), (15, 13)]
CHAIN = ("inverter-add", "homeruns", "panel-group-create", "solve", "string-sizer")
OUT_OF_CHAIN = (
    ("cable-export", "This chain produces a circuit schedule, not the cable-export artifact."),
    ("electrical-zone-add", "The frozen rooftop case uses global sizing and creates no electrical zones."),
    ("electrical-zone-assign-panels", "The frozen rooftop case has no zone membership assignment."),
    ("panel-group-create-zone-aware", "Groups use globally confirmed sizing, not zone-specific sizing."),
    ("unit-sync", "Units are supplied explicitly at graph initialization; no unit-sync operation runs."),
    ("leaf-workflow-palette", "This hermetic backend acceptance does not operate the plugin palette."),
    ("leaf-platform-webview", "This hermetic backend acceptance does not operate the embedded platform webview."),
)


def digest(value):
    return hashlib.sha256(cloud.canonical_bytes(value)).hexdigest()


def valid(items):
    assert all(item["validity"] == {"state": "valid", "reasons": []} for item in items)


def rectangles(intake):
    panels = [p for p in intake["polylines"] if "panel" in p["layer"].lower()]
    result = {normalized_handle(p["handle"]): p for p in panels}
    assert len(result) == len(panels) == 882
    assert all(p["closed"] is True and len(p["pts"]) == 4 for p in panels)
    return result


def equivalent_panels(recorded, uploaded):
    before, after = rectangles(recorded), rectangles(uploaded)
    assert before.keys() == after.keys()
    for handle, source in before.items():
        target = after[handle]
        assert (target["layer"], target["closed"], len(target["pts"])) == (
            source["layer"], source["closed"], len(source["pts"]))
        for a, b in zip(source["pts"], target["pts"]):
            assert len(a) == len(b) == 3
            assert b == pytest.approx(a, abs=0.001, rel=0)


def frame_strings(graph, frame):
    members = set(frame["panel_refs"])
    selected = [s for s in graph["strings"] if members.intersection(s["ordered_panel_refs"])]
    assert all(set(s["ordered_panel_refs"]) <= members for s in selected)
    return selected


def membership_views(graph):
    panels = {p["id"]: p for p in graph["panels"]}
    strings = {s["id"]: s for s in graph["strings"]}
    assigned = []
    for string in strings.values():
        refs = string["ordered_panel_refs"]
        assert string["module_count"] == len(refs)
        # Equipment repoints to_ref at the inverter (solar_equipment); a later correction rewrites the
        # corrected string's ends to its panels and leaves inverter_ref for re-assignment
        # (solar_solve_results.correct_graph), so only that stale string points back at a panel.
        corrected = string["validity"] == {"state": "stale", "reasons": ["upstream_corrected"]}
        terminal = string.get("inverter_ref") if string.get("inverter_ref") and not corrected else refs[-1]
        assert (string["from_ref"], string["to_ref"]) == (refs[0], terminal)
        for seq, ref in enumerate(refs):
            assert panels[ref]["assignment"] == {"string_ref": string["id"], "seq": seq}
        assigned.extend(refs)
    assert len(assigned) == len(set(assigned))
    for frame in graph["frames"]:
        expected = {s["id"]: s["ordered_panel_refs"] for s in frame_strings(graph, frame)}
        assert {s["string_ref"]: s["ordered_panel_refs"] for s in frame["sequences"]} == expected
        for item in frame["panel_assignments"] + [
                cell for row in frame["matrix"] for cell in row if cell["panel_ref"] is not None]:
            assignment = panels[item["panel_ref"]]["assignment"]
            assert item["seq"] == assignment["seq"]
            if "string_ref" in item:
                assert item["string_ref"] == assignment["string_ref"]
    return set(assigned)


def recorded_strings(graph, recordings):
    handles = {p["id"]: normalized_handle(p["provenance"]["source_handle"])
               for p in graph["panels"]}
    for index, recording in enumerate(recordings):
        frame = next(f for f in graph["frames"] if f["name"] == f"Group {index + 1}")
        strings = frame_strings(graph, frame)
        assert len(strings) == STRING_COUNTS[index]
        assert sorted(tuple(handles[r] for r in s["ordered_panel_refs"]) for s in strings) == sorted(
            tuple(normalized_handle(h) for h in refs) for refs in recording["plugin_strings"])
        for string in strings:
            polarity = string["extra"]["polarity"]
            assert polarity["rule"] == "ordered-final-grid-first-negative-last-positive"
            assert polarity["negative_panel_ref"] == string["ordered_panel_refs"][0]
            assert polarity["positive_panel_ref"] == string["ordered_panel_refs"][-1]
    membership_views(graph)


def check_schedule(graph):
    assert len(graph["routes"]) == 132
    valid(graph["routes"])
    panels = {p["id"]: p for p in graph["panels"]}
    inverters = {i["id"]: i for i in graph["inverters"]}
    expected_rows = {}
    for string in graph["strings"]:
        leads = [r for r in graph["routes"] if r["from_ref"] == string["id"]]
        assert len(leads) == 2
        by_kind = {r["route_kind"]: r for r in leads}
        assert set(by_kind) == {"start homerun", "end homerun"}
        lengths = []
        for kind, ref in zip(("start homerun", "end homerun"),
                             (string["ordered_panel_refs"][0], string["ordered_panel_refs"][-1])):
            route = by_kind[kind]
            centre = panels[ref]["centre"]
            assert route["points"] == [centre + [0.0], inverters[string["inverter_ref"]]["position"]]
            assert route["to_ref"] == string["inverter_ref"]
            assert route["extra"]["terminal_panel_ref"] == ref
            assert route["wire_gauge"] == string["wire_gauge"] == "10 AWG"
            assert (route["point_units"], route["length_units"]) == ("m", "ft")
            assert route["length_ft"] == pytest.approx(math.dist(*route["points"]) / 0.3048)
            lengths.append(route["length_ft"])
        expected_rows[string["circuit_tag"]] = [len(string["ordered_panel_refs"]), "10 AWG",
                                                  *lengths, sum(lengths)]
    assert len(graph["schedules"]) == 1
    valid(graph["schedules"])
    schedule = graph["schedules"][0]
    assert schedule["insertion_point"] == [10, 20, 0]
    assert schedule["headers"] == ["Circuit", "Modules", "Conductor", "Start homerun",
                                    "End homerun", "Total wire"]
    assert schedule["column_units"] == [None, "count", None, "ft", "ft", "ft"]
    assert schedule["source_rev"] == 15
    assert len(schedule["rows"]) == len(expected_rows) == 66
    assert {row[0] for row in schedule["rows"]} == set(expected_rows)
    for row in schedule["rows"]:
        expected = expected_rows[row[0]]
        assert type(row[1]) is int and row[1:3] == expected[:2]
        assert all(type(v) in (int, float) for v in row[3:])
        assert row[3:] == pytest.approx(expected[2:])
    assert set(schedule["source_refs"]) == {
        e["id"] for key in ("strings", "inverters", "panels", "routes") for e in graph[key]}
    assert schedule["extra"]["column_types"] == ["string", "integer", "string", "number", "number", "number"]
    assert schedule["extra"]["row_source_refs"] == [s["id"] for s in graph["strings"]]


class Rooftop:
    def __init__(self, client, backend, tools):
        self.client, self.backend, self.tools = client, backend, tools
        self.drawing = None
        self.headers = dict(HEADERS)
        self.executed = []
        self.timings = []

    def read(self, head, rev, count, versions):
        response = self.client.get(f"/api/drawings/{self.drawing}/versions", headers=self.headers)
        assert response.status_code == 200, response.text
        entries = response.json()["versions"]
        assert len(entries) == versions
        assert store.load_manifest(self.backend, TENANT, self.drawing)["head"] == head
        response = self.client.get(f"/api/drawings/{self.drawing}/intake?version={head}", headers=self.headers)
        assert response.status_code == 200, response.text
        intake = response.json()["intake"]
        active = self.client.get(f"/api/drawings/{self.drawing}/intake", headers=self.headers)
        assert active.status_code == 200, active.text
        assert active.json()["intake"] == intake
        version, key, entry = store.resolve_version_entry(self.backend, TENANT, self.drawing, head)
        raw = self.backend.get(key)
        assert version == head
        assert hashlib.sha256(raw).hexdigest() == entry["sha256"]
        assert next(e for e in entries if e["v"] == head)["sha256"] == entry["sha256"]
        assert json.loads(raw) == intake
        graph = intake.get("solar_design_graph")
        if rev is None:
            assert graph is None and "solar_design_graph_sha256" not in intake
        else:
            assert graph["rev"] == rev
            assert intake["solar_design_graph_sha256"] == digest(graph)
        rows = [dict(row) for row in jobs._query("SELECT job_id, tool, status FROM jobs")]
        assert len(rows) == count
        assert all(row["status"] == "complete" for row in rows)
        return copy.deepcopy({"head": head, "rev": rev, "intake": intake, "graph": graph,
                              "bytes": raw, "sha256": entry["sha256"], "intake_digest": digest(intake),
                              "versions": entries, "jobs": rows})

    def submit(self, tool, params, head, **top):
        return self.client.post("/api/run?wait=1", headers=self.headers, json={
            "tool": tool, "dwg": self.drawing, "dwg_version": head,
            "catalog_digest": deps.catalog_tool_digest(self.tools[tool]),
            "params": copy.deepcopy(params), **top})

    def step(self, tool, params, head, rev, count, *, proposal=False, **top):
        started = time.monotonic()
        response = self.submit(tool, params, head, **top)
        assert response.status_code == 200, (tool, head, response.text)
        envelope = response.json()
        assert envelope["ok"] is True, (tool, envelope)
        result = envelope["result"]
        if proposal:
            assert result["schema"] == "leaf.solar-bound-proposal.v1"
            job_id = result["proposal"]["job_id"]
            assert result["scope"] == {"drawing_id": self.drawing, "source_version": head}
            after = self.read(head, rev, count, head)
        else:
            assert result["schema_version"] == (
                "leaf.solar-graph-seed.v1" if head == 1 else "leaf.solar-graph-commit.v1")
            assert result["after_rev"] == rev + 1
            assert result["new_version"] == {"drawing_id": self.drawing, "version": head + 1, "parent": head}
            job_id = result["job_id"]
            after = self.read(head + 1, rev + 1, count, head + 1)
        job = jobs.get_job(job_id)
        assert job["status"] == "complete" and job["dwg_version"] == head
        self.executed.append(tool)
        self.timings.append((tool, time.monotonic() - started))
        return after, job_id


@pytest.fixture(scope="module")
def rooftop(tmp_path_factory):
    started = time.monotonic()
    root = tmp_path_factory.mktemp("rooftop-acceptance")
    # Enter the original fixture generators so their cleanup also runs on a
    # failed intermediate producer. No function-scoped fixture is requested.
    with ExitStack() as stack:
        monkeypatch = pytest.MonkeyPatch()
        stack.callback(monkeypatch.undo)
        stack.enter_context(contextmanager(isolated_jobs.__wrapped__)(root, monkeypatch))
        no_network.__wrapped__(monkeypatch)
        client, backend, tools = stack.enter_context(contextmanager(product.__wrapped__)(
            None, None, root, monkeypatch))
        for record in solar_tools.registry_records():
            if record["name"] in ("solar-panels-from-drawing", "solar-string-conductors"):
                tools[record["name"]] = record
        tools["solar-solve-proposal"] = next(t for t in json.loads(
            (SERVER / "catalog_tools.json").read_bytes())["tools"] if t["name"] == "solar-solve-proposal")
        executor = next(iter(jobs._executors.values()))
        for tool in tools.values():
            monkeypatch.setitem(jobs._executors, jobs.lane_for(tool, False), executor)
        from routers import jobs as route
        live = {"on": False}
        monkeypatch.setattr(route.deps, "auth_live", lambda: live["on"])
        monkeypatch.setattr(route, "_active_binding_tenant", lambda *a: None)
        monkeypatch.setattr(route.catalog, "live_aps_runtime_authorized", lambda *a, **k: False)

        raw = (ROOT / "data/rooftop_unsplit.intake.json").read_bytes()
        assert hashlib.sha256(raw).hexdigest() == INTAKE_SHA256
        recorded = json.loads(raw)
        recordings = json.loads((SERVER / "tests/fixtures/w1_rooftop_unsplit_solve.json").read_bytes())["groups"]
        sizing_record = json.loads((SERVER / "tests/fixtures/w1_rooftop_unsplit_sizing_response.json").read_bytes())
        assert sizing_record["synthetic"] is True
        sizing_calls, solve_calls, used = [], [], set()

        def grant(reference, tenant):
            assert (reference, tenant) == ("fixture-grant", TENANT)
            return CloudGrant(tenant, "")

        def size(request, resolved):
            assert request.wire() == sizing_record["request"]
            sizing_calls.append(request.wire())
            return cloud.canonical_bytes(sizing_record["response"])

        monkeypatch.setattr(sizing, "resolve_grant", grant)
        monkeypatch.setattr(sizing, "post_string_length", size)
        monkeypatch.setattr(cloud, "resolve_grant", grant)
        walk = Rooftop(client, backend, tools)
        response = client.post("/api/drawings/upload", files={
            "file": ("rooftop-unsplit.dxf", io.BytesIO(intake_to_dxf(recorded)))}, headers=HEADERS)
        assert response.status_code == 202, response.text
        receipt = response.json()
        assert receipt["tenant_kind"] == "account" and receipt["tenant_id"] == TENANT
        walk.drawing = receipt["drawing_id"]
        status = client.get(f"/api/drawings/{walk.drawing}/upload-status", headers=HEADERS)
        assert status.status_code == 200 and status.json()["status"] == "ready"
        uploaded = walk.read(1, None, 0, 1)
        equivalent_panels(recorded, uploaded["intake"])
        walk.headers["X-Checkout-Capability"] = checkout(client, walk.drawing)
        live["on"] = True

        def release():
            response = client.delete(f"/api/drawings/{walk.drawing}/checkout", headers=walk.headers)
            assert response.status_code == 200, response.text

        stack.callback(release)
        state, _ = walk.step("solar-settings", {"expected_rev": 0, "changes": {"panels_in_sequence": 14},
            "initialize": {"schema_version": 1, "source_intake_sha256": uploaded["sha256"], "units": UNITS}}, 1, 0, 1)
        graph = state["graph"]
        assert graph["source_hash"] == uploaded["sha256"]
        assert all(graph["project"]["units"][k] == v for k, v in UNITS.items())
        assert graph["settings"]["panels_in_sequence"] == 14 and not graph["panels"]
        assert graph["project"]["validity"]["state"] == "unknown"
        project = {"name": "Rooftop unsplit", "zip_code": "44224", "latitude": None, "longitude": None}
        state, _ = walk.step("solar-settings", {"expected_rev": 1, "project_changes": project}, 2, 1, 2)
        assert all(state["graph"]["project"][k] == v for k, v in project.items())
        valid([state["graph"]["project"], state["graph"]["settings"]])
        state, _ = walk.step("solar-panels-from-drawing", {"expected_rev": 2}, 3, 2, 3)
        graph = state["graph"]
        valid(graph["panels"])
        assert not graph["frames"] and not graph["strings"]
        by_handle = {normalized_handle(p["provenance"]["source_handle"]): p for p in graph["panels"]}
        outlines = rectangles(uploaded["intake"])
        assert len(graph["panels"]) == len(by_handle) == 882 and by_handle.keys() == outlines.keys()
        for handle, panel in by_handle.items():
            pts = outlines[handle]["pts"]
            assert panel["centre"] == [sum(p[i] for p in pts) / 4 for i in (0, 1)]
            assert panel["angle"] == math.degrees(math.atan2(pts[1][1] - pts[0][1], pts[1][0] - pts[0][0]))
        responses = replay_responses({"responses": [g["response"] for g in recordings]}, by_handle)

        def solve(request, resolved):
            solve_calls.append(request.wire_payload())
            return matching_response(request, responses, used)

        monkeypatch.setattr(cloud, "post_stringer", solve)
        settings_id = graph["settings"]["id"]
        state, _ = walk.step("solar-size-strings", {"expected_rev": 3, "mode": "global",
            "requests": {settings_id: sizing_record["request"]}, "grant_ref": "fixture-grant", "confirm": True}, 4, 3, 4)
        graph = state["graph"]
        evidence = sizing.require_sizing(graph)
        record = evidence["records"][settings_id]
        assert evidence["mode"] == "global" and graph["settings"]["global_string_sizing_confirmed"] is True
        assert record["adapter_version"] == sizing.ADAPTER_VERSION
        assert record["request"] == sizing_record["request"] and record["request"]["zip_code"] == "44224"
        assert record["response"] == sizing_record["response"] and record["response"]["pmp"] == 595.0
        assert record["sizing"]["panels_in_sequence"] == graph["settings"]["panels_in_sequence"] == 14
        assert record["sizing"]["voc_cold"]["passes"] is True
        assert sizing_calls == [sizing_record["request"]]
        groups = []
        assert [g["piece"] for g in recordings] == [12, 13, 16, 19, 20, 21, 22]
        for number, recording in enumerate(recordings, 1):
            pts = outlines[normalized_handle(recording["panels"][0]["handle"])]["pts"]
            width, height = math.dist(pts[0][:2], pts[1][:2]), math.dist(pts[1][:2], pts[2][:2])
            assert all(math.isfinite(v) and v > 0 for v in (width, height))
            groups.append({"name": f"Group {number}", "panel_refs": [by_handle[normalized_handle(p["handle"])]["id"]
                for p in recording["panels"]], "alignment_tolerance": 0.5,
                "module_width_along_row": width, "module_height_across_row": height})
        state, _ = walk.step("solar-panel-groups", {"expected_rev": 4, "groups": groups}, 5, 4, 5)
        graph = state["graph"]
        assert len(graph["frames"]) == 7
        valid(graph["frames"])
        memberships = [r for f in graph["frames"] for r in f["panel_refs"]]
        assert len(memberships) == len(set(memberships)) == 882
        assert set(memberships) == {p["id"] for p in graph["panels"]}
        handles = {p["id"]: normalized_handle(p["provenance"]["source_handle"]) for p in graph["panels"]}
        for index, group in enumerate(groups):
            frame = next(f for f in graph["frames"] if f["name"] == group["name"])
            assert set(frame["panel_refs"]) == set(group["panel_refs"])
            assert len(frame["panel_refs"]) == PANEL_COUNTS[index]
            assert (frame["module_rows"], frame["module_columns"]) == MATRIX_SHAPES[index]
            assert frame["module_power_watts"] == 595.0
            assert [[handles.get(c["panel_ref"]) for c in row] for row in frame["matrix"]] == [
                [normalized_handle(h) if h is not None else None for h in row] for row in recordings[index]["matrix"]]
        for index in range(7):
            graph = state["graph"]
            frame = next(f for f in graph["frames"] if f["name"] == f"Group {index + 1}")
            request = build_stringer_request(graph, frame["id"], max_string_length=14, dwgname="rooftop_demo.dwg")
            proposal, job_id = walk.step("solar-solve-proposal", {"grant_ref": "fixture-grant", "request": request},
                index + 6, index + 5, 6 + index * 2, proposal=True,
                solve_context={"frame_ref": frame["id"], "expected_rev": index + 5, "phase": "initial"})
            for key in ("graph", "bytes", "sha256", "versions", "intake_digest"):
                assert proposal[key] == state[key]
            state, _ = walk.step("solar-commit-solve", {"expected_rev": index + 5},
                index + 6, index + 5, 7 + index * 2, proposal_job_id=job_id)
            recorded_strings(state["graph"], recordings[:index + 1])
        graph = state["graph"]
        assert len(solve_calls) == len(used) == 7 and used == set(range(7))
        assert len(graph["strings"]) == 66
        assert graph["extra"]["solve_coverage"] == {"duplicate_panel_refs": [], "unassigned_panel_refs": []}
        state, _ = walk.step("solar-string-conductors", {"operation": "set-conductors", "expected_rev": 12,
            "assignments": [{"string_ref": s["id"], "wire_gauge": "10 AWG"} for s in graph["strings"]]}, 13, 12, 20)
        graph = state["graph"]
        valid(graph["strings"])
        assert all(s["wire_gauge"] == "10 AWG" for s in graph["strings"])
        recorded_strings(graph, recordings)
        equipment, assignments, powers = [], [], []
        for number in range(1, 8):
            frame = next(f for f in graph["frames"] if f["name"] == f"Group {number}")
            strings = frame_strings(graph, frame)
            n = len(strings)
            assert n == STRING_COUNTS[number - 1]
            inverter_id = f"leaf:inverter:00000000-0000-4000-8000-{number:012d}"
            equipment.append({"id": inverter_id, "number": number, "type_key": "fixture",
                "model": "Synthetic rooftop inverter", "position": [0, 0, 0], "rotation": 0,
                "scale": [1, 1, 1], "block_name": "INV", "layer": "Inverters", "mppt_count": 1,
                "total_dc_inputs": n, "mppt_inputs": {"A": n}, "max_dc_voltage": 1500,
                "max_ac_power_kw": 150, "max_dc_power_kw": 150, "is_solaredge": False})
            assignments.extend({"string_ref": s["id"], "inverter_ref": inverter_id, "mppt_letter": "A",
                                "input_number": input_number} for input_number, s in enumerate(strings))
            powers.append(len(frame["panel_refs"]) * frame["module_power_watts"] / 1000)
        assert powers == pytest.approx([66.045, 61.880, 58.905, 81.515, 79.730, 73.185, 103.530])
        assert sum(powers) == pytest.approx(524.790) and max(powers) < 150
        state, _ = walk.step("solar-assign-equipment", {"expected_rev": 13, "equipment": equipment,
                                                       "assignments": assignments}, 14, 13, 21)
        graph = state["graph"]
        assert len(graph["inverters"]) == 7 and equipment_ready(graph)
        valid(graph["inverters"])
        actual = [{**a, "inverter_ref": i["id"]} for i in graph["inverters"] for a in i["input_assignments"]]
        assert len(actual) == len({a["string_ref"] for a in actual}) == 66
        assert len({(a["inverter_ref"], a["mppt_letter"], a["input_number"]) for a in actual}) == 66
        assert sum(i["total_dc_inputs"] for i in graph["inverters"]) == 66
        for expected in assignments:
            assert any(all(a[k] == v for k, v in expected.items()) for a in actual)
            string = next(s for s in graph["strings"] if s["id"] == expected["string_ref"])
            assert string["inverter_ref"] == expected["inverter_ref"]
        response = sizing.require_sizing(graph)["records"][settings_id]["response"]
        cold = response["voc"] * (1 + response["bvoc"] / 100 * (response["min_temp"] - 25))
        assert cold == graph["settings"]["voc_cold"]["per_module"]
        assert all(len(s["ordered_panel_refs"]) <= 14 and len(s["ordered_panel_refs"]) * cold < 1500
                   for s in graph["strings"])
        state, _ = walk.step("solar-homeruns", {"expected_rev": 14}, 15, 14, 22)
        assert len(state["graph"]["routes"]) == 132
        valid(state["graph"]["routes"])
        scheduled, _ = walk.step("solar-schedule", {"expected_rev": 15, "insertion_point": [10, 20]}, 16, 15, 23)
        check_schedule(scheduled["graph"])
        recorded_strings(scheduled["graph"], recordings)
        string = next(s for s in scheduled["graph"]["strings"] if s["circuit_tag"] == "S1")
        stale = {string["id"], string["inverter_ref"]}
        stale.update(r["id"] for r in scheduled["graph"]["routes"] if r["to_ref"] == string["inverter_ref"])
        stale.update(s["id"] for s in scheduled["graph"]["schedules"] if stale.intersection(s["source_refs"]))
        assert len(stale) == 19
        corrected, _ = walk.step("solar-correct-string", {"expected_rev": 16, "memberships": [
            {"string_ref": string["id"], "ordered_panel_refs": list(reversed(string["ordered_panel_refs"]))}]}, 17, 16, 24)
        correction_state = {"corrected": corrected, "scheduled": scheduled, "s1": string, "stale": stale}
        check_correction(correction_state)
        readiness = {}
        tenant = deps.TenantContext(TENANT, tier="demo", subject="fixture-subject", authority_resolved=True)
        for tool, reason in (("solar-assign-equipment", "valid_strings_required"),
                             ("solar-homeruns", "equipment_assignment_required"),
                             ("solar-schedule", "complete_routing_required")):
            readiness[tool] = entitlements.w1_tool_availability(tools[tool], tenant, walk.drawing)
            assert readiness[tool]["input_ready"] is False and readiness[tool]["input_reason"] == reason
        refusal = walk.submit("solar-settings", {"expected_rev": 16, "changes": {"num_mppt": 2}}, 17)
        assert refusal.status_code == 409 and refusal.json()["reason_code"] == "not_current_head"
        refused = walk.read(18, 17, 24, 18)
        assert refused == corrected
        restored = []
        for operation, saved in (("undo", scheduled), ("redo", corrected)):
            response = client.post(f"/api/drawings/{walk.drawing}/{operation}", headers=walk.headers)
            assert response.status_code == 200, response.text
            snapshot = walk.read(saved["head"], saved["rev"], 24, 18)
            for key in ("graph", "bytes", "sha256", "intake_digest", "intake"):
                assert snapshot[key] == saved[key]
            assert snapshot["jobs"] == corrected["jobs"] and snapshot["versions"] == corrected["versions"]
            restored.append(snapshot)
        elapsed = time.monotonic() - started
        print(f"rooftop scenario elapsed: {elapsed:.3f}s; stages: {walk.timings!r}")
        assert elapsed <= 900, ("rooftop exceeded 900 seconds", elapsed, walk.timings)
        yield {"uploaded": uploaded, "recorded": recorded, "scheduled": scheduled, "corrected": corrected,
               "stale": stale, "s1": string, "readiness": readiness, "refused": refused,
               "refusal_status": refusal.status_code, "refusal_body": refusal.json(),
               "restored": restored, "executed": tuple(walk.executed), "elapsed": elapsed,
               "sizing_calls": sizing_calls, "solve_calls": solve_calls, "used": used}


def test_rooftop_R1_fixture_identity(rooftop):
    snapshot = rooftop["uploaded"]
    assert snapshot["head"] == 1 and snapshot["graph"] is None and snapshot["jobs"] == []
    equivalent_panels(rooftop["recorded"], snapshot["intake"])


def test_rooftop_R2_product_chain(rooftop):
    snapshot = rooftop["scheduled"]
    assert (snapshot["head"], snapshot["rev"], len(snapshot["jobs"]), len(snapshot["versions"])) == (17, 16, 23, 17)
    graph = snapshot["graph"]
    assert [len(graph[k]) for k in ("panels", "frames", "strings", "inverters", "routes", "schedules")] == [882, 7, 66, 7, 132, 1]
    for key in ("panels", "frames", "strings", "inverters", "routes", "schedules"):
        valid(graph[key])
    assert len(rooftop["sizing_calls"]) == 1 and len(rooftop["solve_calls"]) == 7
    assert rooftop["used"] == set(range(7))
    assert membership_views(graph) == {p["id"] for p in graph["panels"]}
    check_schedule(graph)


def check_correction(rooftop):
    snapshot = rooftop["corrected"]
    assert (snapshot["head"], snapshot["rev"], len(snapshot["jobs"]), len(snapshot["versions"])) == (18, 17, 24, 18)
    graph, before = snapshot["graph"], rooftop["scheduled"]["graph"]
    s1 = rooftop["s1"]
    changed = next(s for s in graph["strings"] if s["id"] == s1["id"])
    assert changed["ordered_panel_refs"] == list(reversed(s1["ordered_panel_refs"]))
    assert changed["module_count"] == s1["module_count"]
    assert membership_views(graph) == {p["id"] for p in before["panels"]}
    assert graph["extra"]["solve_coverage"] == {"duplicate_panel_refs": [], "unassigned_panel_refs": []}
    for key, stale_count, valid_count in (("strings", 1, 65), ("inverters", 1, 6), ("routes", 16, 116), ("schedules", 1, 0)):
        expected = rooftop["stale"] & {e["id"] for e in before[key]}
        assert len(expected) == stale_count
        assert {e["id"] for e in graph[key] if e["validity"]["state"] == "stale"} == expected
        previous = {e["id"]: e for e in before[key]}
        assert len(graph[key]) - len(expected) == valid_count
        for entity in graph[key]:
            assert entity["validity"] == ({"state": "stale", "reasons": ["upstream_corrected"]}
                if entity["id"] in expected else previous[entity["id"]]["validity"])


def test_rooftop_R3_correction(rooftop):
    check_correction(rooftop)
    for tool, reason in (("solar-assign-equipment", "valid_strings_required"),
                         ("solar-homeruns", "equipment_assignment_required"),
                         ("solar-schedule", "complete_routing_required")):
        assert rooftop["readiness"][tool]["input_ready"] is False
        assert rooftop["readiness"][tool]["input_reason"] == reason


def test_rooftop_R4_stale_submission(rooftop):
    assert rooftop["refusal_status"] == 409
    assert rooftop["refusal_body"]["reason_code"] == "not_current_head"
    assert rooftop["refused"] == rooftop["corrected"]


def test_rooftop_R5_undo_redo(rooftop):
    for restored, name in zip(rooftop["restored"], ("scheduled", "corrected")):
        saved = rooftop[name]
        for key in ("head", "rev", "graph", "bytes", "sha256", "intake_digest", "intake"):
            assert restored[key] == saved[key]
        assert len(restored["jobs"]) == 24 and len(restored["versions"]) == 18
        assert restored["intake"]["solar_design_graph_sha256"] == digest(restored["graph"])


def test_rooftop_R6_w1_coverage(rooftop):
    ledger = json.loads((ROOT / "docs/parity/solar-ledger.json").read_bytes())
    rows = [r for r in ledger["rows"] if r["maturity"] == "production" and r["class"] in ("T", "F")
            and r["wave"] is not None and r["wave"] <= 1]
    capabilities = {r["capability"] for r in rows}
    exemptions = dict(OUT_OF_CHAIN)
    assert len(rows) == 15 and len(capabilities) == 12
    assert len(CHAIN) == len(set(CHAIN)) == 5
    assert len(exemptions) == len(OUT_OF_CHAIN) == 7 and all(reason.strip() for reason in exemptions.values())
    assert set(CHAIN).isdisjoint(exemptions)
    assert set(CHAIN) | set(exemptions) == capabilities
    assert {r["global"] for r in rows if r["capability"] in exemptions} == {
        "CableExport", "LEAFADDZONE", "LEAFZONEASSIGNPANELS", "PanelGroupCreateZoneAware", "LEAFUNITSYNC",
        "Branch", "LEAFFLOWROOFTOP", "LEAFPALETTE", "LEAFPLATFORM"}
    executed = set(rooftop["executed"])
    witnesses = {"inverter-add": {"solar-assign-equipment"}, "homeruns": {"solar-homeruns"},
                 "panel-group-create": {"solar-panel-groups"}, "solve": {"solar-solve-proposal", "solar-commit-solve"},
                 "string-sizer": {"solar-size-strings"}}
    assert set(witnesses) == set(CHAIN)
    assert all(tools <= executed for tools in witnesses.values())
