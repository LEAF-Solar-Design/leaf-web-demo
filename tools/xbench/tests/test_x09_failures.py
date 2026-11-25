"""Synthetic ledger checks only; no live sender, customer data, or acceptance."""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import sqlite3
from threading import Barrier

import pytest

from benches.x09_failures import FailureStore, failure_key
from benches.x09_regression import compare_pair


PAIR = {"name": "synthetic-tool", "version": "1.0", "drawing_sha256": "a" * 64}


def result(verdict="FAIL", assertion="count"):
    return {"verdict": verdict, "assertion": assertion}


def test_idempotency_and_frozen_identity(tmp_path):
    store = FailureStore(tmp_path / "ledger.db")
    key = store.observe(PAIR, compare_pair({"rule": "count", "value": 2}, 1), "night-1")["key"]
    canonical = json.dumps([PAIR["name"], PAIR["version"], "a" * 64, "count"], separators=(",", ":"))
    assert key == hashlib.sha256(canonical.encode()).hexdigest()
    store.observe(dict(PAIR, package_sha256="b" * 64, path="/synthetic/private"), result(), "night-1")
    assert store.items()[0]["count"] == 1
    store = FailureStore(tmp_path / "ledger.db")
    store.observe(PAIR, result(), "night-2")
    assert len(store.items()) == 1
    assert store.items()[0]["count"] == 2
    for assertion in ("sha256", "exact-values"):
        store.observe(PAIR, result(assertion=assertion), "night-2")
    assert len({item["key"] for item in store.items()}) == 3
    for field, value in (("name", "another"), ("version", "2"), ("drawing_sha256", "b" * 64)):
        assert failure_key(dict(PAIR, **{field: value}), "count") != key


def test_resolution_reopen_and_history(tmp_path):
    path = tmp_path / "ledger.db"
    store = FailureStore(path)
    for unreadable in ({"verdict": "FAIL"}, result("UNREADABLE"), result(assertion=[])):
        assert store.observe(PAIR, unreadable, "one")["verdict"] == "UNREADABLE"
    assert store.items() == []
    store.observe(PAIR, result(), "one")
    for unreadable in ({"verdict": "PASS"}, result("UNREADABLE"), result("PASS", "unknown"), {}):
        assert store.observe(PAIR, unreadable, "two")["verdict"] == "UNREADABLE"
        assert store.items()[0]["status"] == "open"
    store.observe(PAIR, result("PASS", "sha256"), "two")
    assert len(store.items()) == 1
    assert store.items()[0]["status"] == "open"
    store.observe(PAIR, result("PASS"), "two")
    assert store.items()[0]["status"] == "resolved"
    assert store.items()[0]["count"] == 1
    store.observe(PAIR, result(), "three")
    assert store.items()[0]["status"] == "open"
    assert store.items()[0]["count"] == 2
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT status FROM transitions ORDER BY id").fetchall() == [
            ("open",), ("resolved",), ("open",)]
        assert db.execute("SELECT count(*) FROM sightings").fetchone()[0] == 2


def test_concurrent_instances(tmp_path):
    path = tmp_path / "ledger.db"
    stores = [FailureStore(path), FailureStore(path)]
    barrier = Barrier(2)

    def observe(index):
        barrier.wait()
        for run in ("same", f"separate-{index}"):
            stores[index].observe(PAIR, result(), run)

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(observe, range(2)))
    assert len(stores[0].items()) == 1
    assert stores[0].items()[0]["count"] == 3
    messages = []

    def sender(message):
        # A second connection can write while sender runs: no DB lock is held.
        stores[1].observe(PAIR, result(), "during-send")
        messages.append(message)
        return "synthetic-receipt"

    def notify(index):
        barrier.wait()
        return stores[index].notify_new(sender)

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(notify, range(2)))
    assert len(messages) == 1
    assert stores[0].notify_new(sender) == {"verdict": "PASS"}
    assert stores[0].items()[0]["receipt"] == "sha256:" + hashlib.sha256(b"synthetic-receipt").hexdigest()
    stores[0].observe(PAIR, result("PASS"), "resolved")
    stores[0].observe(PAIR, result(), "reopened")
    stores[0].notify_new(sender)
    assert len(messages) == 1


@pytest.mark.parametrize("mode", ["exception", "missing", "crash"])
def test_ambiguous_attempt_never_resent(tmp_path, mode):
    path = tmp_path / "ledger.db"
    store = FailureStore(path)
    store.observe(PAIR, result(), "one")
    messages = []

    class SimulatedCrash(BaseException):
        pass

    def sender(message):
        messages.append(message)
        if mode == "exception":
            raise RuntimeError("SYNTHETIC_SECRET /private/customer/value")
        if mode == "crash":
            raise SimulatedCrash()
        return None

    if mode == "crash":
        with pytest.raises(SimulatedCrash):
            store.notify_new(sender)
    else:
        assert store.notify_new(sender) == {"verdict": "UNREADABLE"}
    store = FailureStore(path)
    store.observe(PAIR, result(), "two")
    store.observe(PAIR, result("PASS"), "three")
    assert store.notify_new(sender) == {"verdict": "UNREADABLE"}
    assert len(messages) == 1
    assert store.items()[0]["notice"] == "attempted"
    assert store.items()[0]["receipt"] is None
    report = tmp_path / "report.md"
    store.export_markdown(report)
    assert "SYNTHETIC_SECRET" not in report.read_text()
    assert b"SYNTHETIC_SECRET" not in path.read_bytes()


@pytest.mark.parametrize("field,value", [
    ("name", ""), ("name", " " ), ("name", "x\nsecret"),
    ("name", "x\x7f"), ("version", "x\u202e"), ("version", "x" * 129),
    ("drawing_sha256", "A" * 64), ("drawing_sha256", "a" * 63),
])
def test_invalid_identity(tmp_path, field, value):
    store = FailureStore(tmp_path / "ledger.db")
    with pytest.raises(ValueError):
        store.observe(dict(PAIR, **{field: value}), result(), "run")
    assert store.items() == []


def test_sanitized_atomic_export(tmp_path, monkeypatch):
    store = FailureStore(tmp_path / "ledger.db")
    pair = dict(PAIR, name="*[synthetic]*")
    store.observe(pair, dict(result(), value="SYNTHETIC_SECRET", path="/private/customer"), "one")
    store.observe(pair, result(), "two")
    store.observe(dict(PAIR, name="/private/customer.dwg"), result(), "one")
    report = tmp_path / "report.md"
    store.export_markdown(report)
    text = report.read_text()
    assert text.count("\n## ") == 2
    assert "\\*\\[synthetic\\]\\*" in text
    assert "Count: 2" in text
    assert "Drawing SHA256: " + "a" * 64 in text
    assert "Assertion: count" in text
    assert "/private" not in text and "SYNTHETIC_SECRET" not in text
    assert b"SYNTHETIC_SECRET" not in (tmp_path / "ledger.db").read_bytes()

    messages = []

    def sender(message):
        messages.append(message)
        return "SYNTHETIC_SECRET /private/customer/receipt"

    assert store.notify_new(sender) == {"verdict": "PASS"}
    assert len(messages) == 2
    assert all("/private" not in message and "SYNTHETIC_SECRET" not in message
               for message in messages)
    assert b"SYNTHETIC_SECRET" not in (tmp_path / "ledger.db").read_bytes()

    def fail_replace(*args):
        raise OSError("synthetic replace failure")

    monkeypatch.setattr("benches.x09_failures.os.replace", fail_replace)
    with pytest.raises(OSError):
        store.export_markdown(report)
    assert report.read_text() == text
    assert {p.name for p in tmp_path.iterdir()} == {"ledger.db", "report.md"}


@pytest.mark.parametrize("run", ["", "\n", "x" * 257, None])
def test_invalid_run_does_not_create(tmp_path, run):
    store = FailureStore(tmp_path / "ledger.db")
    with pytest.raises(ValueError):
        store.observe(PAIR, result(), run)
    assert store.items() == []


@pytest.mark.parametrize(("seen", "expected"), [
    (
        [("1", "night-03"), ("1", "night-01"), ("1", "night-02")],
        {"1": ("night-03", "night-02", 3)},
    ),
    (
        [("1", "night-01"), ("2", "night-02"), ("1", "night-03")],
        {"1": ("night-01", "night-03", 2), "2": ("night-02", None, 1)},
    ),
    (
        [("1", "night-01"), ("1", "night-02"), ("1", "night-01")],
        {"1": ("night-01", "night-02", 2)},
    ),
], ids=["recorded-order", "independent-revisions", "replayed-first"])
def test_report_identifies_first_failure_and_latest_distinct_repeat(tmp_path, seen, expected):
    path = tmp_path / "ledger.db"
    store = FailureStore(path)
    for version, run_id in seen:
        store.observe(dict(PAIR, version=version), result(), run_id)

    reopened = FailureStore(path)
    report_path = tmp_path / "failures.md"
    reopened.export_markdown(report_path)
    sections = report_path.read_text(encoding="utf-8").split("\n## ")
    rows = reopened.items()
    assert {row["version"] for row in rows} == set(expected)
    for row in rows:
        first, repeat, count = expected[row["version"]]
        first_hash = hashlib.sha256(first.encode()).hexdigest()
        repeat_hash = hashlib.sha256(repeat.encode()).hexdigest() if repeat else None
        assert row["first_failure_run_sha256"] == first_hash
        assert row["latest_repeat_run_sha256"] == repeat_hash
        assert row["count"] == count
        section = next(section for section in sections if section.startswith(row["key"] + "\n"))
        assert f"- First recorded failure run: sha256:{first_hash}" in section
        latest = f"sha256:{repeat_hash}" if repeat_hash else "None recorded"
        assert f"- Latest distinct repeat run: {latest}" in section
        assert first not in section and (repeat is None or repeat not in section)
