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
    assert row["evidence"]["6"] == "no module_set bound in manifest"
    assert len(module_calls(client)) == 1


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


def test_run_writes_safe_files_and_uses_manifest(fixture, tmp_path, monkeypatch):
    client = FakeClient()
    client.upload["checkout_capability"] = CAP
    client.provenance["arbitrary_text"] = CAP
    ctx = SimpleNamespace(manifest={"fixtures": [fixture], "source_repo": "repo"},
                          fixtures=[{"id": fixture["id"], "ok": True}], out_dir=tmp_path,
                          source_sha=SOURCE, base="https://example.test")

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
