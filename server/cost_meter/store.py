"""Append-only JSON Lines store for resource share revisions (TCM-01).

Layout under the root (env LEAF_COST_LEDGER_DIR; unset means disabled, readers
return empty and writers refuse):

  revisions/<YYYY-MM>.jsonl          one line per revision, every resource of that month
  publications/<publication_id>.json immutable manifest naming one revision per resource

Nothing is ever rewritten or deleted. A revision line is appended then fsynced;
a torn tail (a crash mid-append) is never parsed, and the next append terminates
it first so it stays one skipped, counted line. A line that fails its schema or
its content digest is skipped and counted in `malformed_lines`, never guessed at.
Single writer per process: revision numbering is serialized by an in-process lock,
and a second writer racing on the same number loses at read time (first line wins).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import localcontext
from pathlib import Path
from typing import Any, List, Optional, Sequence, Tuple

from .ledger import PERIOD_RE, ResourcePeriod, ShareEntry, validate_shares

ENV_DIR = "LEAF_COST_LEDGER_DIR"
REVISION_SCHEMA = "leaf.cost-share-revision.v1"
PUBLICATION_SCHEMA = "leaf.cost-share-publication.v1"
PUBLICATION_ID_RE = re.compile(r"pub-(\d{4}-(?:0[1-9]|1[0-2]))-([0-9a-f]{20})")
_MAX_REASON = 2000


class LedgerStoreDisabled(RuntimeError):
    """A write was attempted with no LEAF_COST_LEDGER_DIR configured."""


class LedgerCorrupt(RuntimeError):
    """A publication names a revision the store cannot read back byte-for-byte."""


def _canon_dec(value) -> Optional[str]:
    # Value-canonical: 10, 10.0 and 1E+1 digest the same. Precision widened so normalize never rounds.
    if value is None:
        return None
    with localcontext() as ctx:
        ctx.prec = max(28, len(value.as_tuple().digits))
        return format(value.normalize(), "f")


def _canon_json(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def content_digest(resource_period: ResourcePeriod, shares: Sequence[ShareEntry]) -> str:
    """sha256 over the revision's content (resource-period plus shares), not its reason or time."""
    rp = resource_period
    body = {
        "resource_period": {
            "resource_id": rp.resource_id,
            "period": rp.period,
            "unit": rp.unit,
            "total_usage": _canon_dec(rp.total_usage),
            "gross_cost_usd": _canon_dec(rp.gross_cost_usd),
            "credits_usd": _canon_dec(rp.credits_usd),
            "source_batch_ids": sorted(rp.source_batch_ids),
            "coverage": rp.coverage,
        },
        "shares": [
            {
                "participant_id": s.participant_id,
                "dimension": s.dimension,
                "participant_usage": _canon_dec(s.participant_usage),
                "usage_share": _canon_dec(s.usage_share),
                "status": s.status,
            }
            for s in sorted(shares, key=lambda s: s.key)
        ],
    }
    return hashlib.sha256(_canon_json(body)).hexdigest()


def revision_id_for(resource_id: str, period: str, revision: int) -> str:
    return f"{resource_id}@{period}#r{revision}"


@dataclass(frozen=True)
class Revision:
    revision_id: str
    resource_id: str
    period: str
    revision: int
    resource_period: ResourcePeriod
    shares: Tuple[ShareEntry, ...]
    reason: str
    supersedes: Optional[str]
    digest: str
    recorded_at: str


def _parse_revision(raw: bytes, period: str) -> Revision:
    obj = json.loads(raw.decode("utf-8"))
    if not isinstance(obj, dict) or obj.get("schema") != REVISION_SCHEMA:
        raise ValueError("not a cost share revision line")
    rp = ResourcePeriod.from_dict(obj["resource_period"])
    shares_raw = obj["shares"]
    if not isinstance(shares_raw, list):
        raise TypeError("shares must be a list")
    shares = validate_shares(ShareEntry.from_dict(s) for s in shares_raw)
    revision = obj["revision"]
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise ValueError("revision must be a positive integer")
    if rp.period != period or obj["resource_id"] != rp.resource_id or obj["period"] != period:
        raise ValueError("revision line is filed under the wrong resource or period")
    if obj["revision_id"] != revision_id_for(rp.resource_id, period, revision):
        raise ValueError("revision id does not match its resource, period and number")
    digest = content_digest(rp, shares)
    if obj["digest"] != digest:
        raise ValueError("content digest mismatch")
    reason, supersedes, recorded_at = obj["reason"], obj["supersedes"], obj["recorded_at"]
    if not isinstance(reason, str) or not isinstance(recorded_at, str):
        raise TypeError("reason and recorded_at must be strings")
    if supersedes is not None and not isinstance(supersedes, str):
        raise TypeError("supersedes must be a string or null")
    return Revision(obj["revision_id"], rp.resource_id, period, revision, rp, shares,
                    reason, supersedes, digest, recorded_at)


def _append_line(path: Path, line: bytes) -> None:
    """Append one line and fsync. A torn tail from an earlier crash is terminated first."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+b") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        if size:
            fh.seek(size - 1)
            if fh.read(1) != b"\n":
                line = b"\n" + line
        fh.write(line)
        fh.flush()
        os.fsync(fh.fileno())


def _write_atomic(path: Path, data: bytes) -> None:
    """Temp file in the same directory, fsync, then rename into place."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class CostLedgerStore:
    """Append-only share ledger. `malformed_lines` counts lines skipped by the last read."""

    def __init__(self, root_dir: Any = None) -> None:
        if root_dir is None:
            root_dir = os.environ.get(ENV_DIR, "")
        text = str(root_dir).strip() if root_dir is not None else ""
        self._root: Optional[Path] = Path(text) if text else None
        self._lock = threading.Lock()
        self.malformed_lines = 0

    @property
    def enabled(self) -> bool:
        return self._root is not None

    def _require_enabled(self) -> Path:
        if self._root is None:
            raise LedgerStoreDisabled(f"cost ledger store is disabled: set {ENV_DIR}")
        return self._root

    @staticmethod
    def _check_period(period: Any) -> str:
        if not isinstance(period, str) or not PERIOD_RE.fullmatch(period):
            raise ValueError(f"period must be YYYY-MM, got {period!r}")
        return period

    def _revisions_path(self, period: str) -> Path:
        return self._require_enabled() / "revisions" / f"{period}.jsonl"

    def _read_period(self, period: str) -> List[Revision]:
        """Every well-formed revision of one month, in file order. Never guesses at a bad line."""
        try:
            data = self._revisions_path(period).read_bytes()
        except FileNotFoundError:
            self.malformed_lines = 0
            return []
        lines = data.split(b"\n")
        tail = lines.pop()  # bytes after the last newline: empty, or a write still in flight
        bad = 0 if tail == b"" else 1
        seen = set()
        out: List[Revision] = []
        for raw in lines:
            if not raw.strip():
                continue
            try:
                rev = _parse_revision(raw, period)
            except (ValueError, TypeError, KeyError, AttributeError, ArithmeticError):
                bad += 1
                continue
            if (rev.resource_id, rev.revision) in seen:
                bad += 1  # a racing second writer: the first line for a number wins
                continue
            seen.add((rev.resource_id, rev.revision))
            out.append(rev)
        self.malformed_lines = bad
        return out

    def history(self, resource_id: str, period: str) -> List[Revision]:
        """All revisions of one resource-period, oldest first. Empty when disabled."""
        self._check_period(period)
        if not self.enabled:
            return []
        revs = [r for r in self._read_period(period) if r.resource_id == resource_id]
        return sorted(revs, key=lambda r: r.revision)

    def current(self, resource_id: str, period: str) -> Optional[Revision]:
        """The newest revision of one resource-period, or None."""
        revs = self.history(resource_id, period)
        return revs[-1] if revs else None

    def append_revision(self, resource_period: ResourcePeriod, shares: Sequence[ShareEntry],
                        reason: str, supersedes: Optional[str] = None) -> str:
        """Validate, then append the next revision. Identical content to current is a no-op.

        `supersedes`, when given, must name the current revision (a stale correction
        is refused rather than silently stacked). The stored line always records the
        revision it actually superseded.
        """
        self._require_enabled()
        if not isinstance(resource_period, ResourcePeriod):
            raise TypeError("resource_period must be a ResourcePeriod")
        shares = validate_shares(shares)
        if resource_period.gross_cost_usd < 0 or resource_period.credits_usd < 0:
            raise ValueError("gross and credits must be >= 0")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > _MAX_REASON:
            raise ValueError(f"reason must be 1..{_MAX_REASON} characters")
        if supersedes is not None and not isinstance(supersedes, str):
            raise TypeError("supersedes must be a revision id string")
        digest = content_digest(resource_period, shares)
        rid, period = resource_period.resource_id, resource_period.period
        with self._lock:
            cur = self.current(rid, period)
            if cur is not None and cur.digest == digest:
                return cur.revision_id
            if supersedes is not None and (cur is None or supersedes != cur.revision_id):
                raise ValueError(f"supersedes {supersedes!r} is not the current revision "
                                 f"({cur.revision_id if cur else None!r})")
            number = (cur.revision if cur else 0) + 1
            revision_id = revision_id_for(rid, period, number)
            record = {
                "schema": REVISION_SCHEMA,
                "revision_id": revision_id,
                "resource_id": rid,
                "period": period,
                "revision": number,
                "resource_period": resource_period.to_dict(),
                "shares": [s.to_dict() for s in shares],
                "reason": reason,
                "supersedes": cur.revision_id if cur else None,
                "digest": digest,
                "recorded_at": _utc_now(),
            }
            _append_line(self._revisions_path(period), _canon_json(record) + b"\n")
            return revision_id

    def publish(self, period: str, *, revision_ids: Optional[Sequence[str]] = None,
                metadata: Optional[dict] = None) -> str:
        """Freeze the current revision of every resource in `period`; returns the publication id.

        Explicit revision_ids freeze only that snapshot, including an empty snapshot.
        Metadata is accepted for caller compatibility but is not stored in the manifest.
        The id is derived from the named revisions, so publishing the same state twice
        returns the same id and never rewrites the manifest.
        """
        root = self._require_enabled()
        self._check_period(period)
        with self._lock:
            newest: dict = {}
            revisions = self._read_period(period)
            for rev in revisions:
                held = newest.get(rev.resource_id)
                if held is None or rev.revision > held.revision:
                    newest[rev.resource_id] = rev
            if revision_ids is not None:
                by_id = {r.revision_id: r for r in revisions}
                selected = [by_id[rid] for rid in revision_ids]
                newest = {r.resource_id: r for r in selected}
                if len(newest) != len(selected):
                    raise ValueError("snapshot names a resource more than once")
            if not newest and revision_ids is None:
                raise ValueError(f"no revisions to publish for {period}")
            entries = [
                {"resource_id": r.resource_id, "revision_id": r.revision_id,
                 "revision": r.revision, "digest": r.digest}
                for r in (newest[k] for k in sorted(newest))
            ]
            publication_id = f"pub-{period}-{hashlib.sha256(_canon_json(entries)).hexdigest()[:20]}"
            path = root / "publications" / f"{publication_id}.json"
            if not path.exists():
                manifest = {
                    "schema": PUBLICATION_SCHEMA,
                    "publication_id": publication_id,
                    "period": period,
                    "published_at": _utc_now(),
                    "revisions": entries,
                }
                _write_atomic(path, _canon_json(manifest) + b"\n")
            return publication_id

    def read_publication(self, publication_id: str) -> List[Revision]:
        """Exactly the revisions a publication named, even after later corrections.

        Empty when disabled; KeyError for an unknown id; LedgerCorrupt when the
        manifest or a revision it names cannot be read back unchanged.
        """
        if not isinstance(publication_id, str):
            raise TypeError("publication id must be a string")
        match = PUBLICATION_ID_RE.fullmatch(publication_id)
        if not match:
            raise ValueError(f"invalid publication id: {publication_id!r}")
        if not self.enabled:
            return []
        path = self._require_enabled() / "publications" / f"{publication_id}.json"
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            raise KeyError(publication_id) from None
        try:
            manifest = json.loads(raw.decode("utf-8"))
            entries = manifest["revisions"]
            identities = [entries]
            if "metadata" in manifest:
                identities.append({"revisions": entries, "metadata": manifest["metadata"]})
            ok = (manifest["schema"] == PUBLICATION_SCHEMA
                  and manifest["publication_id"] == publication_id
                  and manifest["period"] == match.group(1)
                  and isinstance(entries, list)
                  and any(hashlib.sha256(_canon_json(identity)).hexdigest()[:20] == match.group(2)
                          for identity in identities))
        except (ValueError, TypeError, KeyError):
            ok = False
        if not ok:
            raise LedgerCorrupt(f"publication manifest {publication_id} is malformed")
        by_id = {r.revision_id: r for r in self._read_period(match.group(1))}
        out: List[Revision] = []
        for entry in entries:
            rev = by_id.get(entry.get("revision_id")) if isinstance(entry, dict) else None
            if rev is None or rev.digest != entry.get("digest"):
                raise LedgerCorrupt(f"publication {publication_id} names an unreadable revision: {entry!r}")
            out.append(rev)
        return out
