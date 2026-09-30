"""sf-w5-physical-state-head: bounded head discovery and optimistic parent publication over the
Ground physical state store (server/solar_physical_state.py). Every expected value below was
measured by running the module against the FilesystemBackend the physical-state tests seed."""
import hashlib
import json
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
