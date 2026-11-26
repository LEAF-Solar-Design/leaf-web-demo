"""Synthetic checks for identity-bound X09 execution and scratch cleanup."""
import copy
import hashlib
import io

import ezdxf
import pytest

from benches.x09_read_probe import layer_probe


SOURCE = "a" * 40
TOOL = {"name": "instant-list-layers", "version": "1.0.0", "catalog_digest": "sha256:" + "b" * 64}
NAMES = ["0", "Defpoints", "MODULE", "ROOF"]


class Client:
    def __init__(self):
        self.catalog = [copy.deepcopy(TOOL)]
        self.body = {"tool": TOOL["name"], "version": TOOL["version"], "degraded_mode": False,
                     "project_context": {"intake_sha256": "c" * 64}, "result": {"layers": NAMES}}
        self.deleted = []
        self.created = False
        self.status = 200
        self.fail_at = None
        self.catalog_reads = 0
        self.drift = False

    def tools(self):
        self.catalog_reads += 1
        return [] if self.drift and self.catalog_reads > 1 else self.catalog

    def create_project(self, title):
        self.created = True
        return {"project_id": "p", "org_id": "o"}

    def upload_drawing(self, name, raw):
        return "d"

    def wait_upload(self, *args, **kwargs):
        if self.fail_at == "upload":
            raise RuntimeError("private failure must not be persisted")

    def adopt_drawing(self, *args):
        return {"version_id": "v", "drawing_id": "d"}

    def drawing_context(self, *args):
        return {"context": {"intake_sha256": "c" * 64}}

    def run(self, *args, **kwargs):
        assert kwargs["params"] == {}
        return self.status, self.body

    def delete_project(self, project):
        self.deleted.append(project)
        if self.fail_at == "delete":
            raise RuntimeError("private cleanup error")


def run(client=None, **kwargs):
    doc = ezdxf.new()
    for name in ("MODULE", "ROOF"):
        doc.layers.new(name)
    stream = io.StringIO()
    doc.write(stream)
    raw = stream.getvalue().encode()
    args = dict(client=client or Client(), raw=raw,
                fixture={"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()},
                tool=TOOL, source_sha=SOURCE, health=lambda: SOURCE)
    args.update(kwargs)
    return layer_probe(**args)


def test_pass_uses_empty_layer_table_entries_and_cleans_project():
    client = Client()
    result = run(client)
    assert result["verdict"] == "PASS"
    assert result["expected"] == NAMES
    assert result["assertion"] == "exact-values"
    assert client.deleted == ["p"]


def test_crlf_dxf_preserves_all_layer_table_names():
    doc = ezdxf.new()
    doc.layers.new("MODULE")
    doc.layers.new("ROOF")
    stream = io.StringIO()
    doc.write(stream)
    raw = stream.getvalue().replace("\n", "\r\n").encode()
    result = run(raw=raw, fixture={"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()})
    assert result["expected"] == NAMES
    assert result["verdict"] == "PASS"


@pytest.mark.parametrize("actual", [["MODULE", "ROOF"], []])
def test_wrong_implementation_is_a_decided_failure(actual):
    client = Client()
    client.body["result"]["layers"] = actual
    assert run(client)["verdict"] == "FAIL"
    assert client.deleted == ["p"]


@pytest.mark.parametrize("field,value", [("tool", "other"), ("version", "9.0.0"),
                                         ("degraded_mode", True), ("ok", False),
                                         ("project_context", {}), ("result", {"layers": None}),
                                         ("result", {"layers": ["MODULE", "MODULE"]})])
def test_unbound_or_incomplete_output_never_passes_or_fails(field, value):
    client = Client()
    client.body[field] = value
    assert run(client)["verdict"] == "UNREADABLE"
    assert client.deleted == ["p"]


@pytest.mark.parametrize("status", [202, 401, 500, True])
def test_no_complete_response(status):
    client = Client()
    client.status = status
    assert run(client)["verdict"] == "UNREADABLE"


def test_fixture_mismatch_does_not_create_project():
    client = Client()
    assert run(client, fixture={"bytes": 0, "sha256": "f" * 64})["verdict"] == "UNREADABLE"
    assert not client.created


def test_catalog_drift_invalidates_observed_mismatch():
    client = Client()
    client.drift = True
    client.body["result"]["layers"] = ["MODULE"]
    assert run(client)["verdict"] == "UNREADABLE"


def test_source_drift_invalidates_observed_mismatch():
    client = Client()
    client.body["result"]["layers"] = ["MODULE"]
    reads = iter([SOURCE, "d" * 40])
    assert run(client, health=lambda: next(reads))["verdict"] == "UNREADABLE"


@pytest.mark.parametrize("stage", ["upload", "delete"])
def test_cleanup_after_failure_and_no_exception_text(stage):
    client = Client()
    client.fail_at = stage
    result = run(client)
    assert result["verdict"] == "UNREADABLE"
    assert client.deleted == ["p"]
    assert "private" not in str(result)
