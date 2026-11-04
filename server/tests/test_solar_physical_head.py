"""sf-w5-physical-state-head: bounded head discovery and optimistic parent publication over the
Ground physical state store (server/solar_physical_state.py). Every expected value below was
measured by running the module against the FilesystemBackend the physical-state tests seed."""
import hashlib
import copy
import json
import os
import re
import sys
import threading
from pathlib import Path

import pytest
import requests

SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))
sys.path.insert(0, str(SERVER / "tests"))

import solar_artifacts as artifacts  # noqa: E402
import solar_physical_head as ph  # noqa: E402
import solar_physical_state as ps  # noqa: E402
import store  # noqa: E402
import write_loop  # noqa: E402
from test_solar_physical_state import (  # noqa: E402
    DRAWING, GENERATE_SHA, PREFIX, PROJECT, TENANT, TINY, _foreign_artifact, document, written)
from test_w1_design_graph import graph  # noqa: E402,F401
from test_w1_solve_commit import seed, seed_graphless  # noqa: E402
from test_w1_local_graph_adapter import held, run as commit  # noqa: E402
from test_drawing_upload_authority_postgres import postgres_authority  # noqa: E402,F401

LOG = PREFIX + "physical/"


def canonical(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode("utf-8")).hexdigest()


@pytest.fixture
def backend(graph, tmp_path, monkeypatch):
    backend, _ = seed(tmp_path, monkeypatch, graph)
    monkeypatch.setattr(write_loop, "backend_for_tenant", lambda *a, **k: backend)
    return backend


def doc(parent=None, next_handle=2, capability="frame-generate"):
    return document(dict(TINY, next_handle=next_handle), GENERATE_SHA, capability, parent=parent)


def publish(backend, parent=None, next_handle=2, **kw):
    return ph.publish_physical_state(backend, TENANT, DRAWING, doc(parent, next_handle), **kw)


def head(backend, project_id=PROJECT):
    return ph.physical_head(backend, TENANT, DRAWING, project_id=project_id)


def log_keys(backend):
    return {key for key in backend.drawing_object_keys(TENANT, DRAWING) if "/physical/" in key}


def refused(code, fn, *args, **kw):
    with pytest.raises(ps.PhysicalStateError) as exc:
        fn(*args, **kw)
    assert exc.value.code == code
    return exc.value


class Counting:
    """Delegates to a backend and counts reads of head-log keys."""

    def __init__(self, inner):
        self.inner = inner
        self.log_reads = []

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def get(self, key):
        if "/physical/" in key:
            self.log_reads.append(int(key.rsplit("-", 1)[1].split(".")[0]))
        return self.inner.get(key)


# ------------------------------------------------------------------ contract --

def test_physical_head_constants():
    assert (ph.ENTRY_SCHEMA, ph.HEAD_SCHEMA, ph.PUBLISH_SCHEMA) == (
        "leaf.solar-physical-head-entry.v1", "leaf.solar-physical-head.v1", "leaf.solar-physical-head-publish.v1")
    assert (ph.MAX_LOG_ENTRIES, ph.MAX_ENTRY_BYTES, ph.MAX_PROJECT_ID_CHARS) == (4096, 1024, 100)
    assert ph.ENTRY_KEYS == {"schema", "index", "project_id", "parent", "state", "content_sha256"}
    assert len(ph.CODES) == 9 and not ph.CODES & ps.CODES
    error = ph.PhysicalHeadError("PHYSICAL_HEAD_CONFLICT")
    assert isinstance(error, ps.PhysicalStateError) and isinstance(error, ValueError)
    assert error.code == str(error) == "PHYSICAL_HEAD_CONFLICT"


def test_physical_head_entry_key():
    assert ph.entry_key(TENANT, DRAWING, 0) == LOG + "head-0000.json"
    assert ph.entry_key(TENANT, DRAWING, 4095) == LOG + "head-4095.json"
    for bad in (-1, 4096, True, 1.0, "1"):
        refused("PHYSICAL_HEAD_ID_INVALID", ph.entry_key, TENANT, DRAWING, bad)
    for tenant, drawing in (("Bad Tenant", DRAWING), (TENANT, "../x"), (None, DRAWING)):
        refused("PHYSICAL_HEAD_ID_INVALID", ph.entry_key, tenant, drawing, 0)


def test_physical_head_codes_closed():
    source = (SERVER / "solar_physical_head.py").read_text(encoding="utf-8")
    assert set(re.findall(r'"(PHYSICAL_HEAD_[A-Z_]+)"', source)) == ph.CODES


# ------------------------------------------------------------ empty and first --

def test_physical_head_empty(backend):
    assert head(backend) is None
    assert ph.load_physical_head(backend, TENANT, DRAWING, project_id=PROJECT) == (None, None)
    assert log_keys(backend) == set()


def test_physical_head_first_publish(backend):
    result = publish(backend, project_id=PROJECT)
    data = ps.encode_document(doc())
    ref = result["head"]["state"]
    assert result == {"schema": ph.PUBLISH_SCHEMA, "created": True,
                      "head": {"schema": ph.HEAD_SCHEMA, "drawing_id": DRAWING, "project_id": PROJECT,
                               "index": 0, "parent": None, "state": ref}}
    assert ref["content_sha256"] == hashlib.sha256(data).hexdigest() and ref["source_version"] == 1
    assert canonical(result) == MEASURED_FIRST_RESULT
    assert log_keys(backend) == {LOG + "head-0000.json"}
    assert written(backend) == {PREFIX + "artifacts/" + ref["artifact_id"] + ".json",
                                PREFIX + "artifacts/blobs/" + ref["content_sha256"] + ".bin"}
    raw = backend.get(LOG + "head-0000.json")
    assert raw == MEASURED_FIRST_ENTRY and len(raw) == 294
    assert ref["artifact_id"] == "804b7d619d8cbacc36e38fff644611f40778569ffaa804e996b150e8f015c7c7"
    assert raw == ph.entry_bytes(0, PROJECT, None, ref["artifact_id"], ref["content_sha256"])
    assert head(backend) == result["head"]
    view, back = ph.load_physical_head(backend, TENANT, DRAWING, project_id=PROJECT)
    assert view == result["head"] and back == doc() and repr(back["state"]) == repr(doc()["state"])


MEASURED_FIRST_RESULT = "5f6d7a1995fe6b5d5b100484501eb2a8293435248f1a8fb8b2d959ca7cc1c608"
MEASURED_FIRST_ENTRY = (
    b'{"content_sha256":"fd565c6abb1421d7261d9269d42daa568d7e45e677069b69d4bbfad3875a31de","index":0,'
    b'"parent":null,"project_id":"leaf:project:00000000-0000-4000-8000-000000000001",'
    b'"schema":"leaf.solar-physical-head-entry.v1",'
    b'"state":"804b7d619d8cbacc36e38fff644611f40778569ffaa804e996b150e8f015c7c7"}')


def test_physical_head_publish_without_project_binds_the_drawing_project(backend):
    result = publish(backend)
    assert result["head"]["project_id"] == PROJECT and head(backend) == result["head"]


def test_physical_head_chain(backend):
    first = publish(backend)
    second = publish(backend, first["head"]["state"]["artifact_id"], 3)
    third = publish(backend, second["head"]["state"]["artifact_id"], 4)
    assert [r["head"]["index"] for r in (first, second, third)] == [0, 1, 2]
    assert third["head"]["parent"] == second["head"]["state"]["artifact_id"]
    assert head(backend) == third["head"]
    _, back = ph.load_physical_head(backend, TENANT, DRAWING, project_id=PROJECT)
    assert back["state"]["next_handle"] == 4 and back["parent"] == second["head"]["state"]["artifact_id"]
    assert log_keys(backend) == {LOG + "head-0000.json", LOG + "head-0001.json", LOG + "head-0002.json"}
    assert canonical([first, second, third]) == MEASURED_CHAIN


MEASURED_CHAIN = "487a0ab1bda7152e2d9a4b3116f8687109e55a4bb45ac5fc845a59e693223d30"


# ------------------------------------------------------------ retry and races --

def test_physical_head_retry_returns_the_same_head(backend):
    first = publish(backend)
    before = backend.drawing_object_keys(TENANT, DRAWING)
    again = publish(backend, project_id=PROJECT)
    assert again == {"schema": ph.PUBLISH_SCHEMA, "created": False, "head": first["head"]}
    assert backend.drawing_object_keys(TENANT, DRAWING) == before
    child = publish(backend, first["head"]["state"]["artifact_id"], 3)
    assert publish(backend, first["head"]["state"]["artifact_id"], 3) == dict(child, created=False)


def test_physical_head_superseded_retry_is_a_conflict(backend):
    first = publish(backend)
    publish(backend, first["head"]["state"]["artifact_id"], 3)
    before = backend.drawing_object_keys(TENANT, DRAWING)
    refused("PHYSICAL_HEAD_CONFLICT", publish, backend)
    assert backend.drawing_object_keys(TENANT, DRAWING) == before


def test_physical_head_stale_parent_writes_nothing(backend):
    first = publish(backend)
    second = publish(backend, first["head"]["state"]["artifact_id"], 3)
    before = backend.drawing_object_keys(TENANT, DRAWING)
    refused("PHYSICAL_HEAD_CONFLICT", publish, backend, first["head"]["state"]["artifact_id"], 9)
    assert backend.drawing_object_keys(TENANT, DRAWING) == before
    assert head(backend) == second["head"]


def test_physical_head_first_state_must_have_no_parent(backend):
    stored = ps.store_physical_state(backend, TENANT, DRAWING, doc())
    before = backend.drawing_object_keys(TENANT, DRAWING)
    refused("PHYSICAL_HEAD_CONFLICT", publish, backend, stored["state"]["artifact_id"], 3)
    assert backend.drawing_object_keys(TENANT, DRAWING) == before and head(backend) is None


class Racing:
    """Runs a competing publish at the moment this writer creates its head entry."""

    def __init__(self, inner, competitor):
        self.inner = inner
        self.competitor = competitor
        self.raced = False

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def put_if_absent_or_verify(self, key, data):
        if "/physical/" in key and not self.raced:
            self.raced = True
            self.competitor()
        return self.inner.put_if_absent_or_verify(key, data)


def test_physical_head_two_children_of_one_parent(backend):
    first = publish(backend)
    parent = first["head"]["state"]["artifact_id"]
    won = {}
    racing = Racing(backend, lambda: won.update(publish(backend, parent, 3)))
    loser = refused("PHYSICAL_HEAD_CONFLICT", ph.publish_physical_state, racing, TENANT, DRAWING, doc(parent, 7))
    assert loser.code == "PHYSICAL_HEAD_CONFLICT"
    assert won["created"] is True and won["head"]["index"] == 1 and head(backend) == won["head"]
    assert backend.get(LOG + "head-0001.json") == ph.entry_bytes(
        1, PROJECT, parent, won["head"]["state"]["artifact_id"], won["head"]["state"]["content_sha256"])
    assert len([k for k in written(backend) if k.endswith(".json")]) == 3
    assert publish(backend, parent, 3) == dict(won, created=False)


def test_physical_head_threaded_writers_one_wins(backend):
    first = publish(backend)
    parent = first["head"]["state"]["artifact_id"]
    barrier = threading.Barrier(8)
    outcomes = [None] * 8

    def writer(slot):
        barrier.wait()
        try:
            outcomes[slot] = publish(backend, parent, 10 + slot)["head"]["index"]
        except ps.PhysicalStateError as exc:
            outcomes[slot] = exc.code

    threads = [threading.Thread(target=writer, args=(slot,)) for slot in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    assert sorted(outcomes, key=str) == [1] + ["PHYSICAL_HEAD_CONFLICT"] * 7
    current = head(backend)
    assert current["index"] == 1 and current["parent"] == parent
    assert log_keys(backend) == {LOG + "head-0000.json", LOG + "head-0001.json"}


def test_physical_head_identical_concurrent_children_are_one_child(backend):
    first = publish(backend)
    parent = first["head"]["state"]["artifact_id"]
    won = {}
    racing = Racing(backend, lambda: won.update(publish(backend, parent, 3)))
    mine = ph.publish_physical_state(racing, TENANT, DRAWING, doc(parent, 3))
    assert mine == won and mine["created"] is True and head(backend) == mine["head"]


# ---------------------------------------------------------------- the bound --

def test_physical_head_search_reads(backend):
    counting = Counting(backend)
    assert ph.physical_head(counting, TENANT, DRAWING, project_id=PROJECT) is None
    assert counting.log_reads == MEASURED_EMPTY_READS
    parent = None
    for n in range(3):
        parent = publish(backend, parent, 2 + n)["head"]["state"]["artifact_id"]
        counting.log_reads = []
        assert ph.physical_head(counting, TENANT, DRAWING, project_id=PROJECT)["index"] == n
        assert counting.log_reads == MEASURED_READS[n]
    assert max(len(reads) for reads in [MEASURED_EMPTY_READS] + MEASURED_READS) <= 13


MEASURED_EMPTY_READS = [2047, 1023, 511, 255, 127, 63, 31, 15, 7, 3, 1, 0]
MEASURED_READS = [[2047, 1023, 511, 255, 127, 63, 31, 15, 7, 3, 1, 0],
                  [2047, 1023, 511, 255, 127, 63, 31, 15, 7, 3, 1, 2, 0],
                  [2047, 1023, 511, 255, 127, 63, 31, 15, 7, 3, 1, 2]]


class Planted:
    """A head log whose first `count` entries exist; only the last two carry real bytes (the
    search never parses the others). Counts every log read."""

    def __init__(self, inner, count, real):
        self.inner = inner
        self.count = count
        self.real = real
        self.log_reads = []

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def get(self, key):
        if "/physical/" not in key:
            return self.inner.get(key)
        index = int(key.rsplit("-", 1)[1].split(".")[0])
        self.log_reads.append(index)
        if index >= self.count:
            raise KeyError(key)
        return self.real.get(index, b"{}")


@pytest.mark.parametrize("count", [1, 2, 3, 1000, 2048, 2049, 4095, 4096])
def test_physical_head_search_bound(backend, count):
    first = publish(backend)
    second = publish(backend, first["head"]["state"]["artifact_id"], 3)
    a, b = first["head"]["state"], second["head"]["state"]
    real = {count - 1: ph.entry_bytes(count - 1, PROJECT, a["artifact_id"], b["artifact_id"], b["content_sha256"]),
            count - 2: ph.entry_bytes(count - 2, PROJECT, None, a["artifact_id"], a["content_sha256"])}
    if count == 1:
        real = {0: ph.entry_bytes(0, PROJECT, None, a["artifact_id"], a["content_sha256"])}
    planted = Planted(backend, count, real)
    view = ph.physical_head(planted, TENANT, DRAWING, project_id=PROJECT)
    assert view["index"] == count - 1
    assert len(planted.log_reads) == MEASURED_BOUND[count]
    assert len(set(planted.log_reads)) == len(planted.log_reads) <= 13


MEASURED_BOUND = {1: 12, 2: 13, 3: 12, 1000: 13, 2048: 13, 2049: 12, 4095: 13, 4096: 13}


class Stub:
    """A bare log of `count` entries for the search alone."""

    def __init__(self, count):
        self.count = count
        self.reads = 0

    def get(self, key):
        self.reads += 1
        if int(key.rsplit("-", 1)[1].split(".")[0]) >= self.count:
            raise KeyError(key)
        return b"{}"


def test_physical_head_search_bound_every_length():
    probes = {}
    for count in range(ph.MAX_LOG_ENTRIES + 1):
        stub = Stub(count)
        assert ph._Log(stub, TENANT, DRAWING).last() == count - 1
        probes[stub.reads] = probes.get(stub.reads, 0) + 1
    assert probes == MEASURED_PROBES


MEASURED_PROBES = {12: 4095, 13: 2}


def test_physical_head_log_full(backend, monkeypatch):
    monkeypatch.setattr(ph, "MAX_LOG_ENTRIES", 3)
    parent = None
    for n in range(3):
        parent = publish(backend, parent, 2 + n)["head"]["state"]["artifact_id"]
    current = head(backend)
    before = backend.drawing_object_keys(TENANT, DRAWING)
    refused("PHYSICAL_HEAD_LOG_FULL", publish, backend, parent, 9)
    assert backend.drawing_object_keys(TENANT, DRAWING) == before and head(backend) == current
    assert publish(backend, current["parent"], 4) == {"schema": ph.PUBLISH_SCHEMA, "created": False,
                                                      "head": current}


# ------------------------------------------------------ drawing versions --

def test_physical_head_is_per_drawing_not_per_version(backend):
    first = publish(backend)
    with held(backend) as fence:
        commit(backend, fence=fence)
    assert head(backend) == first["head"] and first["head"]["state"]["source_version"] == 1
    second = publish(backend, first["head"]["state"]["artifact_id"], 3)
    assert second["head"]["index"] == 1 and second["head"]["state"]["source_version"] == 2
    assert store.undo(backend, TENANT, DRAWING) == 1
    assert store.load_manifest(backend, TENANT, DRAWING)["head"] == 1
    assert head(backend) == second["head"]


def test_physical_head_retry_after_a_drawing_commit(backend):
    first = publish(backend)
    with held(backend) as fence:
        commit(backend, fence=fence)
    before = backend.drawing_object_keys(TENANT, DRAWING)
    assert publish(backend) == dict(first, created=False)
    assert backend.drawing_object_keys(TENANT, DRAWING) == before


# ------------------------------------------------------------ crash window --

class FailLogWrite:
    def __init__(self, inner):
        self.inner = inner
        self.fail = True

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def put_if_absent_or_verify(self, key, data):
        if "/physical/" in key and self.fail:
            self.fail = False
            raise OSError("disk went away")
        return self.inner.put_if_absent_or_verify(key, data)


def test_physical_head_crash_between_state_and_entry(backend):
    flaky = FailLogWrite(backend)
    refused("PHYSICAL_HEAD_STORE_UNAVAILABLE", ph.publish_physical_state, flaky, TENANT, DRAWING, doc())
    orphan = written(backend)
    assert len(orphan) == 2 and log_keys(backend) == set() and head(backend) is None
    retried = ph.publish_physical_state(flaky, TENANT, DRAWING, doc())
    assert retried["created"] is True and retried["head"]["index"] == 0
    assert written(backend) == orphan
    assert PREFIX + "artifacts/" + retried["head"]["state"]["artifact_id"] + ".json" in orphan


# ------------------------------------------------------------ fail closed --

def _plant(backend, slot, **change):
    first_ref = head(backend)["state"]
    entry = {"schema": ph.ENTRY_SCHEMA, "index": slot, "project_id": PROJECT, "parent": None,
             "state": first_ref["artifact_id"], "content_sha256": first_ref["content_sha256"]}
    entry.update(change)
    backend.put(ph.entry_key(TENANT, DRAWING, slot), json.dumps(entry, sort_keys=True,
                                                                  separators=(",", ":")).encode())


@pytest.mark.parametrize("case", ["noncanonical", "index", "extra-key", "schema", "project", "parent-first",
                                  "state-hex", "content-hex", "bad-json", "empty", "too-large", "link",
                                  "missing-artifact", "foreign-artifact", "content-mismatch", "blob"])
def test_physical_head_corrupt(backend, case):
    first = publish(backend)
    ref = first["head"]["state"]
    key0 = ph.entry_key(TENANT, DRAWING, 0)
    raw = backend.get(key0)
    if case == "noncanonical":
        backend.put(key0, raw.replace(b",", b", ", 1))
    elif case == "index":
        _plant(backend, 0, index=1)
    elif case == "extra-key":
        _plant(backend, 0, note="x")
    elif case == "schema":
        _plant(backend, 0, schema="leaf.solar-physical-head-entry.v2")
    elif case == "project":
        _plant(backend, 0, project_id="")
    elif case == "parent-first":
        _plant(backend, 0, parent=ref["artifact_id"])
    elif case == "state-hex":
        _plant(backend, 0, state="E" * 64)
    elif case == "content-hex":
        _plant(backend, 0, content_sha256=None)
    elif case == "bad-json":
        backend.put(key0, b"{")
    elif case == "empty":
        backend.put(key0, b"")
    elif case == "too-large":
        backend.put(key0, raw + b" " * 1024)
    elif case == "link":
        publish(backend, ref["artifact_id"], 3)
        _plant(backend, 1, parent="0" * 64)
    elif case == "missing-artifact":
        _plant(backend, 0, state="0" * 64)
    elif case == "foreign-artifact":
        _plant(backend, 0, state=_foreign_artifact(backend))
    elif case == "content-mismatch":
        _plant(backend, 0, content_sha256="1" * 64)
    elif case == "blob":
        backend.put(PREFIX + "artifacts/blobs/" + ref["content_sha256"] + ".bin", b"{}")
    refused("PHYSICAL_HEAD_CORRUPT", head, backend)
    refused("PHYSICAL_HEAD_CORRUPT", ph.load_physical_head, backend, TENANT, DRAWING, project_id=PROJECT)
    before = backend.drawing_object_keys(TENANT, DRAWING)
    refused("PHYSICAL_HEAD_CORRUPT", publish, backend, ref["artifact_id"], 9)
    assert backend.drawing_object_keys(TENANT, DRAWING) == before


def test_physical_head_undecodable_state_is_corrupt(backend):
    data = json.dumps(document({}, GENERATE_SHA, "frame-generate")).encode("utf-8")
    artifact_id = _foreign_artifact(backend, ps.TOOL, ps.MEDIA_TYPE, ps.FILENAME, data)
    backend.put(ph.entry_key(TENANT, DRAWING, 0), ph.entry_bytes(0, PROJECT, None, artifact_id,
                                                                 hashlib.sha256(data).hexdigest()))
    assert head(backend)["state"]["artifact_id"] == artifact_id
    refused("PHYSICAL_HEAD_CORRUPT", ph.load_physical_head, backend, TENANT, DRAWING, project_id=PROJECT)


def test_physical_head_project(backend):
    publish(backend)
    refused("PHYSICAL_HEAD_PROJECT_MISMATCH", head, backend, "leaf:project:other")
    refused("PHYSICAL_HEAD_PROJECT_MISMATCH", ph.load_physical_head, backend, TENANT, DRAWING,
            project_id="leaf:project:other")
    before = backend.drawing_object_keys(TENANT, DRAWING)
    refused("PHYSICAL_HEAD_PROJECT_MISMATCH", publish, backend, None, 3, project_id="leaf:project:other")
    assert backend.drawing_object_keys(TENANT, DRAWING) == before
    for bad in ("p" * 101, "", None, 7):
        refused("PHYSICAL_HEAD_PROJECT_ID_INVALID", head, backend, bad)
    for bad in ("p" * 101, "", 7):
        refused("PHYSICAL_HEAD_PROJECT_ID_INVALID", publish, backend, None, 3, project_id=bad)


def test_physical_head_other_tenant_and_drawing_see_nothing(backend):
    publish(backend)
    assert ph.physical_head(backend, "other-tenant", DRAWING, project_id=PROJECT) is None
    assert ph.physical_head(backend, TENANT, "other", project_id=PROJECT) is None
    refused("PHYSICAL_HEAD_ID_INVALID", ph.physical_head, backend, "Other Tenant", DRAWING, project_id=PROJECT)


def test_physical_head_publish_context_refusals(backend, tmp_path, monkeypatch):
    refused("PHYSICAL_STATE_DRAWING_NOT_FOUND", ph.publish_physical_state, backend, TENANT, "nosuch", doc())
    (tmp_path / "graphless").mkdir()
    graphless, _ = seed_graphless(tmp_path / "graphless", monkeypatch)
    refused("PHYSICAL_STATE_GRAPH_REQUIRED", ph.publish_physical_state, graphless, TENANT, DRAWING, doc())
    assert log_keys(graphless) == set()


def test_physical_head_invalid_document_reads_and_writes_nothing(backend):
    counting = Counting(backend)
    bad = doc()
    bad["state"] = {"layers": []}
    before = backend.drawing_object_keys(TENANT, DRAWING)
    refused("PHYSICAL_STATE_KEY_UNKNOWN", ph.publish_physical_state, counting, TENANT, DRAWING, bad)
    assert counting.log_reads == [] and backend.drawing_object_keys(TENANT, DRAWING) == before


def test_physical_head_drained(backend, monkeypatch):
    first = publish(backend)
    monkeypatch.setenv("LEAF_DRAWING_MUTATIONS_ENABLED", "0")
    before = backend.drawing_object_keys(TENANT, DRAWING)
    refused("PHYSICAL_HEAD_WRITES_DRAINED", publish, backend, first["head"]["state"]["artifact_id"], 3)
    assert backend.drawing_object_keys(TENANT, DRAWING) == before
    assert head(backend) == first["head"]


def test_physical_head_store_unavailable(backend):
    publish(backend)

    class Unreadable:
        def __getattr__(self, name):
            return getattr(backend, name)

        def get(self, key):
            if "/physical/" in key:
                raise OSError("unavailable")
            return backend.get(key)

    refused("PHYSICAL_HEAD_STORE_UNAVAILABLE", ph.physical_head, Unreadable(), TENANT, DRAWING, project_id=PROJECT)

    class UnreadableArtifact:
        def __getattr__(self, name):
            return getattr(backend, name)

        def get(self, key):
            if "/artifacts/" in key:
                raise OSError("unavailable")
            return backend.get(key)

    refused("PHYSICAL_HEAD_STORE_UNAVAILABLE", ph.physical_head, UnreadableArtifact(), TENANT, DRAWING,
            project_id=PROJECT)


class Naive(store.StorageBackend):
    """The base class's exists-then-put publication over a real filesystem store."""

    def __init__(self, inner):
        self.inner = inner

    def get(self, key):
        return self.inner.get(key)

    def put(self, key, data):
        self.inner.put(key, data)

    def exists(self, key):
        return self.inner.exists(key)


def test_physical_head_refuses_a_non_atomic_store(backend):
    assert store.OSSBackend.put_if_absent_or_verify is store.StorageBackend.put_if_absent_or_verify
    assert store.FilesystemBackend.put_if_absent_or_verify is not store.StorageBackend.put_if_absent_or_verify
    assert store.InMemoryBackend.put_if_absent_or_verify is not store.StorageBackend.put_if_absent_or_verify
    for unsafe in (store.OSSBackend(), Naive(backend)):
        refused("PHYSICAL_HEAD_STORE_UNSAFE", ph.publish_physical_state, unsafe, TENANT, DRAWING, doc())
    assert written(backend) == set() and log_keys(backend) == set()
    first = publish(backend)
    assert ph.physical_head(Naive(backend), TENANT, DRAWING, project_id=PROJECT) == first["head"]


def test_physical_head_in_memory_store():
    memory = store.InMemoryBackend()
    key = ph.entry_key(TENANT, DRAWING, 0)
    memory.put_if_absent_or_verify(key, b"a")
    memory.put_if_absent_or_verify(key, b"a")
    with pytest.raises(store.ImmutableConflict):
        memory.put_if_absent_or_verify(key, b"b")
    assert memory.get(key) == b"a"


# ------------------------------------------------------------- live OSS reads --

def _response(status):
    response = requests.Response()
    response.status_code = status
    return response


def _objects(backend):
    """Every head-log and artifact object of the drawing, copied out as bytes."""
    return {key: backend.get(key) for key in backend.drawing_object_keys(TENANT, DRAWING)
            if "/physical/" in key or "/artifacts/" in key}


def _memory(objects):
    memory = store.InMemoryBackend()
    for key, data in objects.items():
        memory.put(key, data)
    return memory


def _oss(monkeypatch, objects, fail=None):
    """The real OSSBackend over a dict: a missing key raises the 404 HTTPError the live download
    raises; `fail(key)` may raise first."""
    def download_object(key):
        if fail is not None:
            fail(key)
        if key not in objects:
            raise requests.HTTPError(response=_response(404))
        return objects[key]

    monkeypatch.setattr(store.client, "download_object", download_object)
    return store.OSSBackend()


def test_physical_head_oss_empty_log(monkeypatch):
    oss, memory = _oss(monkeypatch, {}), store.InMemoryBackend()
    assert ph.physical_head(memory, TENANT, DRAWING, project_id=PROJECT) is None
    assert ph.physical_head(oss, TENANT, DRAWING, project_id=PROJECT) is None
    assert ph.load_physical_head(oss, TENANT, DRAWING, project_id=PROJECT) == (None, None)


def test_physical_head_oss_log_of_entry_zero(backend, monkeypatch):
    first = publish(backend)
    objects = _objects(backend)
    memory, oss = _memory(objects), _oss(monkeypatch, objects)
    assert ph.physical_head(memory, TENANT, DRAWING, project_id=PROJECT) == first["head"]
    assert ph.physical_head(oss, TENANT, DRAWING, project_id=PROJECT) == first["head"]
    assert (ph.load_physical_head(oss, TENANT, DRAWING, project_id=PROJECT)
            == ph.load_physical_head(memory, TENANT, DRAWING, project_id=PROJECT))


def test_physical_head_oss_other_status_is_unavailable(backend, monkeypatch):
    publish(backend)

    def fail(key):
        if key == ph.entry_key(TENANT, DRAWING, 2047):
            raise requests.HTTPError(response=_response(500))

    oss = _oss(monkeypatch, _objects(backend), fail)
    refused("PHYSICAL_HEAD_STORE_UNAVAILABLE", ph.physical_head, oss, TENANT, DRAWING, project_id=PROJECT)


def test_physical_head_oss_error_without_response_is_unavailable(backend, monkeypatch):
    publish(backend)

    def fail(key):
        raise OSError("boom")

    oss = _oss(monkeypatch, _objects(backend), fail)
    refused("PHYSICAL_HEAD_STORE_UNAVAILABLE", ph.physical_head, oss, TENANT, DRAWING, project_id=PROJECT)


def _binding_ref(view):
    return {"head_index": view["index"], "state_artifact_id": view["state"]["artifact_id"],
            "state_content_sha256": view["state"]["content_sha256"]}


def _binding_request(backend, graph, apply_id="binding-graph", physical=None):
    v, _, row = store.resolve_version_entry(backend, TENANT, DRAWING)
    previous = None
    if (row.get("note") or "").startswith("solar-bundle:"):
        previous = store.read_graph_bundle(backend, TENANT, DRAWING)["graph"]["rev"]
    value = copy.deepcopy(graph)
    value.update(rev=0 if previous is None else previous + 1, parent_rev=previous,
                 source_hash=hashlib.sha256(b"binding DWG").hexdigest())
    request = {"graph": value, "intake": {"layers": []}, "mapping": {},
               "project_id": PROJECT, "apply_id": apply_id, "source_version": v,
               "source_sha256": row["sha256"], "source_rev": previous}
    if physical is not None:
        request["physical"] = physical
    return request


def _binding_bundle(backend, graph, fence, apply_id="binding-graph", physical=None):
    request = _binding_request(backend, graph, apply_id, physical)
    return write_loop.apply_graph_version(backend, TENANT, DRAWING, b"binding DWG", request,
                                          holder="fixture-owner", fence=fence)


def _binding_commit(backend, checkout_fence, ref, parent=1, apply_id="binding", **kw):
    args = dict(project_id=PROJECT, parent_version=parent, physical=ref,
                holder="fixture-owner", fence=checkout_fence, apply_id=apply_id)
    args.update(kw)
    return write_loop.commit_physical_version(backend, TENANT, DRAWING, **args)


def _binding_payload(backend, payload, parent=1, fence=1):
    from leaf_cloud_client import canonical_bytes
    data = payload if type(payload) is bytes else canonical_bytes(payload)
    return write_loop._put_bytes_version(backend, TENANT, DRAWING, data, parent,
                                         {"tool": "binding-fixture"},
                                         holder="fixture-owner", fence=fence)


def _binding_intake(backend, version=1):
    return json.loads(backend.get(store.drawing_version_key(TENANT, DRAWING, version)))


def _binding_plant_bundle(backend, version, value):
    raw = value if type(value) is bytes else store._bundle_json(value)
    digest = hashlib.sha256(raw).hexdigest()
    backend.put(store._graph_bundle_key(TENANT, DRAWING, digest), raw)
    manifest = store.load_manifest(backend, TENANT, DRAWING)
    next(e for e in manifest["versions"] if e["v"] == version)["note"] = "solar-bundle:" + digest
    store.save_manifest(backend, TENANT, DRAWING, manifest)
    store._physical_memo_clear()


class TestVersionBinding:
    @pytest.fixture(autouse=True)
    def version_binding_enabled(self, monkeypatch):
        monkeypatch.setenv("LEAF_SOLAR_PHYSICAL_VERSIONED", "1")
        store._physical_memo_clear()
        yield
        store._physical_memo_clear()

    def test_reference_contract(self):
        assert store.PHYSICAL_REF_KEYS == {"head_index", "state_artifact_id", "state_content_sha256"}
        assert store.PHYSICAL_MAX_INDEX == ph.MAX_LOG_ENTRIES - 1 == 4095
        assert store.PHYSICAL_INTAKE_KEY == "solar_physical"
        assert store.PHYSICAL_BUNDLE_KEY == "physical"
        assert store.PHYSICAL_INTAKE_MAX_BYTES == write_loop.MAX_OUTPUT_INTAKE_BYTES == 64 * 1024 * 1024
        assert store.PHYSICAL_MEMO_MAX == 1024
        ref = {"head_index": 0, "state_artifact_id": "a" * 64, "state_content_sha256": "b" * 64}
        assert store.check_physical_reference(ref) == ref
        assert store.check_physical_reference(ref) is not ref
        bad = [None, [], dict(ref, extra=1), {k: v for k, v in ref.items() if k != "head_index"}]
        bad += [dict(ref, head_index=i) for i in (True, -1, 4096, 0.0)]
        bad += [dict(ref, state_artifact_id=s) for s in ("A" * 64, "a" * 63)]
        for value in bad:
            with pytest.raises(store.PhysicalReferenceInvalid, match="^invalid physical reference$"):
                store.check_physical_reference(value)

    def test_unbound_bundle_commit_is_byte_identical(self, backend, graph):
        with held(backend) as fence:
            request = _binding_request(backend, graph)
            v = _binding_bundle(backend, graph, fence)
        bundle = store.read_graph_bundle(backend, TENANT, DRAWING, v)
        bundle.pop("version")
        assert set(bundle) == {"graph", "intake", "mapping", "receipt"}
        expected = {k: request[k] for k in ("graph", "intake", "mapping")}
        receipt = {k: request[k] for k in
                   ("project_id", "apply_id", "source_version", "source_sha256", "source_rev")}
        receipt.update(tenant_id=TENANT, drawing_id=DRAWING,
                       hashes={k: hashlib.sha256(store._bundle_json(v)).hexdigest()
                               for k, v in expected.items()})
        receipt["hashes"]["dwg"] = hashlib.sha256(b"binding DWG").hexdigest()
        expected["receipt"] = receipt
        golden = store._bundle_json(expected)
        _, _, row = store.resolve_version_entry(backend, TENANT, DRAWING, v)
        assert row["note"] == "solar-bundle:" + hashlib.sha256(golden).hexdigest()
        assert store._bundle_json(bundle) == golden

    def test_bundle_commit_with_reference_and_inheritance(self, backend, graph):
        ref = _binding_ref(publish(backend)["head"])
        with held(backend) as fence:
            first = _binding_bundle(backend, graph, fence, physical=ref)
            second = _binding_bundle(backend, graph, fence, "inherit")
        for v in (first, second):
            assert store.read_version_physical(backend, TENANT, DRAWING, v) == (v, ref)

    def test_invalid_reference_refused_head_unchanged(self, backend, graph):
        with held(backend) as fence:
            before = store.load_manifest(backend, TENANT, DRAWING)
            with pytest.raises(store.PhysicalReferenceInvalid):
                _binding_bundle(backend, graph, fence, physical={"head_index": True})
            assert store.load_manifest(backend, TENANT, DRAWING) == before

    @pytest.mark.parametrize("bound", [False, True])
    def test_restore_copies_reference_and_refuses_mismatch(self, backend, graph, bound):
        ref = _binding_ref(publish(backend)["head"])
        with held(backend) as fence:
            source = _binding_bundle(backend, graph, fence, physical=ref if bound else None)
            _binding_bundle(backend, graph, fence, "later", physical=ref)
            restored = store.read_graph_bundle(backend, TENANT, DRAWING, source)
            request = _binding_request(backend, graph, "restore")
            request.update({k: restored[k] for k in ("graph", "intake", "mapping")})
            request["restore_version"] = source
            if bound:
                request["physical"] = ref
            bad = dict(request, physical=dict(ref, head_index=5))
            with pytest.raises(ValueError, match="restore must copy"):
                write_loop.apply_graph_version(backend, TENANT, DRAWING, b"binding DWG", bad,
                                                holder="fixture-owner", fence=fence)
            v = write_loop.apply_graph_version(backend, TENANT, DRAWING, b"binding DWG", request,
                                                holder="fixture-owner", fence=fence)
        assert store.read_version_physical(backend, TENANT, DRAWING, v)[1] == (ref if bound else None)

    def test_restore_of_unbound_version_drops_reference(self, backend, graph):
        from routers.drawings import restore_drawing_version
        ref = _binding_ref(publish(backend)["head"])
        with held(backend) as fence:
            unbound = _binding_bundle(backend, graph, fence)
            _binding_bundle(backend, graph, fence, "bound", physical=ref)
            restore_drawing_version(TENANT, DRAWING, unbound, actor="restore", backend=backend,
                                    holder="fixture-owner", fence=fence)
        assert store.read_version_physical(backend, TENANT, DRAWING)[1] is None

    @pytest.mark.parametrize("bundle", [False, True])
    def test_undo_redo_select_reference(self, backend, graph, bundle):
        first = publish(backend)["head"]
        with held(backend) as fence:
            parent = _binding_bundle(backend, graph, fence) if bundle else 1
            v, _ = _binding_commit(backend, fence, _binding_ref(first), parent)
            second = publish(backend, first["state"]["artifact_id"], 3)["head"]
            w, _ = _binding_commit(backend, fence, _binding_ref(second), v, "second")
            assert store.undo(backend, TENANT, DRAWING, holder="fixture-owner", fence=fence) == v
            assert head(backend) == first
            assert store.redo(backend, TENANT, DRAWING, holder="fixture-owner", fence=fence) == w
            assert head(backend) == second

    def test_corrupt_bundle_reference_fails_closed(self, backend, graph):
        ref = _binding_ref(publish(backend)["head"])
        with held(backend) as fence:
            v = _binding_bundle(backend, graph, fence, physical=ref)
            _binding_bundle(backend, graph, fence, "later")
            bundle = store.read_graph_bundle(backend, TENANT, DRAWING, v)
            bundle["physical"]["head_index"] = True
            _binding_plant_bundle(backend, v, bundle)
            with pytest.raises(store.PhysicalReferenceInvalid):
                store.read_graph_bundle(backend, TENANT, DRAWING, v)
            with pytest.raises(store.PhysicalReferenceInvalid):
                store.undo(backend, TENANT, DRAWING, holder="fixture-owner", fence=fence)
        refused("PHYSICAL_HEAD_CORRUPT", ph.load_physical_head, backend, TENANT, DRAWING,
                version=v, project_id=PROJECT)

    def test_intake_reference_read_and_substring_false_positive(self, backend):
        ref = _binding_ref(publish(backend)["head"])
        with held(backend) as fence:
            v = _binding_payload(backend, dict(_binding_intake(backend), solar_physical=ref), fence=fence)
            assert store.read_version_physical(backend, TENANT, DRAWING, v) == (v, ref)
            for payload in ({"value": "solar_physical"}, b'not JSON "solar_physical"',
                            {"solar_physical": ref}):
                w = _binding_payload(backend, payload, parent=v, fence=fence)
                assert store.read_version_physical(backend, TENANT, DRAWING, w) == (w, None)

    def test_intake_graph_commit_inherits_and_replays(self, backend):
        ref = _binding_ref(publish(backend)["head"])
        with held(backend) as fence:
            v, _ = _binding_commit(backend, fence, ref)
            result = commit(backend, source_version=v, fence=fence)
            again = commit(backend, source_version=v, fence=fence)
        assert again["replayed"] is True
        assert store.read_version_physical(backend, TENANT, DRAWING, result["new_version"]["version"])[1] == ref

    def test_plain_dwg_version_unbound_and_commit_refused(self, backend):
        ref = _binding_ref(publish(backend)["head"])
        with held(backend) as fence:
            v = _binding_payload(backend, b"plain DWG", fence=fence)
            assert store.read_version_physical(backend, TENANT, DRAWING, v) == (v, None)
            with pytest.raises(ValueError, match="physical commit requires a graph version"):
                _binding_commit(backend, fence, ref, v)

    @pytest.mark.parametrize("bundle", [False, True])
    def test_commit_physical_version_bundle_and_intake(self, backend, graph, bundle):
        fixed = {"head_index": 0, "state_artifact_id": "0" * 64, "state_content_sha256": "1" * 64}
        assert store.physical_ref_note(fixed) == (
            "solar-physical:41e5754788f718edd0c83dd0cb372258f0a971d0af8c9f187b813f66932abe19")
        ref = _binding_ref(publish(backend)["head"])
        with held(backend) as fence:
            parent = _binding_bundle(backend, graph, fence) if bundle else 1
            original = backend.get(store.drawing_version_key(TENANT, DRAWING, parent))
            v, created = _binding_commit(backend, fence, ref, parent)
            assert created and v > parent
            assert _binding_commit(backend, fence, ref, parent) == (v, False)
            before = store.load_manifest(backend, TENANT, DRAWING)
            assert _binding_commit(backend, fence, ref, v, "noop") == (v, False)
            assert store.load_manifest(backend, TENANT, DRAWING) == before
        new = backend.get(store.drawing_version_key(TENANT, DRAWING, v))
        if bundle:
            assert new == original
            old_bundle = store.read_graph_bundle(backend, TENANT, DRAWING, parent)
            new_bundle = store.read_graph_bundle(backend, TENANT, DRAWING, v)
            assert all(old_bundle[k] == new_bundle[k] for k in ("graph", "intake", "mapping"))
            assert new_bundle["receipt"]["physical_only"] is True
        else:
            assert json.loads(new) == dict(json.loads(original), solar_physical=ref)
            assert before["versions"][-1]["note"] == store.physical_ref_note(ref)

    @pytest.mark.parametrize("bundle", [False, True])
    def test_commit_physical_version_refusals(self, backend, graph, monkeypatch, bundle):
        first = publish(backend)["head"]
        ref = _binding_ref(first)
        with held(backend) as fence:
            parent = _binding_bundle(backend, graph, fence) if bundle else 1
            bad_inputs = [{"holder": None}, {"holder": store.ANONYMOUS_HOLDER},
                          {"holder": "another"}, {"fence": 0}, {"fence": True},
                          {"apply_id": "bad/id"}, {"parent_version": True},
                          {"physical": dict(ref, head_index=99)},
                          {"physical": dict(ref, state_artifact_id="0" * 64)},
                          {"physical": dict(ref, state_content_sha256="0" * 64)},
                          {"project_id": "foreign"}]
            for overrides in bad_inputs:
                before = store.load_manifest(backend, TENANT, DRAWING)
                expected_error = store.CheckoutDenied if {"holder", "fence"} & overrides.keys() else ValueError
                with pytest.raises(expected_error):
                    _binding_commit(backend, fence, ref, parent, **overrides)
                assert store.load_manifest(backend, TENANT, DRAWING) == before
            before = store.load_manifest(backend, TENANT, DRAWING)
            with monkeypatch.context() as patch:
                patch.setenv("LEAF_DRAWING_MUTATIONS_ENABLED", "0")
                with pytest.raises(ValueError, match="drawing mutations are disabled"):
                    _binding_commit(backend, fence, ref, parent)
            assert store.load_manifest(backend, TENANT, DRAWING) == before
            v, _ = _binding_commit(backend, fence, ref, parent)
            second = _binding_ref(publish(backend, first["state"]["artifact_id"], 3)["head"])
            before = store.load_manifest(backend, TENANT, DRAWING)
            with pytest.raises(ValueError, match="apply id was reused"):
                _binding_commit(backend, fence, second, parent)
            with pytest.raises(ValueError, match="stale (parent|drawing head)"):
                _binding_commit(backend, fence, second, parent, "stale")
            assert store.load_manifest(backend, TENANT, DRAWING) == before
            store.undo(backend, TENANT, DRAWING, holder="fixture-owner", fence=fence)
            with pytest.raises(ValueError, match="apply id was reused"):
                _binding_commit(backend, fence, ref, v)
            expired = store.load_manifest(backend, TENANT, DRAWING)
            expired["checkout"]["expires"] = "2000-01-01T00:00:00+00:00"
            store.save_manifest(backend, TENANT, DRAWING, expired)
            with pytest.raises(store.CheckoutDenied):
                _binding_commit(backend, fence, ref, parent)
            assert store.load_manifest(backend, TENANT, DRAWING) == expired

    def test_bound_read_returns_bound_entry_not_tip(self, backend):
        first = publish(backend)["head"]
        with held(backend) as fence:
            v, _ = _binding_commit(backend, fence, _binding_ref(first))
        second = publish(backend, first["state"]["artifact_id"], 3)["head"]
        publish(backend, second["state"]["artifact_id"], 4)
        view, document = ph.load_physical_head(backend, TENANT, DRAWING, project_id=PROJECT, version=v)
        assert view == first and document == doc()

    def test_bound_view_equals_tip_view_when_same_entry(self, backend):
        first = publish(backend)["head"]
        with held(backend) as fence:
            _binding_commit(backend, fence, _binding_ref(first))
        assert head(backend) == ph._head(backend, TENANT, DRAWING, PROJECT)[0]

    def test_default_off_preserves_tip_reads_and_refuses_commits(self, backend, monkeypatch):
        first = publish(backend)["head"]
        with held(backend) as fence:
            v, _ = _binding_commit(backend, fence, _binding_ref(first))
            second = publish(backend, first["state"]["artifact_id"], 3)["head"]
            monkeypatch.delenv("LEAF_SOLAR_PHYSICAL_VERSIONED")
            assert ph.load_physical_head(backend, TENANT, DRAWING,
                                         project_id=PROJECT, version=v)[0] == second
            with pytest.raises(ValueError, match="physical version binding is disabled"):
                _binding_commit(backend, fence, _binding_ref(second), v, "disabled")

    def test_graph_apply_id_cannot_replay_as_physical_commit(self, backend, graph):
        ref = _binding_ref(publish(backend)["head"])
        with held(backend) as fence:
            parent = _binding_bundle(backend, graph, fence, "parent")
            _binding_bundle(backend, graph, fence, "shared", physical=ref)
            before = store.load_manifest(backend, TENANT, DRAWING)
            with pytest.raises(ValueError, match="apply id was reused with different content"):
                _binding_commit(backend, fence, ref, parent, "shared")
            assert store.load_manifest(backend, TENANT, DRAWING) == before

    @pytest.mark.parametrize("bundle", [False, True])
    def test_replay_requires_active_matching_checkout(self, backend, graph, monkeypatch, bundle):
        ref = _binding_ref(publish(backend)["head"])
        with held(backend) as fence:
            parent = _binding_bundle(backend, graph, fence) if bundle else 1
            _binding_commit(backend, fence, ref, parent)
            expired = store.load_manifest(backend, TENANT, DRAWING)
            expired["checkout"]["expires"] = "2000-01-01T00:00:00+00:00"
            store.save_manifest(backend, TENANT, DRAWING, expired)

            def no_reference_read(*args, **kwargs):
                pytest.fail("unauthorized replay read a physical reference")

            monkeypatch.setattr(store, "read_version_physical", no_reference_read)
            with pytest.raises(store.CheckoutDenied):
                _binding_commit(backend, fence, ref, parent)
            assert store.load_manifest(backend, TENANT, DRAWING) == expired

    @pytest.mark.parametrize("project", [None, [], "foreign", {}, {"id": "foreign"}])
    def test_intake_parent_project_refusal(self, backend, project):
        ref = _binding_ref(publish(backend)["head"])
        with held(backend) as fence:
            payload = _binding_intake(backend)
            payload["solar_design_graph"]["project"] = project
            parent = _binding_payload(backend, payload, fence=fence)
            before = store.load_manifest(backend, TENANT, DRAWING)
            with pytest.raises(store.PhysicalReferenceScopeMismatch):
                _binding_commit(backend, fence, ref, parent)
            assert store.load_manifest(backend, TENANT, DRAWING) == before

    def test_raced_replay_rechecks_checkout(self, backend, monkeypatch):
        ref = _binding_ref(publish(backend)["head"])
        with held(backend) as fence:
            real_put = write_loop._put_bytes_version
            raced_manifest = []

            def race(*args, **kwargs):
                real_put(*args, **kwargs)
                expired = store.load_manifest(backend, TENANT, DRAWING)
                expired["checkout"]["expires"] = "2000-01-01T00:00:00+00:00"
                store.save_manifest(backend, TENANT, DRAWING, expired)
                raced_manifest.append(expired)
                raise ValueError("stale drawing head: expected 1")

            monkeypatch.setattr(write_loop, "_put_bytes_version", race)
            with pytest.raises(store.CheckoutDenied):
                _binding_commit(backend, fence, ref)
            assert store.load_manifest(backend, TENANT, DRAWING) == raced_manifest[0]

    @pytest.mark.parametrize("version", [True, False, 0, -1, "1", "latest", None, 1.0])
    def test_invalid_version_is_refused(self, backend, version):
        with pytest.raises(ValueError, match="invalid version"):
            store.read_version_physical(backend, TENANT, DRAWING, version)
        refused("PHYSICAL_HEAD_ID_INVALID", ph.load_physical_head, backend, TENANT, DRAWING,
                project_id=PROJECT, version=version)

    def test_memo_returns_fresh_references(self, backend):
        ref = _binding_ref(publish(backend)["head"])
        with held(backend) as fence:
            v, _ = _binding_commit(backend, fence, ref)
        _, got = store.read_version_physical(backend, TENANT, DRAWING, v)
        got["head_index"] = 99
        assert store.read_version_physical(backend, TENANT, DRAWING, v)[1] == ref

    @pytest.mark.parametrize("error,code", [
        (store.PhysicalReferenceScopeMismatch(), "PHYSICAL_HEAD_PROJECT_MISMATCH"),
        (store.PhysicalReferenceInvalid(), "PHYSICAL_HEAD_CORRUPT"),
        (KeyError("bound object"), "PHYSICAL_HEAD_CORRUPT"),
        (ValueError("version not in manifest"), "PHYSICAL_HEAD_ID_INVALID"),
        (OSError("store fault"), "PHYSICAL_HEAD_STORE_UNAVAILABLE"),
        (RuntimeError("store fault"), "PHYSICAL_HEAD_STORE_UNAVAILABLE"),
    ])
    def test_reference_read_error_mapping(self, backend, monkeypatch, error, code):
        def fail(*args, **kwargs):
            raise error
        monkeypatch.setattr(store, "read_version_physical", fail)
        refused(code, ph.load_physical_head, backend, TENANT, DRAWING, project_id=PROJECT)

    @pytest.mark.parametrize("defect", ["head", "versions", "bytes", "sha256", "note"])
    def test_malformed_manifest_is_not_unbound(self, backend, defect):
        manifest = store.load_manifest(backend, TENANT, DRAWING)
        if defect in {"head", "versions"}:
            manifest[defect] = None
        else:
            del manifest["versions"][0][defect]
        store.save_manifest(backend, TENANT, DRAWING, manifest)
        refused("PHYSICAL_HEAD_CORRUPT", ph.load_physical_head, backend, TENANT, DRAWING,
                project_id=PROJECT)

    def test_unbound_reads_unchanged(self, backend, monkeypatch):
        for populated in (False, True):
            if populated:
                publish(backend)
            expected, content = ph._head(backend, TENANT, DRAWING, PROJECT)
            view, document = ph.load_physical_head(backend, TENANT, DRAWING, project_id=PROJECT)
            assert view == expected
            assert document == (None if content is None else ps.decode_document(content))
            # OSS has only log/artifacts and answers 404 for the manifest.
            oss = _oss(monkeypatch, _objects(backend))
            assert ph.load_physical_head(oss, TENANT, DRAWING, project_id=PROJECT) == (view, document)
        assert ph.load_physical_head(store.InMemoryBackend(), TENANT, DRAWING,
                                     project_id=PROJECT) == (None, None)

    @pytest.mark.parametrize("field,value", [("head_index", 9), ("state_artifact_id", "0" * 64),
                                            ("state_content_sha256", "0" * 64)])
    def test_reference_entry_mismatch_fails_closed(self, backend, field, value):
        ref = dict(_binding_ref(publish(backend)["head"]), **{field: value})
        with held(backend) as fence:
            v = _binding_payload(backend, dict(_binding_intake(backend), solar_physical=ref), fence=fence)
        refused("PHYSICAL_HEAD_CORRUPT", ph.load_physical_head, backend, TENANT, DRAWING,
                project_id=PROJECT, version=v)

    def test_project_and_tenant_isolation(self, backend):
        ref = _binding_ref(publish(backend)["head"])
        with held(backend) as fence:
            v, _ = _binding_commit(backend, fence, ref)
        for warmed in (False, True):
            store._physical_memo_clear()
            if warmed:
                assert head(backend) is not None
            refused("PHYSICAL_HEAD_PROJECT_MISMATCH", ph.load_physical_head, backend, TENANT, DRAWING,
                    project_id="foreign", version=v)
        class Recording:
            def __init__(self):
                self.keys = []
            def get(self, key):
                self.keys.append(key)
                return backend.get(key)
        recording = Recording()
        assert ph.physical_head(recording, "tenant-b", DRAWING, project_id=PROJECT) is None
        assert recording.keys and all(k.startswith("tenants/tenant-b/") for k in recording.keys)

    def test_bound_read_is_bounded(self, backend, graph):
        first = publish(backend)["head"]
        second = publish(backend, first["state"]["artifact_id"], 3)["head"]
        with held(backend) as fence:
            v = _binding_bundle(backend, graph, fence, physical=_binding_ref(second))
        publish(backend, second["state"]["artifact_id"], 4)
        class Recording(Counting):
            def __init__(self, inner):
                super().__init__(inner)
                self.keys = []
            def get(self, key):
                self.keys.append(key)
                return super().get(key)
        recording = Recording(backend)
        for memo in (False, True):
            recording.keys.clear()
            recording.log_reads.clear()
            assert ph.load_physical_head(recording, TENANT, DRAWING, project_id=PROJECT, version=v)[0] == second
            assert recording.log_reads == [1, 0]
            assert recording.keys.count(store.manifest_key(TENANT, DRAWING)) == 1
            assert sum("/bundles/" in k or "/v/" in k for k in recording.keys) == (0 if memo else 1)
            assert sum("/artifacts/" in k for k in recording.keys) == 2

    def test_local_read_and_proof_follow_source_version(self, backend):
        import solar_local_read as local
        first = publish(backend)["head"]
        with held(backend) as fence:
            v, _ = _binding_commit(backend, fence, _binding_ref(first))
        publish(backend, first["state"]["artifact_id"], 3)
        current = local._current_physical_head(backend, TENANT, DRAWING, PROJECT, version=v)
        assert current["head"] == first
        output = {"head": first}
        assert local._recorded_physical_head(backend, TENANT, DRAWING, PROJECT, output,
                                             version=v) == current
        with pytest.raises(ValueError):
            local._recorded_physical_head(backend, TENANT, DRAWING, PROJECT,
                                          {"head": head(backend, PROJECT) | {"index": 1}}, version=v)
        with pytest.raises(ValueError):
            local._recorded_physical_head(backend, TENANT, DRAWING, PROJECT, {"head": None}, version=v)
        params = {"drawing_id": DRAWING}
        result = local.run_local_graph_read(backend, TENANT, "solar-terrain-read", params,
                                            drawing_id=DRAWING, source_version=v, job_id="binding-read")
        assert result["output"]["head"] == first
        assert local.graph_read_provenance(result, params, TENANT, "binding-read", "solar-terrain-read",
                                           v, backend=backend) is not None
        tampered = copy.deepcopy(result)
        tampered["output"]["head"]["index"] = 1
        with pytest.raises(ValueError, match="graph read terminal proof rejected"):
            local.graph_read_provenance(tampered, params, TENANT, "binding-read", "solar-terrain-read",
                                        v, backend=backend)

    def test_terrain_get_and_civil_reopen_follow_head_version(self, backend, monkeypatch):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from envelopes import install_error_handlers
        from routers import solar_terrain as route
        first = publish(backend)["head"]
        with held(backend) as fence:
            v, _ = _binding_commit(backend, fence, _binding_ref(first))
            second = publish(backend, first["state"]["artifact_id"], 3)["head"]
            _binding_commit(backend, fence, _binding_ref(second), v, "next")
            store.undo(backend, TENANT, DRAWING, holder="fixture-owner", fence=fence)
        monkeypatch.delenv("LEAF_AUTH_LIVE", raising=False)
        monkeypatch.delenv("APS_LIVE", raising=False)
        app = FastAPI()
        install_error_handlers(app)
        app.include_router(route.router)
        with TestClient(app) as client:
            for view in ("", "&view=civil"):
                response = client.get(f"/api/drawings/{DRAWING}/terrain?project_id={PROJECT}" + view,
                                      headers={"X-Tenant-Id": TENANT})
                assert response.status_code == 200, response.text
                assert response.json()["head"] == first

    def test_postgres_commit_physical_version_and_undo(self, backend, graph, tmp_path, monkeypatch, request):
        # Exercise the PostgreSQL stale-prefix retry without requiring a database in
        # this hermetic suite. The registered authority suite owns live DB setup.
        first = publish(backend)["head"]
        ref = _binding_ref(first)
        with held(backend) as fence:
            real_put = write_loop._put_bytes_version
            raced = []
            def race(*args, **kwargs):
                raced.append(real_put(*args, **kwargs))
                raise ValueError("stale drawing head: expected 1")
            with monkeypatch.context() as patch:
                patch.setattr(write_loop, "_put_bytes_version", race)
                assert _binding_commit(backend, fence, ref) == (raced[0], False)
            assert _binding_commit(backend, fence, ref) == (raced[0], False)
            assert store.undo(backend, TENANT, DRAWING, holder="fixture-owner", fence=fence) == 1
        if os.environ.get("DATABASE_URL"):
            # The suite remains hermetic without DATABASE_URL; with one, exercise
            # the real authority using the established migration/pool fixture.
            request.getfixturevalue("postgres_authority")
            import uuid
            tenant, drawing = "binding-" + uuid.uuid4().hex[:12], "physical"
            memory = store.InMemoryBackend()
            source = tmp_path / "pg-binding.json"
            from solar_sizing_client import digest
            source.write_text(json.dumps({"solar_design_graph": graph,
                                          "solar_design_graph_sha256": digest(graph)}))
            store.ingest_drawing(memory, tenant, str(source), drawing_id=drawing)
            fence = store.acquire_checkout_fence(memory, tenant, drawing, "owner", 300)
            entry = ph.publish_physical_state(memory, tenant, drawing, doc(), project_id=PROJECT)["head"]
            args = dict(project_id=PROJECT, parent_version=1, physical=_binding_ref(entry),
                        holder="owner", fence=fence, apply_id="pg-binding")
            v, created = write_loop.commit_physical_version(memory, tenant, drawing, **args)
            assert created
            assert write_loop.commit_physical_version(memory, tenant, drawing, **args) == (v, False)
            assert store.undo(memory, tenant, drawing, holder="owner", fence=fence) == 1
            next_entry = ph.publish_physical_state(memory, tenant, drawing,
                                                   doc(entry["state"]["artifact_id"], 3),
                                                   project_id=PROJECT)["head"]
            args.update(physical=_binding_ref(next_entry), apply_id="pg-race")
            raced.clear()
            with monkeypatch.context() as patch:
                patch.setattr(write_loop, "_put_bytes_version", race)
                assert write_loop.commit_physical_version(memory, tenant, drawing, **args) == (raced[0], False)

    @pytest.mark.parametrize("defect", ["deleted-bundle", "receipt", "tenant", "project", "json",
                                       "deleted-intake", "hash", "intake-project", "oss"])
    def test_missing_or_malformed_bound_record_fails_closed(self, backend, graph, monkeypatch, defect):
        ref = _binding_ref(publish(backend)["head"])
        with held(backend) as fence:
            if defect in {"deleted-intake", "hash", "intake-project"}:
                payload = dict(_binding_intake(backend), solar_physical=ref)
                if defect == "intake-project":
                    del payload["solar_design_graph"]["project"]["id"]
                v = _binding_payload(backend, payload, fence=fence)
                key = store.drawing_version_key(TENANT, DRAWING, v)
            else:
                v = _binding_bundle(backend, graph, fence, physical=ref)
                bundle = store.read_graph_bundle(backend, TENANT, DRAWING, v)
                if defect in {"receipt", "tenant", "project"}:
                    if defect == "receipt":
                        del bundle["receipt"]
                    else:
                        del bundle["receipt"]["tenant_id" if defect == "tenant" else "project_id"]
                    _binding_plant_bundle(backend, v, bundle)
                elif defect == "json":
                    _binding_plant_bundle(backend, v, b'not JSON "physical"')
                _, _, row = store.resolve_version_entry(backend, TENANT, DRAWING, v)
                key = store._graph_bundle_key(TENANT, DRAWING, row["note"].split(":")[1])
        if defect in {"deleted-bundle", "deleted-intake", "oss"}:
            inner = backend
            class Missing:
                def get(self, requested):
                    if requested == key:
                        if defect == "oss":
                            raise requests.HTTPError(response=_response(404))
                        raise KeyError(key)
                    return inner.get(requested)
            backend = Missing()
        elif defect == "hash":
            backend.put(key, b"different immutable bytes")
        store._physical_memo_clear()
        refused("PHYSICAL_HEAD_CORRUPT", ph.load_physical_head, backend, TENANT, DRAWING,
                project_id=PROJECT, version=v)
        assert not store._physical_memo

    def test_unbound_read_cost(self, backend, graph, monkeypatch):
        manifest = store.load_manifest(backend, TENANT, DRAWING)
        manifest["versions"][0]["bytes"] = store.PHYSICAL_INTAKE_MAX_BYTES + 1
        store.save_manifest(backend, TENANT, DRAWING, manifest)
        class ManifestOnly:
            def get(self, key):
                assert key == store.manifest_key(TENANT, DRAWING)
                return backend.get(key)
        assert store.read_version_physical(ManifestOnly(), TENANT, DRAWING) == (1, None)
        # Restore the row before using the fixture as a graph source.
        manifest["versions"][0]["bytes"] = len(backend.get(store.drawing_version_key(TENANT, DRAWING, 1)))
        store.save_manifest(backend, TENANT, DRAWING, manifest)
        store._physical_memo_clear()
        with held(backend) as fence:
            v = _binding_bundle(backend, graph, fence)
        loads = json.loads
        seen = []
        def checked_loads(raw, *args, **kwargs):
            assert b'"receipt"' not in (raw if type(raw) is bytes else raw.encode())
            seen.append(raw)
            return loads(raw, *args, **kwargs)
        with monkeypatch.context() as patch:
            patch.setattr(store.json, "loads", checked_loads)
            assert store.read_version_physical(backend, TENANT, DRAWING, v) == (v, None)
        assert len(seen) == 1  # the manifest only
        for i in range(store.PHYSICAL_MEMO_MAX + 5):
            manifest["versions"][0]["sha256"] = f"{i:064x}"
            manifest["versions"][0]["bytes"] = store.PHYSICAL_INTAKE_MAX_BYTES + 1
            store.save_manifest(backend, TENANT, DRAWING, manifest)
            assert store.read_version_physical(ManifestOnly(), TENANT, DRAWING, 1) == (1, None)
        assert len(store._physical_memo) == store.PHYSICAL_MEMO_MAX


class TestLegacyAdoption:
    @pytest.fixture(autouse=True)
    def legacy_mode(self, monkeypatch):
        import solar_physical_legacy
        self.legacy = solar_physical_legacy
        monkeypatch.delenv("LEAF_SOLAR_PHYSICAL_VERSIONED", raising=False)
        store._physical_memo_clear()
        yield
        store._physical_memo_clear()

    class Recording(Counting):
        def __init__(self, inner, readonly=False):
            super().__init__(inner)
            self.keys = []
            self.readonly = readonly

        def get(self, key):
            self.keys.append(key)
            return super().get(key)

        def put(self, *args, **kwargs):
            assert not self.readonly, "read attempted a write"
            return self.inner.put(*args, **kwargs)

        def put_if_absent_or_verify(self, *args, **kwargs):
            assert not self.readonly, "read attempted an immutable write"
            return self.inner.put_if_absent_or_verify(*args, **kwargs)

    def _freeze(self, backend, project_id=PROJECT):
        return self.legacy.freeze_legacy(backend, TENANT, DRAWING, project_id=project_id)

    def _view(self, backend, version="head", project_id=PROJECT):
        return ph.physical_head(backend, TENANT, DRAWING, version=version, project_id=project_id)

    def _source_at_two(self, backend, graph):
        with held(backend) as fence:
            v = _binding_bundle(backend, graph, fence)
        return v, publish(backend, project_id=PROJECT)["head"]

    def _append_bound_entry(self, backend, previous):
        stored = ps.store_physical_state(backend, TENANT, DRAWING,
                                         doc(previous["state"]["artifact_id"], 3), project_id=PROJECT)
        state = stored["state"]
        index = previous["index"] + 1
        backend.put_if_absent_or_verify(
            ph.entry_key(TENANT, DRAWING, index),
            ph.entry_bytes(index, PROJECT, previous["state"]["artifact_id"],
                           state["artifact_id"], state["content_sha256"]))
        return {"head_index": index, "state_artifact_id": state["artifact_id"],
                "state_content_sha256": state["content_sha256"]}

    def test_key_constants_and_codes(self, monkeypatch):
        legacy = self.legacy
        assert legacy.freeze_key(TENANT, DRAWING) == PREFIX + "physical-legacy/freeze.json"
        assert "/physical/" not in legacy.freeze_key(TENANT, DRAWING)
        assert legacy.FREEZE_SCHEMA == "leaf.solar-physical-legacy-freeze.v1"
        assert legacy.MAX_RECORD_BYTES == 1024
        assert legacy.SWITCH_ENV == "LEAF_SOLAR_PHYSICAL_VERSIONED"
        assert legacy.CODES == {
            "PHYSICAL_LEGACY_CORRUPT", "PHYSICAL_LEGACY_PROJECT_MISMATCH",
            "PHYSICAL_LEGACY_STORE_UNAVAILABLE", "PHYSICAL_LEGACY_STORE_UNSAFE",
            "PHYSICAL_LEGACY_ID_INVALID", "PHYSICAL_LEGACY_WRITES_DRAINED", "PHYSICAL_LEGACY_FROZEN"}
        assert isinstance(legacy.PhysicalLegacyError("PHYSICAL_LEGACY_FROZEN"), ps.PhysicalStateError)
        for value in (None, "0", "1", "true", "on"):
            if value is None:
                monkeypatch.delenv(legacy.SWITCH_ENV, raising=False)
            else:
                monkeypatch.setenv(legacy.SWITCH_ENV, value)
            assert legacy.versioned_mode() == store.physical_versioned_enabled()

    def test_switch_off_read_path_untouched(self, backend, graph):
        publish(backend)
        with held(backend) as fence:
            _binding_bundle(backend, graph, fence)
            store.undo(backend, TENANT, DRAWING, holder="fixture-owner", fence=fence)
        recording = self.Recording(backend, readonly=True)
        expected, content = ph._head(backend, TENANT, DRAWING, PROJECT)
        assert ph.load_physical_head(recording, TENANT, DRAWING, project_id=PROJECT) == (
            expected, ps.decode_document(content))
        assert not any("physical-legacy" in key for key in recording.keys)

    def test_no_log_absent(self, backend, monkeypatch):
        monkeypatch.setenv(self.legacy.SWITCH_ENV, "1")
        assert self._view(backend) is None

    def test_source_version_and_descendants_show_tip(self, backend, graph, monkeypatch):
        tip = publish(backend)["head"]
        with held(backend) as fence:
            v = _binding_bundle(backend, graph, fence)
        monkeypatch.setenv(self.legacy.SWITCH_ENV, "1")
        assert self._view(backend, 1) == self._view(backend, v) == tip
        assert self.legacy.legacy_reference(backend, TENANT, DRAWING, v,
                                            project_id=PROJECT) == _binding_ref(tip)

    def test_ancestor_of_source_version_absent(self, backend, graph, monkeypatch):
        v, tip = self._source_at_two(backend, graph)
        monkeypatch.setenv(self.legacy.SWITCH_ENV, "1")
        assert self._view(backend, 1) is None
        assert self._view(backend, v) == tip

    def test_undo_below_source_absent_and_redo_restores(self, backend, graph, monkeypatch):
        v, tip = self._source_at_two(backend, graph)
        monkeypatch.setenv(self.legacy.SWITCH_ENV, "1")
        with held(backend) as fence:
            assert store.undo(backend, TENANT, DRAWING, holder="fixture-owner", fence=fence) == 1
            assert self._view(backend) is None
            assert store.redo(backend, TENANT, DRAWING, holder="fixture-owner", fence=fence) == v
            assert self._view(backend) == tip

    def test_pinned_preflip_undo_case(self, backend, graph, monkeypatch):
        first = publish(backend)["head"]
        with held(backend) as fence:
            v = _binding_bundle(backend, graph, fence)
            tip = publish(backend, first["state"]["artifact_id"], 3)["head"]
            store.undo(backend, TENANT, DRAWING, holder="fixture-owner", fence=fence)
            branch = _binding_bundle(backend, graph, fence, "branch")
        monkeypatch.setenv(self.legacy.SWITCH_ENV, "1")
        assert self._view(backend, 1) is None
        assert self._view(backend, v) == tip
        assert self._view(backend, branch) is None

    def test_commits_after_adoption_keep_state(self, backend, graph, monkeypatch):
        tip = publish(backend)["head"]
        payload = _binding_intake(backend)
        monkeypatch.setenv(self.legacy.SWITCH_ENV, "1")
        assert self._view(backend) == tip
        with held(backend) as fence:
            bundle = _binding_bundle(backend, graph, fence)
            intake = _binding_payload(backend, payload, parent=bundle, fence=fence)
        before = backend.drawing_object_keys(TENANT, DRAWING)
        readonly = self.Recording(backend, readonly=True)
        assert self._view(readonly, bundle) == self._view(readonly, intake) == tip
        assert backend.drawing_object_keys(TENANT, DRAWING) == before

    def test_reads_write_nothing(self, backend, graph, monkeypatch):
        readonly = self.Recording(backend, readonly=True)
        monkeypatch.setenv(self.legacy.SWITCH_ENV, "1")
        before = backend.drawing_object_keys(TENANT, DRAWING)
        assert self._view(readonly) is None
        assert backend.drawing_object_keys(TENANT, DRAWING) == before
        monkeypatch.delenv(self.legacy.SWITCH_ENV)
        v, tip = self._source_at_two(backend, graph)
        with held(backend) as fence:
            child = _binding_bundle(backend, graph, fence, "child")
            store.undo(backend, TENANT, DRAWING, holder="fixture-owner", fence=fence)
            store.undo(backend, TENANT, DRAWING, holder="fixture-owner", fence=fence)
            branch = _binding_payload(backend, _binding_intake(backend), fence=fence)
        monkeypatch.setenv(self.legacy.SWITCH_ENV, "1")
        before = backend.drawing_object_keys(TENANT, DRAWING)
        for version, expected in ((1, None), (v, tip), (child, tip), (branch, None), ("head", None)):
            assert self._view(readonly, version) == expected
        assert backend.drawing_object_keys(TENANT, DRAWING) == before

    def test_freeze_records_tip_and_is_idempotent(self, backend, monkeypatch):
        tip = publish(backend)["head"]
        monkeypatch.setenv(self.legacy.SWITCH_ENV, "1")
        record = self._freeze(backend)
        assert record == {"schema": self.legacy.FREEZE_SCHEMA, "tenant_id": TENANT,
                          "drawing_id": DRAWING, "project_id": PROJECT, "legacy_last_index": 0,
                          "state": tip["state"]["artifact_id"],
                          "content_sha256": tip["state"]["content_sha256"], "source_version": 1}
        key = self.legacy.freeze_key(TENANT, DRAWING)
        raw = backend.get(key)
        recording = self.Recording(backend)
        assert self._freeze(recording) == record
        assert recording.keys == [key]
        assert backend.get(key) == raw

    def test_freeze_empty_log(self, backend, monkeypatch):
        monkeypatch.setenv(self.legacy.SWITCH_ENV, "1")
        record = self._freeze(backend)
        assert record["legacy_last_index"] == -1
        assert all(record[k] is None for k in ("project_id", "state", "content_sha256", "source_version"))
        assert self._freeze(backend, "another-project") == record
        assert self._view(backend) is None

    def test_freeze_threaded_one_record(self, backend, monkeypatch):
        from concurrent.futures import ThreadPoolExecutor
        publish(backend)
        monkeypatch.setenv(self.legacy.SWITCH_ENV, "1")
        barrier = threading.Barrier(8)
        def freeze(_):
            barrier.wait(timeout=10)
            return self._freeze(backend)
        with ThreadPoolExecutor(max_workers=8) as pool:
            records = list(pool.map(freeze, range(8)))
        from leaf_cloud_client import canonical_bytes
        assert all(canonical_bytes(record) == backend.get(self.legacy.freeze_key(TENANT, DRAWING))
                   for record in records)
        assert sum("physical-legacy/" in k for k in backend.drawing_object_keys(TENANT, DRAWING)) == 1

    def test_frozen_reads_ignore_later_entries(self, backend, graph, monkeypatch):
        tip = publish(backend)["head"]
        with held(backend) as fence:
            child = _binding_bundle(backend, graph, fence)
        monkeypatch.setenv(self.legacy.SWITCH_ENV, "1")
        self._freeze(backend)
        ref = self._append_bound_entry(backend, tip)
        assert self._view(backend, child) == tip
        with held(backend) as fence:
            bound, _ = _binding_commit(backend, fence, ref, child, "v3-test-13")
        assert _binding_ref(self._view(backend, bound)) == ref
        assert self._view(backend, child) == tip

    @pytest.mark.parametrize("switch_on", [False, True])
    def test_unbound_publish_refused_when_frozen(self, backend, monkeypatch, switch_on):
        tip = publish(backend)["head"]
        self._freeze(backend)
        before = backend.drawing_object_keys(TENANT, DRAWING)
        if switch_on:
            monkeypatch.setenv(self.legacy.SWITCH_ENV, "1")
            refused("PHYSICAL_LEGACY_FROZEN", self.legacy.refuse_if_frozen, backend, TENANT, DRAWING)
        else:
            refused("PHYSICAL_LEGACY_FROZEN", publish, backend, tip["state"]["artifact_id"], 3)
        assert backend.drawing_object_keys(TENANT, DRAWING) == before

    @pytest.mark.parametrize("switch_on", [False, True])
    def test_unbound_publish_unchanged_when_not_frozen(self, backend, monkeypatch, switch_on):
        recording = self.Recording(backend)
        key = self.legacy.freeze_key(TENANT, DRAWING)
        if switch_on:
            monkeypatch.setenv(self.legacy.SWITCH_ENV, "1")
            assert self.legacy.refuse_if_frozen(recording, TENANT, DRAWING) is None
            assert recording.keys == [key]
        else:
            assert publish(recording)["created"]
            assert recording.keys.count(key) == 2
            assert not any("physical-legacy" in k and k != key for k in recording.keys)
            assert self._view(backend) == ph._head(backend, TENANT, DRAWING, PROJECT)[0]

    def test_retry_of_head_after_freeze_returns_existing(self, backend, monkeypatch):
        first = publish(backend)
        monkeypatch.setenv(self.legacy.SWITCH_ENV, "1")
        self._freeze(backend)
        monkeypatch.delenv(self.legacy.SWITCH_ENV)
        before = backend.drawing_object_keys(TENANT, DRAWING)
        recording = self.Recording(backend)
        retry = publish(recording)
        assert retry["created"] is False
        assert retry["head"] == first["head"]
        assert backend.drawing_object_keys(TENANT, DRAWING) == before
        assert recording.keys.count(self.legacy.freeze_key(TENANT, DRAWING)) == 0

    def test_freeze_racing_publish_is_refused(self, backend, monkeypatch):
        tip = publish(backend)["head"]
        index = tip["index"] + 1
        entry_key = ph.entry_key(TENANT, DRAWING, index)
        records = []
        legacy = self.legacy

        class FreezeBeforeAppend(self.Recording):
            def put_if_absent_or_verify(self, key, raw):
                if key == entry_key and not records:
                    with monkeypatch.context() as patch:
                        patch.setenv(legacy.SWITCH_ENV, "1")
                        records.append(legacy.freeze_legacy(
                            self.inner, TENANT, DRAWING, project_id=PROJECT))
                return super().put_if_absent_or_verify(key, raw)

        refused("PHYSICAL_LEGACY_FROZEN", publish, FreezeBeforeAppend(backend),
                tip["state"]["artifact_id"], 3)
        assert len(records) == 1
        status = legacy.legacy_status(backend, TENANT, DRAWING)
        assert status["stranded_after_freeze"] is True
        assert status["log_last_index"] == index
        assert status["freeze"] == records[0]
        view, _ = legacy.legacy_view(backend, TENANT, DRAWING, 1, project_id=PROJECT)
        assert view == tip
        assert view["index"] == records[0]["legacy_last_index"] < index

    def test_freeze_after_append_keeps_success(self, backend, monkeypatch):
        tip = publish(backend)["head"]
        index = tip["index"] + 1
        entry_key = ph.entry_key(TENANT, DRAWING, index)
        records = []
        legacy = self.legacy

        class FreezeAfterAppend(self.Recording):
            def put_if_absent_or_verify(self, key, raw):
                result = super().put_if_absent_or_verify(key, raw)
                if key == entry_key and not records:
                    with monkeypatch.context() as patch:
                        patch.setenv(legacy.SWITCH_ENV, "1")
                        records.append(legacy.freeze_legacy(
                            self.inner, TENANT, DRAWING, project_id=PROJECT))
                return result

        result = publish(FreezeAfterAppend(backend), tip["state"]["artifact_id"], 3)
        assert result["created"] is True
        assert result["head"]["index"] == index
        assert len(records) == 1
        assert records[0]["legacy_last_index"] == index
        status = legacy.legacy_status(backend, TENANT, DRAWING)
        assert status["freeze"] == records[0]
        assert status["stranded_after_freeze"] is False

    def test_freeze_drained_refused(self, backend, monkeypatch):
        monkeypatch.setenv("LEAF_DRAWING_MUTATIONS_ENABLED", "0")
        recording = self.Recording(backend)
        before = backend.drawing_object_keys(TENANT, DRAWING)
        refused("PHYSICAL_LEGACY_WRITES_DRAINED", self._freeze, recording)
        assert recording.keys == []
        assert backend.drawing_object_keys(TENANT, DRAWING) == before

    def test_freeze_store_unsafe_refused(self, backend):
        before = backend.drawing_object_keys(TENANT, DRAWING)
        refused("PHYSICAL_LEGACY_STORE_UNSAFE", self._freeze, Naive(backend))
        assert backend.drawing_object_keys(TENANT, DRAWING) == before

    def test_restore_bundle_carries_legacy_ref(self, backend, graph, monkeypatch):
        # Amendment E: only a source's recorded binding is carried by restore.
        from routers import drawings
        tip = publish(backend)["head"]
        monkeypatch.setenv(self.legacy.SWITCH_ENV, "1")
        ref = self.legacy.legacy_reference(backend, TENANT, DRAWING, 1, project_id=PROJECT)
        with held(backend) as fence:
            source = _binding_bundle(backend, graph, fence, physical=ref)
            _binding_bundle(backend, graph, fence, "later")
            restored = drawings.restore_drawing_version(
                TENANT, DRAWING, source, actor="restore", holder="fixture-owner", fence=fence, backend=backend)
        assert store.read_graph_bundle(backend, TENANT, DRAWING, restored.version)["physical"] == ref
        assert self._view(backend) == tip

    def test_restore_intake_carries_legacy_ref(self, backend, monkeypatch):
        from routers import drawings
        from leaf_cloud_client import canonical_bytes
        tip = publish(backend)["head"]
        monkeypatch.setenv(self.legacy.SWITCH_ENV, "1")
        ref = self.legacy.legacy_reference(backend, TENANT, DRAWING, 1, project_id=PROJECT)
        payload = dict(_binding_intake(backend), solar_physical=ref)
        with held(backend) as fence:
            source = _binding_payload(backend, payload, fence=fence)
            restored = drawings.restore_drawing_version(
                TENANT, DRAWING, source, actor="restore", holder="fixture-owner", fence=fence, backend=backend)
        raw = backend.get(store.drawing_version_key(TENANT, DRAWING, restored.version))
        assert raw == canonical_bytes(payload)
        assert json.loads(raw)["solar_physical"] == ref
        assert json.loads(backend.get(write_loop.intake_cache_key(TENANT, DRAWING, restored.version))) == payload
        assert self._view(backend) == tip

    def test_restore_of_non_descendant_absent(self, backend, graph, monkeypatch):
        from routers import drawings
        self._source_at_two(backend, graph)
        monkeypatch.setenv(self.legacy.SWITCH_ENV, "1")
        with held(backend) as fence:
            store.undo(backend, TENANT, DRAWING, holder="fixture-owner", fence=fence)
            restored = drawings.restore_drawing_version(
                TENANT, DRAWING, 1, actor="restore", holder="fixture-owner", fence=fence, backend=backend)
        assert "solar_physical" not in _binding_intake(backend, restored.version)
        assert store.read_version_physical(backend, TENANT, DRAWING)[1] is None
        assert self._view(backend) is None

    def test_restore_request_physical_mismatch_refused(self, backend, graph, monkeypatch):
        ref = _binding_ref(publish(backend)["head"])
        with held(backend) as fence:
            source = _binding_bundle(backend, graph, fence)
            restored = store.read_graph_bundle(backend, TENANT, DRAWING, source)
            request = _binding_request(backend, graph, "restore")
            request.update({k: restored[k] for k in ("graph", "intake", "mapping")})
            request.update(restore_version=source, physical=ref)
            for enabled in ("1", "0"):
                monkeypatch.setenv(self.legacy.SWITCH_ENV, enabled)
                with pytest.raises(ValueError, match="restore must copy the complete graph bundle"):
                    write_loop.apply_graph_version(backend, TENANT, DRAWING, b"binding DWG", request,
                                                    holder="fixture-owner", fence=fence)

    def test_project_mismatch(self, backend, monkeypatch):
        publish(backend, project_id=PROJECT)
        monkeypatch.setenv(self.legacy.SWITCH_ENV, "1")
        for frozen in (False, True):
            if frozen:
                self._freeze(backend)
            before = backend.drawing_object_keys(TENANT, DRAWING)
            refused("PHYSICAL_HEAD_PROJECT_MISMATCH", self._view, backend, project_id="foreign")
            assert backend.drawing_object_keys(TENANT, DRAWING) == before

    def test_tenant_isolation(self, backend, monkeypatch):
        publish(backend)
        self._freeze(backend)
        monkeypatch.setenv(self.legacy.SWITCH_ENV, "1")
        recording = self.Recording(backend, readonly=True)
        assert ph.physical_head(recording, "tenant-b", DRAWING, project_id=PROJECT) is None
        assert self.legacy.legacy_view(recording, "tenant-b", DRAWING, 1, project_id=PROJECT) == (None, None)
        assert recording.keys and all(k.startswith("tenants/tenant-b/") for k in recording.keys)

    @pytest.mark.parametrize("defect", ["oversize", "non-canonical", "extra", "missing", "schema",
                                       "tenant", "hex", "null-pairing"])
    def test_corrupt_freeze(self, backend, monkeypatch, defect):
        from leaf_cloud_client import canonical_bytes
        publish(backend)
        record = self._freeze(backend)
        if defect == "extra":
            record["extra"] = 1
        elif defect == "missing":
            del record["state"]
        elif defect == "schema":
            record["schema"] = "wrong"
        elif defect == "tenant":
            record["tenant_id"] = "other"
        elif defect == "hex":
            record["state"] = "A" * 64
        elif defect == "null-pairing":
            record["legacy_last_index"] = -1
        raw = canonical_bytes(record)
        if defect == "oversize":
            raw += b" " * 1024
        elif defect == "non-canonical":
            raw += b"\n"
        backend.put(self.legacy.freeze_key(TENANT, DRAWING), raw)
        monkeypatch.setenv(self.legacy.SWITCH_ENV, "1")
        before = backend.drawing_object_keys(TENANT, DRAWING)
        refused("PHYSICAL_HEAD_CORRUPT", self._view, backend)
        refused("PHYSICAL_LEGACY_CORRUPT", self.legacy.refuse_if_frozen, backend, TENANT, DRAWING)
        assert backend.drawing_object_keys(TENANT, DRAWING) == before

    def test_frozen_entry_mismatch_corrupt(self, backend, monkeypatch):
        from leaf_cloud_client import canonical_bytes
        publish(backend)
        record = self._freeze(backend)
        record["state"] = "0" * 64
        backend.put(self.legacy.freeze_key(TENANT, DRAWING), canonical_bytes(record))
        monkeypatch.setenv(self.legacy.SWITCH_ENV, "1")
        refused("PHYSICAL_HEAD_CORRUPT", self._view, backend)

    def test_lineage_cycle_and_missing_row_corrupt(self, backend, graph, monkeypatch):
        v, _ = self._source_at_two(backend, graph)
        with held(backend) as fence:
            child = _binding_bundle(backend, graph, fence, "child")
        original = store.load_manifest(backend, TENANT, DRAWING)
        monkeypatch.setenv(self.legacy.SWITCH_ENV, "1")
        for parent in (child, 999):
            manifest = copy.deepcopy(original)
            next(row for row in manifest["versions"] if row["v"] == child)["parent"] = parent
            store.save_manifest(backend, TENANT, DRAWING, manifest)
            store._physical_memo_clear()
            refused("PHYSICAL_HEAD_CORRUPT", self._view, backend, child)
        assert v != child

    def test_store_unavailable(self, backend, monkeypatch):
        tip = publish(backend)["head"]
        key = self.legacy.freeze_key(TENANT, DRAWING)
        class Unavailable(self.Recording):
            def get(self, requested):
                if requested == key:
                    raise OSError("unavailable")
                return super().get(requested)
        recording = Unavailable(backend)
        monkeypatch.setenv(self.legacy.SWITCH_ENV, "1")
        refused("PHYSICAL_HEAD_STORE_UNAVAILABLE", self._view, recording)
        monkeypatch.delenv(self.legacy.SWITCH_ENV)
        refused("PHYSICAL_LEGACY_STORE_UNAVAILABLE", publish, recording,
                tip["state"]["artifact_id"], 3)

    def test_read_cost_bounded(self, backend, graph, monkeypatch):
        v, first = self._source_at_two(backend, graph)
        tip = publish(backend, first["state"]["artifact_id"], 3)["head"]
        baseline = self.Recording(backend, readonly=True)
        ph._head(baseline, TENANT, DRAWING, PROJECT)
        monkeypatch.setenv(self.legacy.SWITCH_ENV, "1")
        recording = self.Recording(backend, readonly=True)
        assert self.legacy.legacy_view(recording, TENANT, DRAWING, v, project_id=PROJECT)[0] == tip
        assert recording.log_reads == baseline.log_reads
        assert len(recording.log_reads) <= 13
        assert len(set(recording.log_reads)) == len(recording.log_reads)
        assert recording.keys.count(self.legacy.freeze_key(TENANT, DRAWING)) == 1
        assert recording.keys.count(store.manifest_key(TENANT, DRAWING)) == 1
        assert sum("/artifacts/" in k for k in recording.keys) == 2
        assert len(recording.keys) == len(baseline.keys) + 2
        self._freeze(backend)
        for version, descendant in ((1, False), (v, True)):
            recording = self.Recording(backend, readonly=True)
            view, _ = self.legacy.legacy_view(recording, TENANT, DRAWING, version, project_id=PROJECT)
            assert view == (tip if descendant else None)
            assert recording.keys.count(self.legacy.freeze_key(TENANT, DRAWING)) == 1
            assert recording.keys.count(store.manifest_key(TENANT, DRAWING)) == 1
            assert recording.log_reads == ([1, 0] if descendant else [])
            assert sum("/artifacts/" in k for k in recording.keys) == (2 if descendant else 0)
            assert len(recording.keys) == (6 if descendant else 2)
        # The first frozen entry has no predecessor to fetch.
        from leaf_cloud_client import canonical_bytes
        record = self.legacy._read_freeze(backend, TENANT, DRAWING)
        record.update(legacy_last_index=0, state=first["state"]["artifact_id"],
                      content_sha256=first["state"]["content_sha256"])
        backend.put(self.legacy.freeze_key(TENANT, DRAWING), canonical_bytes(record))
        recording = self.Recording(backend, readonly=True)
        assert self.legacy.legacy_view(recording, TENANT, DRAWING, v, project_id=PROJECT)[0] == first
        assert recording.log_reads == [0] and len(recording.keys) == 5

    def test_legacy_status_reports_stranded(self, backend, monkeypatch):
        tip = publish(backend)["head"]
        self._freeze(backend)
        ref = self._append_bound_entry(backend, tip)
        status = self.legacy.legacy_status(backend, TENANT, DRAWING)
        assert status["freeze"]["legacy_last_index"] == 0
        assert status["log_last_index"] == 1 and status["stranded_after_freeze"]
        monkeypatch.setenv(self.legacy.SWITCH_ENV, "1")
        with held(backend) as fence:
            _binding_commit(backend, fence, ref)
        assert not self.legacy.legacy_status(backend, TENANT, DRAWING)["stranded_after_freeze"]

    def test_terrain_get_end_to_end(self, backend, graph, monkeypatch):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from envelopes import install_error_handlers
        from routers import solar_terrain as route
        _, tip = self._source_at_two(backend, graph)
        with held(backend) as fence:
            _binding_bundle(backend, graph, fence, "descendant")
        monkeypatch.setenv(self.legacy.SWITCH_ENV, "1")
        monkeypatch.delenv("LEAF_AUTH_LIVE", raising=False)
        monkeypatch.delenv("APS_LIVE", raising=False)
        app = FastAPI()
        install_error_handlers(app)
        app.include_router(route.router)
        with TestClient(app) as client, held(backend) as fence:
            def get():
                response = client.get(f"/api/drawings/{DRAWING}/terrain?project_id={PROJECT}",
                                      headers={"X-Tenant-Id": TENANT})
                assert response.status_code == 200, response.text
                return response.json()
            assert get()["stored"] and get()["head"] == tip
            store.undo(backend, TENANT, DRAWING, holder="fixture-owner", fence=fence)
            store.undo(backend, TENANT, DRAWING, holder="fixture-owner", fence=fence)
            assert get()["stored"] is False
            store.redo(backend, TENANT, DRAWING, holder="fixture-owner", fence=fence)
            assert get()["stored"] and get()["head"]["state"]["artifact_id"] == tip["state"]["artifact_id"]

    def test_postgres_lineage_and_restore(self, backend, graph, tmp_path, monkeypatch, request):
        from routers import drawings
        def exercise(target, tenant, drawing):
            fence = store.acquire_checkout_fence(target, tenant, drawing, "fixture-owner", 300)
            try:
                from leaf_cloud_client import canonical_bytes
                payload = json.loads(target.get(store.drawing_version_key(tenant, drawing, 1)))
                anchor = write_loop._put_bytes_version(
                    target, tenant, drawing, canonical_bytes(payload), 1, {"tool": "legacy-anchor"},
                    holder="fixture-owner", fence=fence)
                monkeypatch.delenv(self.legacy.SWITCH_ENV, raising=False)
                first = ph.publish_physical_state(target, tenant, drawing, doc(), project_id=PROJECT)["head"]
                monkeypatch.setenv(self.legacy.SWITCH_ENV, "1")
                child = write_loop._put_bytes_version(
                    target, tenant, drawing, canonical_bytes(payload), anchor, {"tool": "legacy-test"},
                    holder="fixture-owner", fence=fence)
                for version in (anchor, child):
                    assert ph.physical_head(target, tenant, drawing, version=version, project_id=PROJECT) == first
                store.undo(target, tenant, drawing, holder="fixture-owner", fence=fence)
                assert ph.physical_head(target, tenant, drawing, project_id=PROJECT) == first
                store.undo(target, tenant, drawing, holder="fixture-owner", fence=fence)
                assert ph.physical_head(target, tenant, drawing, project_id=PROJECT) is None
                store.redo(target, tenant, drawing, holder="fixture-owner", fence=fence)
                assert ph.physical_head(target, tenant, drawing, project_id=PROJECT) == first
                restored = drawings.restore_drawing_version(
                    tenant, drawing, anchor, actor="restore", holder="fixture-owner", fence=fence, backend=target)
                assert store.read_version_physical(target, tenant, drawing, restored.version)[1] is None
                assert ph.physical_head(target, tenant, drawing, project_id=PROJECT) == first
            finally:
                store.release_checkout(target, tenant, drawing, "fixture-owner")
        exercise(backend, TENANT, DRAWING)
        if os.environ.get("DATABASE_URL"):
            request.getfixturevalue("postgres_authority")
            import uuid
            tenant, drawing = "legacy-" + uuid.uuid4().hex[:12], "physical"
            memory = store.InMemoryBackend()
            source = tmp_path / "pg-legacy.json"
            from solar_sizing_client import digest
            source.write_text(json.dumps({"solar_design_graph": graph,
                                          "solar_design_graph_sha256": digest(graph)}))
            store.ingest_drawing(memory, tenant, str(source), drawing_id=drawing)
            exercise(memory, tenant, drawing)

    @pytest.mark.parametrize("frozen", [False, True])
    def test_freeze_project_mismatch(self, backend, monkeypatch, frozen):
        publish(backend, project_id=PROJECT)
        monkeypatch.setenv(self.legacy.SWITCH_ENV, "1")
        if frozen:
            self._freeze(backend)
        before = backend.drawing_object_keys(TENANT, DRAWING)
        recording = self.Recording(backend)
        refused("PHYSICAL_LEGACY_PROJECT_MISMATCH", self._freeze, recording, "foreign")
        assert not any("/artifacts/" in key for key in recording.keys)
        assert backend.drawing_object_keys(TENANT, DRAWING) == before

    @pytest.mark.parametrize("project_id", [None, "", "x" * 101, 7])
    def test_freeze_project_id_invalid(self, backend, project_id):
        recording = self.Recording(backend)
        refused("PHYSICAL_LEGACY_ID_INVALID", self._freeze, recording, project_id)
        assert recording.keys == []

    def test_restore_unbound_legacy_descendant_records_no_reference(self, backend, graph, monkeypatch):
        from routers import drawings
        tip = publish(backend)["head"]
        with held(backend) as fence:
            source = _binding_bundle(backend, graph, fence)
            _binding_bundle(backend, graph, fence, "later", physical=_binding_ref(tip))
            monkeypatch.setenv(self.legacy.SWITCH_ENV, "1")
            assert self._view(backend, source) == tip
            restored = drawings.restore_drawing_version(
                TENANT, DRAWING, source, actor="restore", holder="fixture-owner", fence=fence, backend=backend)
        bundle = store.read_graph_bundle(backend, TENANT, DRAWING, restored.version)
        assert "physical" not in bundle
        assert store.read_version_physical(backend, TENANT, DRAWING, restored.version)[1] is None
