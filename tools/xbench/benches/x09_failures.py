"""Offline failure ledger; this helper provides no live acceptance evidence.

Pairs use name/version/drawing_sha256; results use verdict/assertion. observe
returns a verdict and (when applicable) key. Invalid identities raise ValueError.
notify_new accepts sender(message) -> nonempty string message ID. A claimed
notice is never retried, even after a crash; PASS requires a durable receipt for
every attempted notice. Receipt IDs and run IDs are stored as SHA256 digests.
"""

import hashlib
import html
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timezone


ASSERTIONS = frozenset(("sha256", "count", "exact-values"))
EXECUTION_TIME = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])"
)


def _execution_time(value):
    if type(value) is not str or EXECUTION_TIME.fullmatch(value) is None:
        raise ValueError("executed_at must be an ISO 8601 timestamp with a UTC offset")
    try:
        instant = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
        utc = instant.astimezone(timezone.utc)
        return utc.isoformat(timespec="microseconds").removesuffix("+00:00") + "Z"
    except (ValueError, OverflowError) as exc:
        raise ValueError("executed_at is outside the supported calendar range") from exc


def _text(value, limit=128):
    if (type(value) is not str or not value.strip() or len(value) > limit
            or any(unicodedata.category(c).startswith("C") for c in value)):
        raise ValueError("invalid bounded identity string")
    return value


def _digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def failure_key(pair, assertion):
    """SHA256 of the UTF-8 compact JSON array of the exact frozen identity."""
    if type(pair) is not dict or type(assertion) is not str or assertion not in ASSERTIONS:
        raise ValueError("invalid failure identity")
    name, version = _text(pair.get("name")), _text(pair.get("version"))
    drawing = pair.get("drawing_sha256")
    if type(drawing) is not str or re.fullmatch(r"[0-9a-f]{64}", drawing) is None:
        raise ValueError("invalid drawing hash")
    return _digest(json.dumps([name, version, drawing, assertion],
                              ensure_ascii=False, separators=(",", ":")))


def _display(value):
    # Only catalog-like labels are public. Path-like/free-form identity strings
    # retain a stable opaque representation, never a customer path or value.
    if re.fullmatch(r"[A-Za-z0-9_.+ *\[\]()-]{1,128}", value) is None:
        return "sha256:" + _digest(value)
    value = html.escape(value, quote=True)
    return re.sub(r"([\\`*_{}\[\]()#+.!|>-])", r"\\\1", value)


class FailureStore:
    def __init__(self, db_path):
        self.db_path = os.fspath(db_path)
        # Connections are short-lived, allowing separate instances and threads.
        with self._transaction() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS failures (
                key TEXT PRIMARY KEY, name TEXT NOT NULL, version TEXT NOT NULL,
                drawing_sha256 TEXT NOT NULL, assertion TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('open','resolved')),
                count INTEGER NOT NULL DEFAULT 0,
                notice TEXT NOT NULL DEFAULT 'pending'
                    CHECK(notice IN ('pending','attempted','confirmed')),
                receipt TEXT,
                UNIQUE(name, version, drawing_sha256, assertion))""")
            db.execute("""CREATE TABLE IF NOT EXISTS sightings (
                key TEXT NOT NULL REFERENCES failures(key), run_hash TEXT NOT NULL,
                executed_at TEXT, PRIMARY KEY(key, run_hash))""")
            if all(column["name"] != "executed_at" for column in db.execute("PRAGMA table_info(sightings)")):
                db.execute("ALTER TABLE sightings ADD COLUMN executed_at TEXT")
            db.execute("""CREATE TABLE IF NOT EXISTS transitions (
                id INTEGER PRIMARY KEY, key TEXT NOT NULL REFERENCES failures(key),
                status TEXT NOT NULL, run_hash TEXT NOT NULL)""")

    @contextmanager
    def _transaction(self):
        db = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def observe(self, pair, result, run_id, *, executed_at=None):
        """Record one sighting per failed run, optionally with its execution time."""
        assertion = result.get("assertion") if type(result) is dict else None
        verdict = result.get("verdict") if type(result) is dict else None
        if type(assertion) is not str or assertion not in ASSERTIONS or verdict not in ("PASS", "FAIL"):
            return {"verdict": "UNREADABLE", "key": None}
        key = failure_key(pair, assertion)
        run_hash = _digest(_text(run_id, 256))
        timestamp = _execution_time(executed_at) if verdict == "FAIL" and executed_at is not None else None
        with self._transaction() as db:
            old = db.execute("SELECT status FROM failures WHERE key=?", (key,)).fetchone()
            status = "open" if verdict == "FAIL" else "resolved"
            if verdict == "FAIL":
                db.execute("""INSERT OR IGNORE INTO failures
                    (key,name,version,drawing_sha256,assertion,status)
                    VALUES (?,?,?,?,?, 'open')""",
                           (key, pair["name"], pair["version"], pair["drawing_sha256"], assertion))
                added = db.execute("""INSERT OR IGNORE INTO sightings (key, run_hash, executed_at)
                    VALUES (?, ?, ?)""", (key, run_hash, timestamp)).rowcount
                if not added and timestamp is not None:
                    previous = db.execute("SELECT executed_at FROM sightings WHERE key=? AND run_hash=?",
                                          (key, run_hash)).fetchone()["executed_at"]
                    if previous is None:
                        db.execute("UPDATE sightings SET executed_at=? WHERE key=? AND run_hash=?",
                                   (timestamp, key, run_hash))
                    elif previous != timestamp:
                        raise ValueError("conflicting executed_at for an existing failure sighting")
                db.execute("UPDATE failures SET status='open', count=count+? WHERE key=?",
                           (added, key))
            elif old:
                db.execute("UPDATE failures SET status='resolved' WHERE key=?", (key,))
            if (old is not None or verdict == "FAIL") and (old is None or old["status"] != status):
                db.execute("INSERT INTO transitions(key,status,run_hash) VALUES (?,?,?)",
                           (key, status, run_hash))
        return {"verdict": verdict, "key": key}

    def items(self):
        """Return arrival and execution bounds per identity in one snapshot."""
        with self._transaction() as db:
            # Sightings retain row IDs during backfill and are deduplicated by key and run hash.
            # Row order reflects recording order, independent of IDs supplied by callers.
            # A consistent digest tie-breaker makes execution bounds independent of arrival.
            rows = db.execute("""
                WITH bounds AS (
                    SELECT key, MIN(rowid) AS first_id, MAX(rowid) AS last_id,
                           COUNT(executed_at) = COUNT(*) AS complete_times
                    FROM sightings GROUP BY key
                )
                SELECT failures.*,
                       first_sighting.run_hash AS first_failure_run_sha256,
                       last_sighting.run_hash AS latest_repeat_run_sha256,
                       earliest.run_hash AS earliest_execution_run_sha256,
                       earliest.executed_at AS earliest_executed_at,
                       latest.run_hash AS latest_repeat_execution_run_sha256,
                       latest.executed_at AS latest_repeat_executed_at
                FROM failures
                LEFT JOIN bounds ON bounds.key = failures.key
                LEFT JOIN sightings AS first_sighting ON first_sighting.rowid = bounds.first_id
                LEFT JOIN sightings AS last_sighting
                    ON last_sighting.rowid = bounds.last_id AND bounds.last_id > bounds.first_id
                LEFT JOIN sightings AS earliest ON bounds.complete_times AND earliest.rowid = (
                    SELECT rowid FROM sightings WHERE key = failures.key
                    ORDER BY executed_at, run_hash LIMIT 1
                )
                LEFT JOIN sightings AS latest ON bounds.complete_times
                    AND bounds.last_id > bounds.first_id AND latest.rowid = (
                    SELECT rowid FROM sightings WHERE key = failures.key
                    ORDER BY executed_at DESC, run_hash DESC LIMIT 1
                )
                ORDER BY failures.key
            """)
            return [dict(row) for row in rows]

    def notify_new(self, sender):
        """Claim each FYI before calling sender outside any DB transaction.

        Exceptions and invalid/missing receipts remain attempted/unconfirmed.
        Process termination also leaves the committed claim intact.
        """
        while True:
            with self._transaction() as db:
                row = db.execute("SELECT * FROM failures WHERE notice='pending' "
                                 "ORDER BY key LIMIT 1").fetchone()
                if row is not None:
                    db.execute("UPDATE failures SET notice='attempted' WHERE key=?", (row["key"],))
            if row is None:
                break
            message = (f"X09 failure {row['key']} tool={_display(row['name'])} "
                       f"version={_display(row['version'])} drawing={row['drawing_sha256']} "
                       f"assertion={row['assertion']}")
            try:
                receipt = sender(message)
                _text(receipt, 1024)
                safe_receipt = "sha256:" + _digest(receipt)
            except Exception:
                # No exception text, sender data, or fabricated receipt survives.
                continue
            try:
                with self._transaction() as db:
                    db.execute("UPDATE failures SET notice='confirmed', receipt=? WHERE key=?",
                               (safe_receipt, row["key"]))
            except sqlite3.Error:
                return {"verdict": "UNREADABLE"}
        with self._transaction() as db:
            ambiguous = db.execute("SELECT 1 FROM failures WHERE notice='attempted' "
                                   "OR (notice='confirmed' AND receipt IS NULL) LIMIT 1").fetchone()
        return {"verdict": "UNREADABLE" if ambiguous else "PASS"}

    def export_markdown(self, path):
        """Atomically replace the caller's report with one item per identity."""
        lines = ["# X09 failure ledger", ""]
        for item in self.items():
            first = item["first_failure_run_sha256"]
            repeat = item["latest_repeat_run_sha256"]
            first_ref = f"sha256:{first}" if first else "Unavailable"
            repeat_ref = f"sha256:{repeat}" if repeat else "None recorded"
            earliest = item["earliest_execution_run_sha256"]
            latest = item["latest_repeat_execution_run_sha256"]
            unknown = "Unavailable (execution timestamps missing)"
            earliest_ref = f"sha256:{earliest} at {item['earliest_executed_at']}" if earliest else unknown
            latest_ref = unknown if repeat else "None recorded"
            if latest:
                latest_ref = f"sha256:{latest} at {item['latest_repeat_executed_at']}"
            lines.extend([f"## {item['key']}", "",
                          f"- Status: {item['status']}", f"- Count: {item['count']}",
                          f"- First recorded failure run: {first_ref}",
                          f"- Latest distinct repeat run: {repeat_ref}",
                          f"- Earliest failure execution: {earliest_ref}",
                          f"- Latest repeat execution: {latest_ref}",
                          f"- Tool: {_display(item['name'])}",
                          f"- Version: {_display(item['version'])}",
                          f"- Drawing SHA256: {item['drawing_sha256']}",
                          f"- Assertion: {item['assertion']}",
                          f"- Notice: {item['notice']}", ""])
        destination = Path(path)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                             dir=destination.parent, delete=False) as stream:
                temporary = stream.name
                stream.write("\n".join(lines))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
        finally:
            if temporary is not None and os.path.exists(temporary):
                os.unlink(temporary)
