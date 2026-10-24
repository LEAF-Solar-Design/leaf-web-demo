"""Widget parity, tenant admission, pinned output identity and bounded values."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import sqlite3

import pytest

import lab_artifacts as ledger
from lab_doc_validators import TooLarge
from lab_widget_doc import InvalidWidget, validate_widget
from lab_widget_values import MAX_RESPONSE_BYTES, MAX_VALUE_BYTES, resolve_values
from test_lab_artifacts import client

ROOT = Path(__file__).resolve().parents[2]
PARITY = json.loads((ROOT / "contract/fixtures/widget/parity/basic.json").read_text(encoding="utf-8"))
EMPTY = {"schema": "leaf.lab-graph.v1", "blocks": [], "pipes": [], "outputs": {}}
PARITY_REASONS = {
    "unknown kind": "Unknown primitive kind.",
    "HTML field": "Unsupported document field.",
    "URL field": "Unsupported document field.",
    "action field": "Unsupported document field.",
    **{name: "Numeric value outside bounds." for name in (
        "NaN substitute", "negative geometry", "oversized geometry", "zero height", "scale bound")},
    **{name: "Invalid primitive style." for name in (
        "weight bound", "unknown tone", "alignment", "visibility")},
    "primitive gate": "Unsupported min_renderer.",
    "binding gate": "Unsupported min_renderer.",
    "binding source": "Unknown binding.",
    "unknown binding": "Unknown binding.",
    "ledger bound": "Ledger supports at most 4 rows.",
    "win rate window": "Win rate requires the today-utc window field.",
}


@pytest.fixture(autouse=True)
def store(monkeypatch, tmp_path):
    monkeypatch.setenv("LEAF_LAB_STORE", "sqlite")
    monkeypatch.setenv("LEAF_LAB_DB", str(tmp_path / "lab.db"))
    monkeypatch.setenv("LEAF_RUNTIME_ENV", "test")
    monkeypatch.setenv("LEAF_PLATFORM_POSTGRES_REQUIRED", "0")
    monkeypatch.setenv("LEAF_LAB_SURFACE_ENABLED", "1")
    monkeypatch.setenv("LEAF_AUTH_LIVE", "1")
    return tmp_path / "lab.db"


def binding(doc_id="source", output="total", **extra):
    return {"source": "graph.output", "doc_id": doc_id, "output": output,
            "min_renderer": "1.0.0", "scale": "identity", "tone": "auto", **extra}


def graph(value=42):
    return {**EMPTY, "blocks": [{"id": "constant", "registry": {
        "id": "constant", "version": "1.0.0", "code_sha256": "0" * 64,
    }, "settings": {"value": value}}],
        "outputs": {"total": {"node": "constant", "port": "result"}}}


def save_source(value=42, doc_id="source"):
    return ledger.save("tenant-a", doc_id, graph(value), None, "member")


def save_widget():
    return ledger.save("tenant-a", "widget", PARITY["doc"], None, "member", kind="widget")


def size(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


@pytest.mark.parametrize("verdict", PARITY["verdicts"], ids=lambda case: case["name"])
def test_shared_widget_parity(verdict):
    doc = copy.deepcopy(PARITY["doc"])
    path = verdict["path"]
    if path:
        parent = doc
        for key in path[:-1]:
            parent = parent[key]
        parent[path[-1]] = verdict["value"]
    if verdict["accepted"]:
        assert json.loads(validate_widget(doc)) == doc
    else:
        with pytest.raises(InvalidWidget) as caught:
            validate_widget(doc)
        assert caught.value.code == "invalid_widget_document"
        assert caught.value.reason == PARITY_REASONS[verdict["name"]]
        assert caught.value.pointer.startswith("/")


@pytest.mark.parametrize("mutation", ["duplicate", "containment", "renderer", "active"])
def test_widget_semantic_checks(mutation):
    doc = copy.deepcopy(PARITY["doc"])
    elements = doc["formats"]["la"]["els"]
    if mutation == "duplicate":
        elements[1]["id"] = elements[0]["id"]
    elif mutation == "containment":
        elements[0]["x"] = 360
    elif mutation == "renderer":
        doc["min_renderer"] = "1.0.1"
    else:
        doc["active"] = "diMin"
        del doc["formats"]["diMin"]
    with pytest.raises(InvalidWidget):
        validate_widget(doc)


def test_graph_default_widget_save_and_fixed_kind(store):
    first = ledger.save("tenant-a", "graph", EMPTY, None, "member")
    assert first["kind"] == "graph"
    widget = save_widget()
    assert widget["kind"] == "widget"
    assert ledger.get("tenant-a", "widget")["graph"] == PARITY["doc"]
    assert ledger.list_revisions("tenant-a", "widget")[0]["kind"] == "widget"
    assert ledger.restore("tenant-a", "widget", 1, 1, "member")["kind"] == "widget"
    with pytest.raises(ledger.KindFixed):
        ledger.save("tenant-a", "widget", EMPTY, 2, "member")
    with sqlite3.connect(store) as connection:
        assert {row[1]: row[4] for row in connection.execute("PRAGMA table_info(lab_documents)")}["kind"] == "'graph'"
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE lab_documents SET kind='unknown'")


def test_graph_output_binding_is_additive_and_closed():
    doc = copy.deepcopy(PARITY["doc"])
    element = doc["formats"]["la"]["els"][1]
    element["binding"] = binding(field="amount")
    assert json.loads(validate_widget(doc)) == doc
    del element["binding"]["doc_id"]
    with pytest.raises(InvalidWidget):
        validate_widget(doc)
    element["binding"] = {**binding(), "execute": "bad"}
    with pytest.raises(InvalidWidget):
        validate_widget(doc)


def test_existing_sqlite_documents_expand_to_graph(store):
    with sqlite3.connect(store) as connection:
        connection.execute("CREATE TABLE lab_documents (tenant_id TEXT, doc_id TEXT, head INTEGER, published_revision INTEGER, PRIMARY KEY(tenant_id,doc_id))")
    assert ledger.save("tenant-a", "old", EMPTY, None, "member")["kind"] == "graph"


def test_widget_save_http_and_kind_conflict():
    http = client()
    response = http.post("/api/lab/docs/widget/save", json={
        "kind": "widget", "graph": PARITY["doc"], "expected_head": None})
    assert response.status_code == 200, response.text
    assert response.json()["kind"] == "widget"
    conflict = http.post("/api/lab/docs/widget/save", json={"graph": EMPTY, "expected_head": 1})
    assert conflict.status_code == 409
    assert conflict.json()["detail"] == "lab_document_kind_fixed"


def test_invalid_widget_http_has_pointer_and_never_writes(store):
    doc = copy.deepcopy(PARITY["doc"])
    doc["formats"]["la"]["els"][0]["x"] = -1
    response = client().post("/api/lab/docs/widget/save", json={
        "kind": "widget", "graph": doc, "expected_head": None})
    assert response.status_code == 422
    assert response.json() == {"detail": "invalid_widget_document",
                               "pointer": "/formats/la/els/0/x", "block": None}
    assert not store.exists()


def test_widget_oversize_http_and_validator(store):
    doc = {"text": "x" * 262145}
    with pytest.raises(TooLarge):
        validate_widget(doc)
    assert client().post("/api/lab/docs/widget/save", json={
        "kind": "widget", "graph": doc, "expected_head": None}).status_code == 413
    assert not store.exists()


def test_widget_tenant_isolation_and_stale_foreign_save():
    save_widget()
    other = client("tenant-b")
    assert other.get("/api/lab/docs/widget/head").status_code == 404
    response = other.post("/api/lab/docs/widget/save", json={
        "kind": "widget", "graph": PARITY["doc"], "expected_head": 1})
    assert response.status_code == 409
    assert response.json()["detail"] == "stale_expected_head"
    assert other.post("/api/lab/widgets/widget/values", json={"bindings": []}).status_code == 404


def test_32_bindings_read_three_heads_once(monkeypatch):
    for doc_id in ("a", "b", "c"):
        save_source(doc_id=doc_id)
    reads = []
    original = ledger.get

    def spy(tenant, doc_id):
        reads.append((tenant, doc_id))
        return original(tenant, doc_id)

    monkeypatch.setattr(ledger, "get", spy)
    result = resolve_values("tenant-a", [binding(("a", "b", "c")[i % 3]) for i in range(32)])
    assert len(reads) == len(set(reads)) == 3
    assert len(result["values"]) == 32
    assert all(value["value"] == 42 for value in result["values"])


@pytest.mark.parametrize("field,expected", [
    ("absent", "missing"), ("nested.absent", "missing"), ("nested.null", None),
    ("null", None), ("null.absent", "missing"), ("amount", 42),
    ("items.0", None), ("items.1.amount", 7), ("items.length", 2),
    ("items.2", "missing"), ("items.01", "missing"), ("items.-1", "missing"),
    ("items.١", "missing"), ("items.10000000000", "missing"),
    ("__proto__", "missing"), ("prototype", "missing"), ("constructor", "missing"),
    ("nested.constructor", "missing"),
])
def test_F1_absent_field_is_unavailable_and_present_null_resolves(field, expected):
    head = save_source({"amount": 42, "null": None, "nested": {"null": None},
                        "items": [None, {"amount": 7}]})
    value = resolve_values("tenant-a", [binding(field=field)])["values"][0]
    if expected == "missing":
        assert value == {"status": "unavailable", "reason": "field-missing", "truncated": False}
    else:
        assert value == {"status": "resolved", "value": expected, "truncated": False, "pins": {
            "doc_id": "source", "graph_revision": 1, "graph_sha256": head["sha256"],
            "output": {"name": "total", "node": "constant", "port": "result"}, "schema": None,
        }}


def test_F2_values_route_reads_widget_and_each_binding_document_once(monkeypatch):
    save_widget()
    save_source()
    reads = []
    original = ledger.get

    def spy(tenant, doc_id):
        reads.append((tenant, doc_id))
        return original(tenant, doc_id)

    monkeypatch.setattr(ledger, "get", spy)
    response = client().post("/api/lab/widgets/widget/values", json={
        "bindings": [binding("widget"), binding(), binding("widget"), binding()]})
    assert response.status_code == 200, response.text
    assert reads == [("tenant-a", "widget"), ("tenant-a", "source")]
    values = response.json()["values"]
    assert values[0] == values[2] == {
        "status": "unavailable", "reason": "missing-document", "truncated": False}
    assert values[1]["value"] == values[3]["value"] == 42


def test_F3_values_route_requires_widget_target():
    save_source(doc_id="g")
    response = client().post("/api/lab/widgets/g/values", json={"bindings": []})
    assert response.status_code == 409
    assert response.json() == {"detail": "lab_document_not_widget"}


@pytest.mark.parametrize("source", ["graph.output", "kashbot.board"])
@pytest.mark.parametrize("field", ["scale", "tone"])
def test_F5_binding_scale_and_tone_errors_match_client(source, field):
    from lab_widget_doc import validate_binding

    value = binding() if source == "graph.output" else {
        "source": source, "field": "total", "min_renderer": "1.0.0",
        "scale": "identity", "tone": "auto"}
    value[field] = "bogus"
    with pytest.raises(InvalidWidget) as caught:
        validate_binding(value)
    assert caught.value.reason == "Invalid binding scale or tone."
    assert caught.value.pointer == "/" + field
    doc = copy.deepcopy(PARITY["doc"])
    doc["formats"]["la"]["els"][1]["binding"] = value
    response = client().post("/api/lab/docs/widget/save", json={
        "kind": "widget", "graph": doc, "expected_head": None})
    assert response.status_code == 422
    assert response.json() == {"detail": "invalid_widget_document",
                               "pointer": "/formats/la/els/1/binding/" + field, "block": None}
    with pytest.raises(InvalidWidget) as caught:
        validate_widget(doc)
    assert caught.value.reason == "Invalid binding scale or tone."


@pytest.mark.parametrize("missing,reason", [
    ("document", "missing-document"), ("output", "missing-output"), ("port", "missing-port"),
])
def test_missing_values_are_unavailable(missing, reason):
    value = graph()
    if missing == "port":
        value["outputs"]["total"]["port"] = "absent"
    if missing != "document":
        ledger.save("tenant-a", "source", value, None, "member")
    result = resolve_values("tenant-a", [binding(output="absent" if missing == "output" else "total")])
    assert result["values"] == [{"status": "unavailable", "reason": reason, "truncated": False}]


@pytest.mark.parametrize("embedded", [False, True])
def test_host_residency_is_never_evaluated(monkeypatch, embedded):
    from types import SimpleNamespace
    import lab_widget_values

    value = graph()
    if embedded:
        from test_lab_validate import manifest

        value["blocks"][0]["manifest"] = manifest()
    ledger.save("tenant-a", "source", value, None, "member")
    monkeypatch.setattr(lab_widget_values, "lookup", lambda kind, version: SimpleNamespace(
        residency="host" if kind == "constant" else "tenant", effect="read"))
    monkeypatch.setattr(lab_widget_values, "run_operator", lambda *args: pytest.fail("host evaluated"))
    result = resolve_values("tenant-a", [binding()])
    assert result["values"][0]["reason"] == "host-resident"


def test_widget_depth_errors_keep_widget_error_code():
    doc = {"nested": None}
    for _ in range(33):
        doc = {"nested": doc}
    with pytest.raises(InvalidWidget) as caught:
        validate_widget(doc)
    assert caught.value.code == "invalid_widget_document"


def test_pins_follow_head_revision_and_field():
    save_source({"amount": 1})
    head = ledger.save("tenant-a", "source", graph({"amount": 2}), 1, "member")
    value = resolve_values("tenant-a", [binding(field="amount")])["values"][0]
    assert value["value"] == 2
    assert value["pins"] == {
        "doc_id": "source", "graph_revision": 2, "graph_sha256": head["sha256"],
        "output": {"name": "total", "node": "constant", "port": "result"}, "schema": None,
    }


def test_port_schema_is_carried_in_pins():
    from test_lab_validate import manifest

    value = graph()
    value["blocks"][0]["manifest"] = manifest()
    ledger.save("tenant-a", "source", value, None, "member")
    resolved = resolve_values("tenant-a", [binding()])["values"][0]
    assert resolved["status"] == "resolved"
    assert resolved["pins"]["schema"] == value["blocks"][0]["manifest"]["outputs"]["result"]["schema"]


def test_rows_are_cut_at_200():
    save_source(list(range(201)))
    result = resolve_values("tenant-a", [binding()])
    assert result["truncated"] is True
    assert result["values"][0]["value"] == list(range(200))
    assert result["values"][0]["truncated"] is True


def test_value_bytes_and_response_bytes_are_bounded():
    # The stored graph fits 256 KiB; JSON escaping and the repeated pinned
    # value exhaust the wire bounds without an oversized stored document.
    save_source("é" * 120000)
    result = resolve_values("tenant-a", [binding()] * 32)
    assert size(result) <= MAX_RESPONSE_BYTES
    assert all(size(value) <= MAX_VALUE_BYTES for value in result["values"])
    assert result["truncated"] is True
    assert any(value.get("reason") == "response-limit" for value in result["values"])


def test_single_value_byte_cut(monkeypatch):
    import lab_widget_values

    save_source()
    original = ["é" * 10000] * 200
    monkeypatch.setattr(lab_widget_values, "run_operator", lambda *args: original)
    value = resolve_values("tenant-a", [binding()])["values"][0]
    assert size(value) <= MAX_VALUE_BYTES
    assert value["status"] == "resolved"
    assert value["truncated"] is True
    assert 0 < len(value["value"]) < 200
    assert value["value"] == original[:len(value["value"])]


def test_object_exceeding_value_limit_is_unavailable():
    from lab_doc_validators import MAX_GRAPH_BYTES

    original = {"payload": ""}
    original["payload"] = "x" * (MAX_GRAPH_BYTES - size(graph(original)))
    doc_id = "s" * 128
    head = save_source(original, doc_id=doc_id)
    pins = {
        "doc_id": doc_id, "graph_revision": 1, "graph_sha256": head["sha256"],
        "output": {"name": "total", "node": "constant", "port": "result"}, "schema": None,
    }
    assert size({"status": "resolved", "value": original, "truncated": False,
                 "pins": pins}) > MAX_VALUE_BYTES
    result = resolve_values("tenant-a", [binding(doc_id)] * 32)
    assert result == {"values": [{"status": "unavailable", "reason": "value-limit",
                                  "truncated": True, "pins": pins}] * 32,
                      "truncated": True}
    assert size(result) <= MAX_RESPONSE_BYTES
    assert all(size(value) <= MAX_VALUE_BYTES for value in result["values"])


@pytest.mark.parametrize("original", [{"amount": 42}, [1, 2], "hello", None],
                         ids=["object", "list", "string", "null"])
def test_fitting_value_is_unchanged(original):
    head = save_source(original)
    assert resolve_values("tenant-a", [binding()]) == {
        "values": [{"status": "resolved", "value": original, "truncated": False,
                    "pins": {"doc_id": "source", "graph_revision": 1,
                             "graph_sha256": head["sha256"],
                             "output": {"name": "total", "node": "constant", "port": "result"},
                             "schema": None}}],
        "truncated": False,
    }


@pytest.mark.parametrize("original", [[1, 2], "hello"], ids=["list", "string"])
def test_empty_prefix_exceeding_value_limit_is_unavailable(monkeypatch, original):
    import lab_widget_values

    result = {"status": "resolved", "value": original, "truncated": False,
              "pins": {"doc_id": "source"}}
    monkeypatch.setattr(lab_widget_values, "MAX_VALUE_BYTES",
                        size({**result, "value": original[:0], "truncated": True}) - 1)
    assert lab_widget_values._fit(result) == {
        "status": "unavailable", "reason": "value-limit", "truncated": True,
        "pins": {"doc_id": "source"},
    }


@pytest.mark.parametrize("body", [{"bindings": [binding()] * 33}, {"bindings": [{}]}, {"bindings": "bad"}])
def test_bad_bindings_are_refused_before_storage(monkeypatch, body):
    monkeypatch.setattr(ledger, "get", lambda *args: pytest.fail("invalid request read ledger"))
    response = client().post("/api/lab/widgets/widget/values", json=body)
    assert response.status_code == 422


def test_values_route_entitlement_rail_and_store_errors(monkeypatch):
    save_widget()
    save_source()
    path = "/api/lab/widgets/widget/values"
    response = client().post(path, json={"bindings": [binding()]})
    assert response.status_code == 200
    assert response.json()["values"][0]["value"] == 42
    assert client(tier="restricted").post(path, json={"bindings": []}).status_code == 403
    monkeypatch.setenv("LEAF_LAB_SURFACE_ENABLED", "0")
    assert client().post(path, json={"bindings": []}).json()["detail"]["reason"] == "lab_surface_disabled"
    monkeypatch.setenv("LEAF_LAB_SURFACE_ENABLED", "1")
    monkeypatch.setattr(ledger, "get", lambda *args: (_ for _ in ()).throw(RuntimeError("private-host")))
    response = client().post(path, json={"bindings": []})
    assert response.status_code == 503
    assert response.json()["detail"] == "lab_store_unavailable"
    assert "private-host" not in response.text
