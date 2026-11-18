"""X04 product-route grading with scripted I/O only."""

from contextlib import contextmanager
from copy import deepcopy
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from benches import x04
from xbench_studio import StudioClient, StudioError


SHA = "a" * 64
SOURCE = "b" * 40
PROJECT = {"project_id": "p", "org_id": "o"}
CAP = "private-capability-must-never-be-written"


class FakeClock:
    def __init__(self):
        self.now = 0

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class FakeClient:
    def __init__(self):
        self.upload_status = 202
        self.upload = {"status": "ready"}
        self.provenance = {"original": {"sha256": SHA}}
        self.intake = {"layers": ["Modules"], "polylines": [],
                       "inserts": [{"name": "MODULE", "layer": "Modules", "pt": [0, 0, 0]}],
                       "blocks": {"MODULE": {"base": [0, 0, 0], "count": 1, "complete": True,
                                             "children": [{"kind": "LINE", "layer": "0",
                                                           "pts": [[0, 0, 0], [1, 1, 0]]}]}}}
        self.counts = {"Modules": 12}
        self.catalog = {"tools": []}
        self.module_responses = []
        self.calls = []
        self.deleted = []

    def transport(self, method, path, **kw):
        if (method, path) == ("GET", "/api/tools"):
            self.calls.append((method, path, kw))
            return 200, self.catalog
        if (method, path) == ("POST", "/api/run?wait=1"):
            self.calls.append((method, path, kw))
            return self.module_responses.pop(0)
        assert (method, path) == ("POST", "/api/drawings/upload")
        self.calls.append((method, path, kw))
        return self.upload_status, {"drawing_id": "uploaded", "checkout_capability": CAP}

    def wait_upload(self, drawing_id, *, timeout_s, poll_s):
        assert (drawing_id, timeout_s, poll_s) == ("uploaded", 300, 3)
        if isinstance(self.upload, Exception):
            raise self.upload
        return self.upload

    def adopt_drawing(self, project_id, drawing_id, *, name):
        assert (project_id, drawing_id) == ("p", "uploaded")
        return {"drawing_id": "d", "version_id": "v", "provenance": self.provenance,
                "intake_ref": {}, "oss_object": {}, "checkout_capability": CAP}

    def drawing_context(self, project_id, version_id, drawing_id):
        assert (project_id, version_id, drawing_id) == ("p", "v", "d")
        return {"context": {"intake_sha256": "c" * 64}, "intake": self.intake}

    def run(self, tool, **kw):
        assert tool == "count-by-layer"
        assert (kw["org_id"], kw["project_id"], kw["version_id"]) == ("o", "p", "v")
        assert kw["params"]["drawing_id"] == "d"
        self.calls.append((tool, kw))
        return 200, {"result": {"counts": self.counts}}

    def create_project(self, name):
        assert name.startswith("XBench X04 " + SOURCE[:8])
        return PROJECT

    def delete_project(self, project_id):
        self.deleted.append(project_id)


@pytest.fixture
def fixture(tmp_path):
    path = tmp_path / "raw.dwg"
    path.write_bytes(b"raw\x00drawing\xff")
    return {"id": "customer-01", "path": str(path), "bytes": 12, "sha256": SHA,
            "modules": 12, "module_layers": ["Modules"], "module_blocks": ["MODULE"],
            "module_entity": "INSERT", "design_is_whole_layer": True}


def grade(fixture, client=None, **kw):
    client = client or FakeClient()
    return x04.grade_files(client, client.transport, project=PROJECT, fixtures=[fixture], **kw)[fixture["id"]]


def test_multipart_preserves_raw_bytes():
    content = b"\x00\xff\r\nuntouched"
    body, content_type = x04.build_multipart("sample.dwg", content)
    boundary = content_type.split("boundary=", 1)[1].encode()
    assert body.startswith(b"--" + boundary + b"\r\n")
    assert b'name="file"; filename="sample.dwg"' in body
    assert body.split(b"\r\n\r\n", 1)[1] == content + b"\r\n--" + boundary + b"--\r\n"


def test_multipart_rejects_header_injection():
    with pytest.raises(ValueError):
        x04.build_multipart('bad"\r\nname', b"data")


def test_multipart_does_not_enforce_client_size_limit():
    content = b"x" * (25 * 1024 * 1024 + 1)
    body, _ = x04.build_multipart("large.dwg", content)
    assert len(body) > len(content)
    assert body.split(b"\r\n\r\n", 1)[1].startswith(content)


def test_413_fails_only_that_file(fixture):
    client = FakeClient()
    calls = []

    def transport(*args, **kw):
        calls.append(kw)
        return (413, {}) if len(calls) == 1 else (202, {"drawing_id": "uploaded"})

    other = {**fixture, "id": "customer-02"}
    rows = x04.grade_files(client, transport, project=PROJECT, fixtures=[fixture, other])
    assert list(rows) == [fixture["id"], other["id"]]
    assert rows[fixture["id"]]["steps"] == {"1": "FAIL", **{str(n): "UNREADABLE" for n in range(2, 7)}}
    assert rows[other["id"]]["steps"]["1"] == "PASS"
    assert b'filename="customer-02.dwg"' in calls[1]["data"]


@pytest.mark.parametrize("status,stop_on_5xx,stops", [
    (503, True, True), (503, False, False), (503, None, False), (413, True, False),
])
def test_stop_on_upload_http_error(fixture, status, stop_on_5xx, stops):
    client = FakeClient()
    fixtures = [{**fixture, "id": f"customer-{n:02}"} for n in range(1, 4)]
    uploads = []

    def transport(method, path, **kw):
        if path == "/api/drawings/upload":
            uploads.append(kw["data"])
            if len(uploads) == 2:
                return status, {}
        return client.transport(method, path, **kw)

    options = {} if stop_on_5xx is None else {"stop_on_5xx": stop_on_5xx}
    rows = x04.grade_files(client, transport, project=PROJECT, fixtures=fixtures, **options)
    assert list(rows) == [item["id"] for item in fixtures]
    assert rows["customer-01"]["steps"]["1"] == "PASS"
    assert rows["customer-02"]["steps"]["1"] == "FAIL"
    assert rows["customer-02"]["evidence"]["1"] == f"upload HTTP {status}"
    assert len(uploads) == (2 if stops else 3)
    if stops:
        assert rows["customer-03"]["steps"] == {str(n): "UNREADABLE" for n in range(1, 7)}
        assert rows["customer-03"]["evidence"] == {
            str(n): "not attempted: pass stopped after HTTP 503 on customer-02"
            for n in range(1, 7)}
        assert rows["customer-03"]["timings"] == rows["customer-03"]["statuses"] == {}
    else:
        assert rows["customer-03"]["steps"]["1"] == "PASS"
        assert b'filename="customer-03.dwg"' in uploads[2]


@pytest.mark.parametrize("method,status", [
    ("wait_upload", 502), ("adopt_drawing", 503), ("drawing_context", 504),
    ("run", 500), ("wait_upload", 422),
])
def test_stop_on_client_error(fixture, monkeypatch, method, status):
    client = FakeClient()
    fixtures = [{**fixture, "id": f"customer-{n:02}"} for n in range(1, 4)]
    uploads = []
    original = getattr(client, method)

    def fail_second(*args, **kw):
        if len(uploads) == 2:
            raise StudioError(status, "UNAVAILABLE", "server unavailable")
        return original(*args, **kw)

    def transport(method, path, **kw):
        if path == "/api/drawings/upload":
            uploads.append(kw["data"])
        return client.transport(method, path, **kw)

    monkeypatch.setattr(client, method, fail_second)
    rows = x04.grade_files(client, transport, project=PROJECT, fixtures=fixtures,
                           stop_on_5xx=True)
    assert CAP not in json.dumps(rows)
    failed = rows["customer-02"]
    assert failed["steps"]["1"] == ("FAIL" if method == "wait_upload" else "PASS")
    if method == "run":
        assert failed["steps"]["4"] == "PASS"
        assert failed["steps"]["5"] == "UNREADABLE"
    if status >= 500:
        assert len(uploads) == 2
        assert rows["customer-03"]["steps"] == {str(n): "UNREADABLE" for n in range(1, 7)}
        assert rows["customer-03"]["evidence"] == {
            str(n): f"not attempted: pass stopped after HTTP {status} on customer-02"
            for n in range(1, 7)}
    else:
        assert len(uploads) == 3
        assert rows["customer-03"]["steps"]["1"] == "PASS"


def test_timeout_uses_real_poller_with_fake_clock(fixture):
    timer = FakeClock()

    def transport(method, path, **kw):
        return (202, {"drawing_id": "uploaded"}) if method == "POST" else (200, {"status": "processing"})

    client = StudioClient(transport, clock=timer.clock, sleep=timer.sleep)
    row = x04.grade_files(client, transport, project=PROJECT, fixtures=[fixture], clock=timer.clock)[fixture["id"]]
    assert row["steps"]["1"] == "FAIL"
    assert row["timings"]["upload_s"] == 300
    assert all(row["steps"][str(n)] == "UNREADABLE" for n in range(2, 7))


def test_upload_failed_is_failure(fixture):
    client = FakeClient()
    client.upload = StudioError(422, "UPLOAD_FAILED", CAP)
    row = grade(fixture, client)
    assert row["steps"]["1"] == "FAIL"
    assert CAP not in json.dumps(row)


@pytest.mark.parametrize("method,error", [
    ("adopt_drawing", TimeoutError("token=" + "secret123" * 5 + " " + "unavailable " * 40)),
    ("drawing_context", StudioError(503, "SIP_R1_INTAKE_UNAVAILABLE", "intake unavailable")),
    ("drawing_context", StudioError(503, "SERVICE_UNAVAILABLE", "intake unavailable",
                                    reason_code="SIP_R1_INTAKE_UNAVAILABLE")),
])
def test_adoption_or_intake_failure_stays_unreadable(fixture, monkeypatch, method, error):
    client = FakeClient()
    client.upload["dependencies"] = []
    fixture["deps"] = []
    original = getattr(client, method)
    calls = []

    def fail_once(*args, **kw):
        calls.append((args, kw))
        if len(calls) == 1:
            raise error
        return original(*args, **kw)

    monkeypatch.setattr(client, method, fail_once)
    other = {**fixture, "id": "customer-02"}
    rows = x04.grade_files(client, client.transport, project=PROJECT, fixtures=[fixture, other])
    row = rows[fixture["id"]]
    assert row["steps"] == {"1": "PASS", **{str(n): "UNREADABLE" for n in range(2, 7)}}
    prefix = f"adoption or intake unavailable: {type(error).__name__}: "
    evidence = row["evidence"]["2"]
    assert evidence.startswith(prefix)
    assert len(evidence[len(prefix):]) <= 300
    assert all(row["evidence"][str(n)] == evidence for n in range(2, 7))
    if isinstance(error, StudioError):
        assert "SIP_R1_INTAKE_UNAVAILABLE" in evidence
    else:
        assert "secret123" not in evidence
        assert "[redacted]" in evidence
        assert len(evidence[len(prefix):]) == 300
    assert row["counts"] == []
    assert rows[other["id"]]["steps"] == {str(n): "PASS" for n in range(1, 7)}


def test_late_ready_fails(fixture):
    timer = FakeClock()
    client = FakeClient()
    original = client.wait_upload

    def wait(*args, **kw):
        timer.sleep(301)
        return original(*args, **kw)

    client.wait_upload = wait
    assert grade(fixture, client, clock=timer.clock)["steps"]["1"] == "FAIL"


def test_dependency_report_absent(fixture):
    row = grade(fixture)
    assert row["steps"]["2"] == "FAIL"
    assert row["evidence"]["2"] == "no dependency report exposed"
    assert row["dependency_report"] is None
    assert row["missing_deps"] == []


def test_dependency_report_precedence():
    assert x04.find_dependency_report({"xrefs": []}, {"fonts": [1]}) == {"xrefs": []}
    assert x04.find_dependency_report({}, {"fonts": []}) == {"fonts": []}
    assert x04.find_dependency_report({}, {}, {"images": []}) == {"images": []}
    assert x04.find_dependency_report({"layers": []}) is None


def test_dependency_report_without_inventory_unreadable(fixture):
    client = FakeClient()
    client.upload["dependencies"] = []
    assert grade(fixture, client)["steps"]["2"] == "UNREADABLE"


def test_dependency_inventory_needs_every_disposition(fixture):
    client = FakeClient()
    fixture["deps"] = [{"kind": "xref", "name": "reference"}, {"kind": "font", "name": "face"}]
    client.upload["dependencies"] = [{"kind": "xref", "name": "reference", "disposition": "bound"},
                                     {"kind": "font", "name": "face", "disposition": "font remapped"}]
    assert grade(fixture, client)["steps"]["2"] == "PASS"
    del client.upload["dependencies"][1]["disposition"]
    assert grade(fixture, client)["steps"]["2"] == "FAIL"


def test_dependency_evidence_names_only_missing_dependencies(fixture):
    client = FakeClient()
    fixture["deps"] = [{"kind": "xref", "name": "reference"},
                       {"kind": "font", "name": "face"},
                       {"kind": "image", "name": "site.png"}]
    client.upload["dependencies"] = [
        {"kind": "xref", "name": "reference", "disposition": "bound"},
        {"kind": "font", "name": "face"}]
    row = grade(fixture, client)
    assert row["steps"]["2"] == "FAIL"
    assert row["evidence"]["2"] == (
        "dependency missing or without disposition: 2 of 3 missing: font:face; image:site.png")
    assert row["missing_deps"] == fixture["deps"][1:]
    assert row["dependency_report"] == {"dependencies": client.upload["dependencies"]}


@pytest.mark.parametrize("source", ["upload", "provenance", "intake"])
def test_complete_dependencies_save_report_and_empty_missing_list(fixture, source):
    client = FakeClient()
    fixture["deps"] = [{"kind": "xref", "name": "reference"},
                       {"kind": "font", "name": "face"}]
    dependencies = [{**fixture["deps"][0], "disposition": "bound"},
                    {**fixture["deps"][1], "disposition": "font remapped"}]
    getattr(client, source)["dependencies"] = dependencies
    client.upload["checkout_capability"] = CAP
    client.intake["arbitrary_text"] = CAP
    row = grade(fixture, client)
    assert row["steps"]["2"] == "PASS"
    assert row["evidence"]["2"] == "dependency dispositions complete"
    assert row["missing_deps"] == []
    assert row["dependency_report"] == {"dependencies": dependencies}
    assert CAP not in json.dumps(row)


def test_missing_dependency_evidence_caps_entries_without_truncating_names(fixture):
    client = FakeClient()
    fixture["deps"] = [{"kind": "xref", "name": f"reference-{n:02}-" + "x" * 100}
                       for n in range(25)]
    client.upload["dependencies"] = []
    row = grade(fixture, client)
    assert row["steps"]["2"] == "FAIL"
    prefix = "dependency missing or without disposition: 25 of 25 missing: "
    assert row["evidence"]["2"].startswith(prefix)
    assert row["evidence"]["2"][len(prefix):].split("; ") == [
        f"xref:{dep['name']}" for dep in fixture["deps"][:20]]
    assert row["missing_deps"] == fixture["deps"]
    assert len(row["missing_deps"]) == 25


@pytest.mark.parametrize("dep,record,expected", [
    ({"kind": "underlay", "name": "u.pdf"},
     {"kind": "underlay", "name": "u.pdf", "disposition": "underlay omitted"}, True),
    ({"kind": "proxy", "name": "p"},
     {"kind": "proxy", "name": "p", "disposition": "proxy_omitted"}, True),
    ({"kind": "underlay", "name": "u.pdf"},
     {"kind": "image", "name": "u.pdf", "disposition": "underlay omitted"}, False),
    ({"kind": "underlay", "name": "u.pdf"},
     {"kind": "underlay", "name": "u.pdf", "disposition": "ignored"}, False),
])
def test_dependency_present_omission_dispositions(dep, record, expected):
    assert x04._dependency_present(record, dep) is expected


def test_sha_in_nested_provenance_and_absent(fixture):
    client = FakeClient()
    assert grade(fixture, client)["steps"]["3"] == "PASS"
    client.provenance = {}
    row = grade(fixture, client)
    assert row["steps"]["3"] == "FAIL"
    assert row["evidence"]["3"] == "original SHA-256 not exposed"
    client.upload["original_sha256"] = SHA
    assert grade(fixture, client)["steps"]["3"] == "PASS"


def test_sha_depth_and_exact_hex():
    assert x04.find_sha({"a": [{"b": SHA.upper()}]}, SHA)
    assert not x04.find_sha({"a": {"b": SHA}}, SHA, depth=1)
    assert not x04.find_sha("prefix" + SHA, SHA)


def test_module_insert_geometry(fixture):
    assert grade(fixture)["steps"]["4"] == "PASS"


@pytest.mark.parametrize("block_name", ["*U999", "CONVERTED_MODULE"])
def test_anonymous_module_matches_geometry_on_any_module_layer(fixture, block_name):
    client = FakeClient()
    fixture["module_blocks"] = ["*U72"]
    fixture["module_layers"] = ["Other", "Modules"]
    client.counts["Other"] = 0
    client.intake["blocks"][block_name] = client.intake["blocks"].pop("MODULE")
    client.intake["inserts"][0]["name"] = block_name
    row = grade(fixture, client)
    assert row["steps"]["4"] == "PASS"
    assert block_name in row["evidence"]["4"]
    assert "*U72" not in row["evidence"]["4"]


@pytest.mark.parametrize("invalid", ["layer", "no_layers", "insert", "definition", "empty", "text"])
def test_anonymous_module_requires_insert_layer_and_geometry(fixture, invalid):
    client = FakeClient()
    fixture["module_blocks"] = ["*U72"]
    if invalid == "layer":
        client.intake["inserts"][0]["layer"] = "Other"
    elif invalid == "no_layers":
        del fixture["module_layers"]
    elif invalid == "insert":
        client.intake["inserts"] = []
    elif invalid == "definition":
        client.intake["blocks"] = {}
    else:
        client.intake["blocks"]["MODULE"]["children"] = (
            [] if invalid == "empty" else [{"kind": "TEXT", "text": "module"}])
    assert grade(fixture, client)["steps"]["4"] == "FAIL"


def test_anonymous_module_evidence_names_at_most_five_blocks(fixture):
    client = FakeClient()
    fixture["module_blocks"] = ["*U72"]
    block = client.intake["blocks"].pop("MODULE")
    names = [f"*U{n}" for n in range(100, 107)]
    client.intake["blocks"] = {name: block for name in names}
    client.intake["inserts"] = [{"name": name, "layer": "Modules"} for name in names]
    row = grade(fixture, client)
    assert row["steps"]["4"] == "PASS"
    assert row["evidence"]["4"].endswith("matched blocks: " + ", ".join(names[:5]))
    assert all(name not in row["evidence"]["4"] for name in names[5:])


def test_named_module_still_requires_exact_name_with_anonymous_entry(fixture):
    fixture["module_blocks"] = ["*U72", "EXPECTED_MODULE"]
    row = grade(fixture)
    assert row["steps"]["4"] == "FAIL"
    assert "EXPECTED_MODULE" in row["evidence"]["4"]
    assert "*U72" not in row["evidence"]["4"]
    assert "matched blocks: MODULE" in row["evidence"]["4"]


def test_empty_block_definition(fixture):
    client = FakeClient()
    client.intake["blocks"]["MODULE"]["children"] = []
    row = grade(fixture, client)
    assert row["steps"]["4"] == "FAIL"
    assert "MODULE" in row["evidence"]["4"]


def test_missing_insert_and_definition(fixture):
    client = FakeClient()
    client.intake["inserts"] = []
    assert grade(fixture, client)["steps"]["4"] == "FAIL"
    client = FakeClient()
    client.intake["blocks"] = {}
    assert grade(fixture, client)["steps"]["4"] == "FAIL"


def test_text_is_not_geometry(fixture):
    client = FakeClient()
    client.intake["blocks"]["MODULE"]["children"] = [{"kind": "TEXT", "text": "private customer text"}]
    row = grade(fixture, client)
    assert row["steps"]["4"] == "FAIL"
    assert "private customer text" not in json.dumps(row)


def test_whole_layer_equal_and_unequal(fixture):
    client = FakeClient()
    assert grade(fixture, client)["steps"]["5"] == "PASS"
    client.counts["Modules"] = 11
    assert grade(fixture, client)["steps"]["5"] == "FAIL"


def test_multiple_layers_sum(fixture):
    client = FakeClient()
    fixture["module_layers"] = ["Modules", "Second"]
    client.counts = {"Modules": 7, "Second": 5}
    row = grade(fixture, client)
    assert row["counts"] == [7, 5]
    assert row["steps"]["5"] == "PASS"


def test_non_whole_layer_needs_ui_unless_count_equal(fixture):
    client = FakeClient()
    fixture["design_is_whole_layer"] = False
    client.counts["Modules"] = 24
    row = grade(fixture, client)
    assert row["steps"]["5"] == row["steps"]["6"] == "UNREADABLE"
    client.counts["Modules"] = 12
    assert grade(fixture, client)["steps"]["5"] == "PASS"


def test_without_layers_is_unreadable(fixture):
    del fixture["module_layers"]
    assert grade(fixture)["steps"]["5"] == "UNREADABLE"


# Wire shapes from the product's test_count_modules and
# test_x04_selection_envelope_consistency: listing and explicit selection are
# both count-modules results, not a separate module-set endpoint.
MODULE_TOOL = {"name": "count-modules", "version": "1.0.0", "catalog_digest": "d" * 64}
DESIGN_SET = {"set_id": '["INSERT","Modules","MODULE"]', "entity_type": "INSERT",
              "layer": "Modules", "block": "MODULE", "count": 12}
OTHER_SET = {"set_id": '["INSERT","Alternate","Panel B"]', "entity_type": "INSERT",
             "layer": "Alternate", "block": "Panel B", "count": 24}


def module_client(fixture, *, multi=False, count=12):
    client = FakeClient()
    client.catalog = {"tools": [deepcopy(MODULE_TOOL)]}
    fixture["module_set"] = {"set_id": DESIGN_SET["set_id"]}
    sets = [DESIGN_SET, OTHER_SET] if multi else [DESIGN_SET]
    listing = {"sets": sets, "selection_required": True, "selected_sets": [],
               "selected_set": None, "count": None, "original_bytes_integrity": "unverified"}
    selected = {**listing, "selection_required": False, "selected_sets": [DESIGN_SET],
                "selected_set": DESIGN_SET, "count": count}
    for result in (listing, selected):
        client.module_responses.append((200, deepcopy({
            "ok": True, "tool": "count-modules", "version": "1.0.0", "error": None,
            "result": result, "overlay": None})))
    return client


def module_calls(client):
    return [call[2] for call in client.calls if call[:2] == ("POST", "/api/run?wait=1")]


def test_set_evidence_normalizes_duplicate_handles_and_omits_geometry():
    intake = {"inserts": [{"name": "MODULE", "layer": "Modules", "handle": handle,
                           "pt": [1, 2, 3], "private": "omit"}
                          for handle in ("0x000a", "A", " 000A ")]}
    original = deepcopy(intake)
    assert x04.set_evidence(intake, DESIGN_SET["set_id"]) == {
        "rows_total": 3, "distinct_handles": 1, "duplicate_handle_rows": 2,
        "multiplicity": [[1, 1, 3]], "space_layout": [["model", "Model", 3]],
        "sample_handles": ["A"]}
    assert intake == original


def test_set_evidence_records_array_multiplicity():
    intake = {"inserts": [{"name": "MODULE", "layer": "Modules", "rows": 1, "columns": 3}]}
    evidence = x04.set_evidence(intake, DESIGN_SET["set_id"])
    assert evidence["rows_total"] == 1
    assert evidence["multiplicity"] == [[1, 3, 1]]
    assert evidence["distinct_handles"] == evidence["duplicate_handle_rows"] == 0


def test_set_evidence_splits_spaces_and_layouts_without_filtering_paper():
    intake = {"inserts": [
        {"name": "MODULE", "layer": "Modules"},
        {"name": "MODULE", "layer": "Modules", "space": "model", "layout": "Model"},
        {"name": "MODULE", "layer": "Modules", "space": "paper", "layout": "Sheet 1"},
        {"name": "OTHER", "layer": "Modules"},
        {"name": "MODULE", "layer": "Other"}]}
    evidence = x04.set_evidence(intake, DESIGN_SET["set_id"])
    assert evidence["rows_total"] == 3
    assert evidence["duplicate_handle_rows"] == 0
    assert evidence["space_layout"] == [["model", "Model", 2], ["paper", "Sheet 1", 1]]


@pytest.mark.parametrize("intake", [None, {"inserts": {}}, {"inserts": [None]},
                                     {"inserts": [{"handle": []}]}])
def test_set_evidence_malformed_intake_returns_error(intake):
    evidence = x04.set_evidence(intake, DESIGN_SET["set_id"])
    assert set(evidence) == {"error"}
    assert isinstance(evidence["error"], str) and evidence["error"]


def test_set_evidence_polylines_default_layer_and_cap_samples():
    intake = {"polylines": [{"handle": f"0x{n:04x}"} for n in range(12)]
              + [{"layer": "Other", "handle": "FF"}]}
    evidence = x04.set_evidence(intake, '["LWPOLYLINE","0",""]')
    assert evidence == {
        "rows_total": 12, "distinct_handles": 12, "duplicate_handle_rows": 0,
        "multiplicity": [], "space_layout": [["model", "Model", 12]],
        "sample_handles": [f"{n:X}" for n in range(10)]}


@pytest.mark.parametrize("oracle", [11, 12])
def test_selection_records_set_evidence_without_changing_verdict(fixture, monkeypatch, oracle):
    fixture["modules"] = oracle
    client = module_client(fixture, multi=True)
    client.intake["inserts"] *= 3
    for entry in client.intake["inserts"]:
        entry["handle"] = "0x000A"
    row = grade(fixture, client)
    assert row["set_evidence"] == {DESIGN_SET["set_id"]: {
        "rows_total": 3, "distinct_handles": 1, "duplicate_handle_rows": 2,
        "multiplicity": [[1, 1, 3]], "space_layout": [["model", "Model", 3]],
        "sample_handles": ["A"]}}
    monkeypatch.setattr(x04, "set_evidence", lambda intake, set_id: {})
    without_evidence = grade(fixture, module_client(fixture, multi=True))
    assert row["steps"] == without_evidence["steps"]
    assert row["evidence"] == without_evidence["evidence"]
    assert row["counts"] == without_evidence["counts"] == [12]
    assert row["steps"]["5"] == ("PASS" if oracle == 12 else "FAIL")


@pytest.mark.parametrize("stage", ["catalog", "module_listing", "module_selection", "layer"])
def test_stop_on_later_http_error(fixture, monkeypatch, stage):
    client = module_client(fixture) if stage.startswith("module_") else FakeClient()
    fixtures = [{**fixture, "id": f"customer-{n:02}"} for n in range(1, 4)]
    uploads, module_requests = [], []
    responses = deepcopy(client.module_responses)
    original_run = client.run

    def run(*args, **kw):
        if len(uploads) == 2:
            return 503, {}
        return original_run(*args, **kw)

    def transport(method, path, **kw):
        if path == "/api/drawings/upload":
            uploads.append(kw["data"])
            client.module_responses = deepcopy(responses)
            module_requests.clear()
        if path == "/api/run?wait=1":
            module_requests.append(kw)
        if len(uploads) == 2 and (
                (stage == "catalog" and path == "/api/tools") or
                (stage == "module_listing" and path == "/api/run?wait=1") or
                (stage == "module_selection" and path == "/api/run?wait=1"
                 and len(module_requests) == 2)):
            return 503, {}
        return client.transport(method, path, **kw)

    if stage == "layer":
        monkeypatch.setattr(client, "run", run)
    rows = x04.grade_files(client, transport, project=PROJECT, fixtures=fixtures,
                           stop_on_5xx=True)
    assert len(uploads) == 2
    assert rows["customer-02"]["steps"]["1"] == "PASS"
    assert rows["customer-02"]["steps"]["4"] == "PASS"
    assert rows["customer-02"]["steps"]["5"] == "UNREADABLE"
    assert rows["customer-03"]["steps"] == {str(n): "UNREADABLE" for n in range(1, 7)}
    assert rows["customer-03"]["evidence"] == {
        str(n): "not attempted: pass stopped after HTTP 503 on customer-02"
        for n in range(1, 7)}


@pytest.mark.parametrize("selector", ["set_id", "set_ids"])
def test_count_modules_live_uses_manifest_selection_and_records_catalog(fixture, selector):
    client = module_client(fixture)
    fixture["module_set"] = {selector: DESIGN_SET["set_id"] if selector == "set_id"
                             else [DESIGN_SET["set_id"]]}
    del fixture["module_layers"]
    row = grade(fixture, client)
    assert row["steps"]["5"] == row["steps"]["6"] == "PASS"
    assert row["counts"] == [12]
    assert row["tool"] == MODULE_TOOL and row["limits"] == []
    assert row["candidate_sets"] == [DESIGN_SET]
    assert "design_subset" not in fixture and "design_window" not in row
    calls = module_calls(client)
    assert calls[0]["json_body"] == {
        "tool": "count-modules", "catalog_digest": MODULE_TOOL["catalog_digest"], "dwg": "v",
        "params": {"drawing_id": "d"}}
    assert calls[1]["json_body"] == {
        **calls[0]["json_body"], "params": {"drawing_id": "d", "set_ids": [DESIGN_SET["set_id"]]}}
    assert all(call["headers"] == {"X-Org-Id": "o", "X-Project-Id": "p"} for call in calls)
    assert not any(call[0] == "count-by-layer" for call in client.calls)


def test_count_modules_mismatch_fails_even_for_competing_sets(fixture):
    client = module_client(fixture, multi=True)
    fixture["design_is_whole_layer"] = False
    fixture["modules"] = 11
    row = grade(fixture, client)
    assert row["steps"]["5"] == "FAIL"
    assert row["evidence"]["5"] == "count 12, oracle 11"
    assert row["steps"]["6"] == "PASS"


@pytest.mark.parametrize("count,verdict", [(510, "PASS"), (1530, "FAIL")])
def test_design_window_selects_subset_of_module_set(fixture, count, verdict):
    client = module_client(fixture, count=count)
    fixture["modules"] = 510
    window = [0, 1.5, 100, 200]
    fixture["design_subset"] = {"required": True, "window": window, "param": "region"}
    for _, body in client.module_responses:
        result = body["result"]
        result["sets"][0]["count"] = 1530
        if result["selected_sets"]:
            result["selected_sets"][0]["count"] = 1530
            result["selected_set"]["count"] = 1530
    row = grade(fixture, client)
    assert row["steps"]["5"] == verdict
    assert row["evidence"]["5"] == f"count {count}, oracle 510"
    assert row["steps"]["6"] == "PASS"
    assert row["counts"] == [count]
    assert row["design_window"] == window
    calls = module_calls(client)
    assert calls[0]["json_body"]["params"] == {"drawing_id": "d"}
    assert calls[1]["json_body"]["params"] == {
        "drawing_id": "d", "set_ids": [DESIGN_SET["set_id"]], "region": window}


@pytest.mark.parametrize("bound", [False, True])
def test_required_design_window_missing_skips_selection(fixture, bound):
    client = module_client(fixture, multi=True)
    fixture["design_subset"] = {"required": True}
    if bound:
        fixture["design_subset"]["window"] = None
    row = grade(fixture, client)
    assert row["steps"]["5"] == "FAIL"
    assert row["evidence"]["5"] == (
        "X04_DESIGN_SUBSET_SELECTION_MISSING: design window not bound")
    assert row["steps"]["6"] == "PASS"
    assert row["evidence"]["6"] == "module_set selected"
    assert len(module_calls(client)) == 1
    assert row["counts"] == []


@pytest.mark.parametrize("status,ok", [(200, False), (422, False), (503, True)])
@pytest.mark.parametrize("error", ["region boundary is ambiguous",
                                   {"message": "region boundary is ambiguous"}])
def test_product_refusal_of_design_window_fails(fixture, status, ok, error):
    client = module_client(fixture)
    fixture["design_subset"] = {"required": True, "window": [0, 0, 100, 100]}
    client.module_responses[1] = (status, {"ok": ok, "error": error})
    row = grade(fixture, client)
    assert row["steps"]["5"] == "FAIL"
    assert row["evidence"]["5"] == (
        "X04_DESIGN_SUBSET_SELECTION_MISSING: region boundary is ambiguous")
    assert len(module_calls(client)) == 2
    assert row["counts"] == []


def test_design_window_refusal_redacts_product_message(fixture):
    client = module_client(fixture)
    fixture["design_subset"] = {"window": [0, 0, 100, 100]}
    secret = "secret1234567890abcdefghij"
    client.module_responses[1] = (422, {"error": {"message": f"boundary {secret}"}})
    row = grade(fixture, client)
    assert row["steps"]["5"] == "FAIL"
    assert row["evidence"]["5"] == (
        "X04_DESIGN_SUBSET_SELECTION_MISSING: boundary [redacted]")
    assert secret not in json.dumps(row)


@pytest.mark.parametrize("window", [[0, 0, 1], [0, 0, 1, float("inf")],
                                    [0, 0, 1, float("nan")], [0, 0, 1, True],
                                    [0, 0, 1, "2"]])
def test_invalid_design_window_is_not_sent(fixture, window):
    client = module_client(fixture)
    fixture["design_subset"] = {"window": window}
    row = grade(fixture, client)
    assert row["steps"]["5"] == "UNREADABLE"
    assert row["evidence"]["5"] == "design_subset.window"
    assert len(module_calls(client)) == 1


def test_count_modules_absent_preserves_layer_fallback_and_notes_limit(fixture):
    client = FakeClient()
    client.catalog["tools"] = [{"name": "count-by-layer"}]
    row = grade(fixture, client)
    assert row["steps"]["5"] == "PASS"
    assert row["counts"] == [12]
    assert row["limits"] == ["count-modules not live"]
    assert any(call[0] == "count-by-layer" for call in client.calls)


def test_competing_module_set_selected_through_picker_api(fixture):
    client = module_client(fixture, multi=True)
    fixture["design_is_whole_layer"] = False
    client.counts["Modules"] = 36
    row = grade(fixture, client)
    assert row["steps"]["5"] == row["steps"]["6"] == "PASS"
    assert row["evidence"]["6"] == "module_set selected"
    assert row["candidate_sets"] == [OTHER_SET, DESIGN_SET]
    assert module_calls(client)[1]["json_body"]["params"]["set_ids"] == [DESIGN_SET["set_id"]]


def test_manifest_can_select_overlapping_sets_without_summing_them(fixture):
    client = module_client(fixture, multi=True)
    fixture["modules"] = 24
    ids = [OTHER_SET["set_id"], DESIGN_SET["set_id"]]
    # The product schema gives nonempty set_ids precedence over set_id and
    # deduplicates choices. Its count is the physical union of selected sets.
    fixture["module_set"] = {"set_id": "ignored", "set_ids": [*ids, ids[0]]}
    result = client.module_responses[1][1]["result"]
    result.update(selected_sets=deepcopy([DESIGN_SET, OTHER_SET]), selected_set=None, count=24)
    row = grade(fixture, client)
    assert row["steps"]["5"] == row["steps"]["6"] == "PASS"
    assert row["counts"] == [24] and row["module_set"] == {"set_ids": ids}
    assert module_calls(client)[1]["json_body"]["params"]["set_ids"] == ids


def test_competing_sets_without_manifest_binding_are_unreadable(fixture):
    client = module_client(fixture, multi=True)
    del fixture["module_set"]
    row = grade(fixture, client)
    assert row["steps"]["5"] == row["steps"]["6"] == "UNREADABLE"
    assert row["candidate_sets"] == [OTHER_SET, DESIGN_SET]
    assert row["evidence"]["5"] == row["evidence"]["6"] == (
        "no module_set bound in manifest; 2 candidates; top three: "
        f"{OTHER_SET['set_id']} (Alternate/Panel B, 24); "
        f"{DESIGN_SET['set_id']} (Modules/MODULE, 12)")
    assert len(module_calls(client)) == 1


def test_unbound_candidates_are_sorted_and_evidence_shows_only_top_three(fixture):
    client = module_client(fixture)
    del fixture["module_set"]
    sets = [{**DESIGN_SET, "set_id": set_id, "count": count, "extra": "ignored"}
            for set_id, count in [("z", 12), ("b", 24), ("a", 24), ("c", 18)]]
    client.module_responses[0][1]["result"]["sets"] = sets
    row = grade(fixture, client)
    assert row["candidate_sets"] == [
        {key: item[key] for key in ("set_id", "layer", "block", "entity_type", "count")}
        for item in (sets[2], sets[1], sets[3], sets[0])]
    assert row["steps"]["5"] == row["steps"]["6"] == "UNREADABLE"
    assert row["evidence"]["5"] == row["evidence"]["6"] == (
        "no module_set bound in manifest; 4 candidates; top three: "
        "a (Modules/MODULE, 24); b (Modules/MODULE, 24); c (Modules/MODULE, 18)")
    assert len(module_calls(client)) == 1


def test_single_unbound_candidate_preserves_nothing_to_pick_verdict(fixture):
    client = module_client(fixture)
    del fixture["module_set"]
    row = grade(fixture, client)
    assert row["candidate_sets"] == [DESIGN_SET]
    assert row["steps"]["5"] == "UNREADABLE"
    assert "1 candidates" in row["evidence"]["5"]
    assert row["steps"]["6"] == "PASS"
    assert row["evidence"]["6"] == "nothing to pick"


@pytest.mark.parametrize("bound", [False, True])
def test_recorded_candidates_are_capped_without_limiting_selection(fixture, bound):
    client = module_client(fixture)
    if not bound:
        del fixture["module_set"]
    sets = [DESIGN_SET, *[{**OTHER_SET, "set_id": f"set-{n:02}", "count": 100 + n}
                          for n in range(51)]]
    for _, body in client.module_responses:
        body["result"]["sets"] = deepcopy(sets)
    row = grade(fixture, client)
    assert row["candidate_sets"] == list(reversed(sets[2:]))
    assert len(row["candidate_sets"]) == 50
    if bound:
        assert row["steps"]["5"] == row["steps"]["6"] == "PASS"
        assert row["counts"] == [12]
        assert row["module_set"] == {"set_ids": [DESIGN_SET["set_id"]]}
    else:
        assert row["steps"]["5"] == row["steps"]["6"] == "UNREADABLE"
        assert "52 candidates" in row["evidence"]["5"]


@pytest.mark.parametrize("catalog_live", [False, True])
def test_candidate_api_absent_is_unreadable(fixture, catalog_live):
    client = module_client(fixture, multi=True)
    fixture["design_is_whole_layer"] = False
    if catalog_live:
        client.module_responses[0] = (404, {"detail": "Not Found"})
    else:
        client.catalog = {"tools": []}
    row = grade(fixture, client)
    assert row["steps"]["6"] == "UNREADABLE"
    assert row["evidence"]["6"] == "module-set picker not live"
    assert len(module_calls(client)) == int(catalog_live)


@pytest.mark.parametrize("field,value", [
    ("sets", None), ("sets", {}), ("sets", []), ("sets", [None]),
    ("sets", [{**DESIGN_SET, "set_id": ""}]),
    ("sets", [DESIGN_SET, DESIGN_SET]),
    ("sets", [{**DESIGN_SET, "count": True}]),
    ("sets", [{**DESIGN_SET, "layer": None}]),
    ("sets", [{**DESIGN_SET, "entity_type": "TEXT"}]),
    ("selection_required", False), ("selection_required", 1),
    ("selected_sets", [DESIGN_SET]), ("selected_set", DESIGN_SET), ("count", 12),
])
def test_malformed_candidates_fail_closed_naming_field(fixture, field, value):
    client = module_client(fixture, multi=True)
    client.module_responses[0][1]["result"][field] = value
    row = grade(fixture, client)
    assert row["steps"]["5"] == row["steps"]["6"] == "UNREADABLE"
    assert field in row["evidence"]["6"]
    assert len(module_calls(client)) == 1
    assert row["counts"] == []


@pytest.mark.parametrize("field,value", [
    ("count", None), ("count", True), ("count", -1), ("count", 12.0),
    ("count", 2**53), ("count", 11),
    ("sets", [OTHER_SET]), ("selected_sets", []), ("selected_sets", [OTHER_SET]),
    ("selected_sets", [{**DESIGN_SET, "count": 11}]),
    ("selected_set", None), ("selection_required", True),
])
def test_malformed_selected_count_cannot_pass(fixture, field, value):
    client = module_client(fixture, multi=True)
    client.module_responses[1][1]["result"][field] = value
    row = grade(fixture, client)
    assert row["steps"]["5"] == row["steps"]["6"] == "UNREADABLE"
    assert field in row["evidence"]["5"]
    assert row["counts"] == []


@pytest.mark.parametrize("field,value", [("ok", False), ("error", {"message": CAP}),
                                         ("tool", "count-by-layer"), ("version", "2.0.0"),
                                         ("result", None)])
def test_malformed_module_envelope_is_unreadable(fixture, field, value):
    client = module_client(fixture)
    client.module_responses[0][1][field] = value
    row = grade(fixture, client)
    assert row["steps"]["5"] == row["steps"]["6"] == "UNREADABLE"
    assert field in row["evidence"]["5"]
    assert CAP not in json.dumps(row)


@pytest.mark.parametrize("status", [202, 403, 500])
def test_module_listing_http_errors_do_not_fall_back_or_pass(fixture, status):
    client = module_client(fixture)
    client.module_responses[0] = (status, {"job_id": "pending", "message": CAP})
    row = grade(fixture, client)
    assert row["steps"]["5"] == row["steps"]["6"] == "UNREADABLE"
    assert "HTTP status" in row["evidence"]["5"]
    assert row["counts"] == [] and row["limits"] == []
    assert CAP not in json.dumps(row)


@pytest.mark.parametrize("selector", ["opaque-id", {}, {"set_id": "stale"},
                                      {"set_ids": []}, {"set_ids": "wrong"},
                                      {"set_ids": [True]}, {"layer": "Modules"}])
def test_invalid_manifest_module_selector_is_unreadable(fixture, selector):
    client = module_client(fixture, multi=True)
    fixture["module_set"] = selector
    row = grade(fixture, client)
    assert row["steps"]["5"] == row["steps"]["6"] == "UNREADABLE"
    assert "module_set" in row["evidence"]["5"]
    assert len(module_calls(client)) == 1


@pytest.mark.parametrize("catalog,field", [
    ({}, "tools"), ({"tools": None}, "tools"), ({"tools": [None]}, "tools"),
    ({"tools": [MODULE_TOOL, MODULE_TOOL]}, "name"),
    ({"tools": [{**MODULE_TOOL, "version": None}]}, "version"),
    ({"tools": [{**MODULE_TOOL, "catalog_digest": ""}]}, "catalog_digest"),
])
def test_malformed_catalog_does_not_fall_back(fixture, catalog, field):
    client = module_client(fixture)
    client.catalog = catalog
    row = grade(fixture, client)
    assert row["steps"]["5"] == "UNREADABLE"
    assert field in row["evidence"]["5"]
    assert row["counts"] == [] and row["limits"] == []
    assert not module_calls(client)


def test_forbidden_tokens_drop_anonymous_short_and_duplicates():
    fixture = {"id": "customer", "module_blocks": ["*U123", "MODULE", "module", "abc"]}
    assert x04.forbidden_tokens(fixture) == ["customer", "MODULE"]


def test_explicit_forbidden_tokens_preserve_short_and_anonymous_tokens():
    fixture = {"id": "customer", "module_blocks": ["MODULE"]}
    fixture["forbidden_tokens"] = ["custom", "*U999", "foo", "CUSTOM"]
    assert x04.forbidden_tokens(fixture) == ["custom", "*U999", "foo"]
    assert fixture["forbidden_tokens"] == ["custom", "*U999", "foo", "CUSTOM"]


def test_explicit_empty_forbidden_tokens_do_not_fall_back():
    assert x04.forbidden_tokens({"id": "customer", "module_blocks": ["MODULE"],
                                 "forbidden_tokens": []}) == []
    assert x04.forbidden_tokens({"forbidden_tokens": ["x", "x"]}) == ["x"]


class FakeRunner:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.calls = []

    def __call__(self, args, **kw):
        self.calls.append((args, kw))
        code, output = next(self.replies)
        return SimpleNamespace(returncode=code, stdout=output, stderr="")


def test_source_hit_names_token_file_without_line():
    runner = FakeRunner([(0, ""), (0, SOURCE + ':server/app.py:42:secret = "MODULE private drawing text"')])
    verdict, evidence = x04.scan_source("repo", SOURCE, ["MODULE", "customer"], runner)
    assert verdict == "FAIL" and "MODULE" in evidence and "server/app.py" in evidence
    assert "private drawing text" not in evidence and "secret =" not in evidence
    args, kw = runner.calls[1]
    assert args == ["git", "grep", "-n", "-I", "-F", "-i", "-e", "MODULE", "-e", "customer",
                    SOURCE, "--", "server", "web", "platform", "engine", "da", "executor"]
    assert kw["timeout"] == 120 and "shell" not in kw


def test_commit_absent_after_one_fetch():
    runner = FakeRunner([(1, ""), (1, ""), (1, "")])
    assert x04.scan_source("repo", SOURCE, ["MODULE"], runner)[0] == "UNREADABLE"
    assert [args[1] for args, _ in runner.calls] == ["cat-file", "fetch", "cat-file"]
    assert runner.calls[1][1]["timeout"] == 300


def test_fetch_restores_commit_and_no_hits_pass():
    runner = FakeRunner([(1, ""), (0, ""), (0, ""), (1, "")])
    assert x04.scan_source("repo", SOURCE, ["MODULE"], runner)[0] == "PASS"


def test_source_git_failure_unreadable():
    runner = FakeRunner([(0, ""), (128, "")])
    assert x04.scan_source("repo", SOURCE, ["MODULE"], runner)[0] == "UNREADABLE"


def test_source_timeout_unreadable():
    def runner(args, **kw):
        raise subprocess.TimeoutExpired(args, kw["timeout"])
    assert x04.scan_source("repo", SOURCE, ["MODULE"], runner)[0] == "UNREADABLE"


def test_score_seven_steps_and_precedence(fixture):
    row = grade(fixture)
    steps = x04.score({fixture["id"]: row}, ("PASS", "no hits"))
    assert [step.n for step in steps] == list(range(1, 8))
    assert steps[3].verdict == steps[5].verdict == "PASS"
    assert all(len(step.evidence) < 2000 for step in steps)
    assert x04.step_verdict({"a": "FAIL", "b": "UNREADABLE"}) == "FAIL"
    assert x04.step_verdict({"a": "PASS", "b": "UNREADABLE"}) == "UNREADABLE"
    assert x04.step_verdict({}) == "UNREADABLE"


def test_no_insert_fixtures_unreadable(fixture):
    fixture["module_entity"] = "LWPOLYLINE"
    fixture["module_blocks"] = []
    row = grade(fixture)
    assert row["steps"]["4"] == "PASS"
    assert x04.score({fixture["id"]: row}, ("PASS", "no hits"))[3].verdict == "UNREADABLE"


@pytest.mark.parametrize("option", [None, "1", "0", True])
def test_run_writes_safe_files_and_uses_manifest(fixture, tmp_path, monkeypatch, option):
    client = FakeClient()
    client.upload["checkout_capability"] = CAP
    client.provenance["arbitrary_text"] = CAP
    ctx = SimpleNamespace(manifest={"fixtures": [fixture], "source_repo": "repo"},
                          fixtures=[{"id": fixture["id"], "ok": True}], out_dir=tmp_path,
                          source_sha=SOURCE, base="https://example.test")
    if option is not None:
        ctx.options = {"stop_on_5xx": option}
    original_grade_files = x04.grade_files

    def grade_files(*args, **kw):
        assert kw["stop_on_5xx"] is (option == "1")
        return original_grade_files(*args, **kw)

    monkeypatch.setattr(x04, "grade_files", grade_files)

    @contextmanager
    def signed_in(repo, source_sha, *, origin):
        assert repo == Path(x04.__file__).resolve().parents[3]
        assert source_sha == SOURCE and origin == ctx.base
        yield "page", {"headers": CAP}

    monkeypatch.setattr(x04.xbench_session, "signed_in", signed_in)
    monkeypatch.setattr(x04, "_transport", lambda page, **kw: client.transport)
    monkeypatch.setattr(x04, "StudioClient", lambda transport: client)
    scans = []

    def scan(repo, sha, tokens):
        scans.append((repo, sha, tokens))
        return "PASS", "no hits"

    monkeypatch.setattr(x04, "scan_source", scan)
    assert len(x04.run(ctx)) == 7
    raw = (tmp_path / "files.json").read_text()
    assert CAP not in raw and "checkout_capability" not in raw and "X-Checkout-Capability" not in raw
    assert json.loads(raw)[fixture["id"]]["steps"]["5"] == "PASS"
    assert client.deleted == ["p"]
    assert scans == [("repo", SOURCE, [fixture["id"], "MODULE"])]
