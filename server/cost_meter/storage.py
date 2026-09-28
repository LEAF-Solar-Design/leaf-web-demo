"""Per-tenant storage snapshots and the monthly storage observation (TCM-05).

Transparency of Leaf's real cost only, never billing. A daily collector
(scripts/collect-cost-storage.py) calls snapshot() over the tenant-keyed stores
the server already writes, and appends one JSONL line per run. At publish time
storage_usage_observation() integrates those lines into one usage observation
for the file system the stores live on (aws:amazon-elastic-file-system,
gb-month). It never writes the ledger; a publisher joins usage to cost.

How each store is keyed (read from the code that writes it):
  uploads        LEAF_UPLOADS_DIR/<tenant>--<drawing><ext>      guest_uploads.staged_path, broker._resolve_upload_dwg
  drawings       LEAF_STORE_DIR/tenants/<tenant>/drawings/...   da/store.py drawing key, write_loop.store_dir
  tenant_git     LEAF_TENANT_GIT_DIR/<tenant>.git               customization_service._bare_repo
  marathon_runs  LEAF_MARATHON_RUNS_DIR/<tenant>/<run_id>/...   marathon_runs.py
Bytes no tenant owns, or whose owner name is not a valid participant id, go to
leaf|unattributed.

HARDENING CONTRACT. snapshot() is bounded: at most max_entries directory
entries per root (the cap stops the scan and marks the root truncated), no link
or Windows reparse point is ever followed (each is skipped and counted), a
configured root nested inside another is skipped by the outer scan so no byte
is counted twice, and an unreadable directory is counted, never raised. The
observation is exact: integer bytes times integer seconds, one division per
participant, amounts as decimal strings, never floats. Malformed snapshot
lines are skipped and make the coverage partial; unknown is null, never 0.
"""
from __future__ import annotations

import json
import os
import stat as stat_mod
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from .ledger import ESTIMATED, LEAF, PERIOD_RE, validate_participant

SNAPSHOT_KIND = "storage_snapshot"
SNAPSHOT_SCHEMA = "leaf.cost-storage-snapshot.v1"
RESOURCE_ID = "aws:amazon-elastic-file-system"
UNIT = "gb-month"
SOURCE = "cost_meter.storage"
SNAPSHOTS_ENV = "LEAF_COST_STORAGE_SNAPSHOTS"

# Root kind -> the env var the server reads it from. Order is the scan order.
ROOT_ENVS: Dict[str, str] = {
    "uploads": "LEAF_UPLOADS_DIR",
    "drawings": "LEAF_STORE_DIR",
    "tenant_git": "LEAF_TENANT_GIT_DIR",
    "marathon_runs": "LEAF_MARATHON_RUNS_DIR",
}

UNATTRIBUTED_KEY = f"{LEAF}|unattributed"
MAX_ENTRIES_PER_ROOT = 2_000_000
MAX_SNAPSHOT_LINES = 50_000
MAX_LINE_BYTES = 8 * 1024 * 1024
MAX_GAP = timedelta(hours=48)
BYTES_PER_GB = 10 ** 9
USAGE_QUANTUM = Decimal("0.000000000000001")  # 15 places: one byte for one hour still shows
TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

_REPARSE_POINT = getattr(stat_mod, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_IS_WINDOWS = os.name == "nt"


# --------------------------------------------------------------------------- #
# naming and time helpers
# --------------------------------------------------------------------------- #
def aws_resource_id(service_name: str) -> str:
    """Cost Explorer SERVICE name -> resource id: lowercased, each run of
    non-alphanumerics one '-', trimmed, prefixed 'aws:'."""
    if not isinstance(service_name, str) or not service_name.strip():
        raise ValueError("service name is required")
    out: List[str] = []
    dash = False
    for ch in service_name.lower():
        if ch.isascii() and ch.isalnum():
            out.append(ch)
            dash = False
        elif not dash:
            out.append("-")
            dash = True
    slug = "".join(out).strip("-")
    if not slug:
        raise ValueError(f"service name has no alphanumerics: {service_name!r}")
    return "aws:" + slug


def format_timestamp(moment: datetime) -> str:
    """Aware datetime -> 'YYYY-MM-DDTHH:MM:SSZ' in UTC. Naive is refused."""
    if not isinstance(moment, datetime) or moment.tzinfo is None:
        raise ValueError("timestamp must be a timezone-aware datetime")
    return moment.astimezone(timezone.utc).strftime(TIMESTAMP_FORMAT)


def parse_timestamp(text: Any) -> datetime:
    if not isinstance(text, str):
        raise ValueError("taken_at must be a string")
    return datetime.strptime(text, TIMESTAMP_FORMAT).replace(tzinfo=timezone.utc)


def period_bounds(period: str) -> Tuple[datetime, datetime]:
    """'YYYY-MM' -> [first instant, first instant of the next month) in UTC."""
    if not isinstance(period, str) or not PERIOD_RE.fullmatch(period):
        raise ValueError(f"period must be YYYY-MM, got {period!r}")
    year, month = int(period[:4]), int(period[5:])
    start = datetime(year, month, 1, tzinfo=timezone.utc)
    end = datetime(year + (month == 12), month % 12 + 1, 1, tzinfo=timezone.utc)
    return start, end


def _tenant_key(name: str) -> str:
    """A store directory or file prefix -> 'tenant|', or leaf|unattributed
    when it is not a valid tenant participant (including the reserved 'leaf')."""
    try:
        validate_participant(name, "")
    except (TypeError, ValueError):
        return UNATTRIBUTED_KEY
    return f"{name}|"


def _upload_owner(name: str) -> str:
    # guest_uploads.staged_path: '<tenant>--<drawing><ext>'. The first '--'
    # splits, matching how the broker rebuilds the name from a bare tenant id.
    tenant, sep, rest = name.partition("--")
    if not sep or not tenant or not rest:
        return UNATTRIBUTED_KEY
    return _tenant_key(tenant)


def _git_owner(name: str) -> str:
    # customization_service._bare_repo: '<tenant>.git'
    if not name.endswith(".git") or len(name) <= 4:
        return UNATTRIBUTED_KEY
    return _tenant_key(name[:-4])


# --------------------------------------------------------------------------- #
# the bounded scan
# --------------------------------------------------------------------------- #
class _RootScan:
    """One root's bounded walk. Never follows a link; never raises OSError."""

    def __init__(self, cap: int, excluded: frozenset) -> None:
        self.cap = cap
        self.excluded = excluded
        self.entries = 0
        self.links = 0
        self.other = 0
        self.unreadable = 0
        self.nested = 0
        self.truncated = False
        self.bytes: Dict[str, int] = {}
        self._stack: List[Tuple[str, str]] = []

    def _take(self) -> bool:
        if self.entries >= self.cap:
            self.truncated = True
            return False
        self.entries += 1
        return True

    def _is_link(self, entry: os.DirEntry) -> bool:
        if entry.is_symlink():
            return True
        if _IS_WINDOWS:  # junctions: free on Windows, the scandir data carries it
            attrs = getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0)
            return bool(attrs & _REPARSE_POINT)
        return False

    def add(self, entry: os.DirEntry, owner: str) -> None:
        """Count one entry already charged against the cap."""
        try:
            if self._is_link(entry):
                self.links += 1
            elif entry.is_dir(follow_symlinks=False):
                if os.path.normcase(entry.path) in self.excluded:
                    self.nested += 1
                else:
                    self._stack.append((entry.path, owner))
            elif entry.is_file(follow_symlinks=False):
                size = entry.stat(follow_symlinks=False).st_size
                self.bytes[owner] = self.bytes.get(owner, 0) + size
            else:
                self.other += 1
        except OSError:
            self.unreadable += 1

    def children(self, path: str) -> Iterable[os.DirEntry]:
        """Entries of one directory, each charged against the cap."""
        try:
            with os.scandir(path) as it:
                for entry in it:
                    if not self._take():
                        return
                    yield entry
        except OSError:
            self.unreadable += 1

    def drain(self) -> None:
        while self._stack and not self.truncated:
            path, owner = self._stack.pop()
            for entry in self.children(path):
                self.add(entry, owner)
        self._stack.clear()


def _scan_root(kind: str, real_root: str, cap: int, excluded: frozenset) -> _RootScan:
    scan = _RootScan(cap, excluded)
    for entry in scan.children(real_root):
        if kind == "uploads":
            scan.add(entry, _upload_owner(entry.name))
        elif kind == "tenant_git":
            scan.add(entry, _git_owner(entry.name))
        elif kind == "marathon_runs":
            owner = UNATTRIBUTED_KEY
            try:
                if entry.is_dir(follow_symlinks=False):
                    owner = _tenant_key(entry.name)
            except OSError:
                pass
            scan.add(entry, owner)
        elif kind == "drawings" and entry.name == "tenants":
            try:
                plain_dir = entry.is_dir(follow_symlinks=False) and not scan._is_link(entry)
            except OSError:
                plain_dir = False
            if not plain_dir:
                scan.add(entry, UNATTRIBUTED_KEY)
                continue
            for tenant_entry in scan.children(entry.path):
                scan.add(tenant_entry, _tenant_key(tenant_entry.name))
                scan.drain()  # finish one tenant before listing the next: the stack stays one tenant deep
        else:
            scan.add(entry, UNATTRIBUTED_KEY)
        scan.drain()
    scan.drain()  # a 'continue' above can leave one pushed directory
    return scan


def roots_from_env(env: Optional[Mapping[str, str]] = None) -> Dict[str, Optional[str]]:
    """Each root kind -> its configured path, or None when the env var is unset."""
    env = os.environ if env is None else env
    return {kind: (env.get(var, "").strip() or None) for kind, var in ROOT_ENVS.items()}


def snapshot(roots: Mapping[str, Optional[str]], now: datetime, *,
             max_entries: int = MAX_ENTRIES_PER_ROOT) -> dict:
    """Bytes per tenant across the configured roots at `now`. JSON-able, no floats."""
    if not isinstance(roots, Mapping):
        raise TypeError("roots must map a root kind to a path or None")
    unknown = set(roots) - set(ROOT_ENVS)
    if unknown:
        raise ValueError(f"unknown root kind(s): {sorted(unknown)}")
    if isinstance(max_entries, bool) or not isinstance(max_entries, int) or max_entries < 1:
        raise ValueError("max_entries must be a positive int")
    taken_at = format_timestamp(now)

    real: Dict[str, str] = {}
    for kind in ROOT_ENVS:
        path = roots.get(kind)
        if path:
            real[kind] = os.path.realpath(os.fspath(path))

    totals: Dict[str, int] = {}
    report: Dict[str, dict] = {}
    complete = True
    skipped_total = 0
    for kind in ROOT_ENVS:
        if kind not in real:
            report[kind] = {"status": "unset"}
            continue
        root = real[kind]
        try:
            st = os.stat(root)
        except FileNotFoundError:
            report[kind] = {"status": "missing"}
            complete = False
            continue
        except OSError:
            report[kind] = {"status": "unreadable"}
            complete = False
            continue
        if not stat_mod.S_ISDIR(st.st_mode):
            report[kind] = {"status": "not_a_directory"}
            complete = False
            continue
        excluded = frozenset(os.path.normcase(p) for k, p in real.items()
                             if k != kind and p != root)
        scan = _scan_root(kind, root, max_entries, excluded)
        for key, size in scan.bytes.items():
            totals[key] = totals.get(key, 0) + size
        skipped = scan.links + scan.other + scan.unreadable + scan.nested
        skipped_total += skipped
        if scan.truncated or scan.unreadable:
            complete = False
        report[kind] = {
            "status": "truncated" if scan.truncated else "measured",
            "entries_scanned": scan.entries,
            "skipped_links": scan.links,
            "skipped_other": scan.other,
            "skipped_unreadable": scan.unreadable,
            "skipped_nested_roots": scan.nested,
        }

    return {
        "kind": SNAPSHOT_KIND,
        "schema": SNAPSHOT_SCHEMA,
        "taken_at": taken_at,
        "bytes": {key: str(totals[key]) for key in sorted(totals)},
        "total_bytes": str(sum(totals.values())),
        "complete": complete,
        "skipped_entries": skipped_total,
        "roots": report,
    }


# --------------------------------------------------------------------------- #
# the monthly observation
# --------------------------------------------------------------------------- #
def _parse_line(line: Any) -> Tuple[datetime, Dict[str, int], bool]:
    """One snapshot line (str or dict) -> (taken_at, bytes by key, complete). Raises on malformed."""
    if isinstance(line, (bytes, bytearray)):
        line = bytes(line).decode("utf-8")
    if isinstance(line, str):
        if len(line) > MAX_LINE_BYTES:
            raise ValueError("snapshot line too long")
        body = json.loads(line)
    else:
        body = line
    if not isinstance(body, Mapping) or body.get("kind") != SNAPSHOT_KIND:
        raise ValueError("not a storage snapshot")
    taken_at = parse_timestamp(body.get("taken_at"))
    raw = body.get("bytes")
    if not isinstance(raw, Mapping):
        raise ValueError("bytes must be an object")
    out: Dict[str, int] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or key.count("|") != 1:
            raise ValueError("bad participant key")
        validate_participant(*key.split("|"))
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise ValueError("byte counts must be integer strings")
        if isinstance(value, str) and not (value.isascii() and value.isdigit()):
            raise ValueError("byte counts must be integer strings")
        count = int(value)
        if count < 0:
            raise ValueError("byte counts must be >= 0")
        out[key] = count
    return taken_at, out, body.get("complete") is not False


def storage_usage_observation(period: str, snapshot_lines: Iterable[Any], *,
                              now: Optional[datetime] = None) -> dict:
    """One ESTIMATED gb-month usage observation for the period from snapshot lines.

    Each sample holds until the next one (a step function); the newest sample at
    or before the period start carries into it; the last sample holds to the
    period end, or to `now` while the period is open. GB-months are byte-seconds
    over (10^9 bytes x the period's seconds). Coverage is partial when any gap
    without a sample exceeds 48 hours, a line was malformed or incomplete, or the
    period is still open; unknown (total null) when nothing covers the period.
    """
    start, end = period_bounds(period)
    if now is None:
        now = datetime.now(timezone.utc)
    elif not isinstance(now, datetime) or now.tzinfo is None:
        raise ValueError("now must be a timezone-aware datetime")
    effective_end = min(end, now)

    partial = effective_end < end
    samples: List[Tuple[datetime, int, Dict[str, int]]] = []
    carry: Optional[Tuple[datetime, int, Dict[str, int]]] = None
    read = 0
    for order, line in enumerate(snapshot_lines):
        if read >= MAX_SNAPSHOT_LINES:
            partial = True
            break
        read += 1
        try:
            taken_at, counts, complete = _parse_line(line)
        except (ValueError, TypeError, UnicodeDecodeError):
            partial = True
            continue
        if taken_at >= effective_end:
            continue
        if taken_at <= start:
            if carry is None or (taken_at, order) >= (carry[0], carry[1]):
                carry = (taken_at, order, counts)
            continue
        if not complete:
            partial = True
        samples.append((taken_at, order, counts))

    samples.sort(key=lambda s: (s[0], s[1]))
    timeline = ([carry] if carry is not None else []) + samples
    source = f"{SOURCE}:snapshots={len(timeline)}"
    if effective_end <= start or not timeline:
        return _observation(period, None, {}, "unknown", source)

    # Gaps: leading (no carry-in), between samples, trailing to the effective end.
    if carry is None and timeline[0][0] - start > MAX_GAP:
        partial = True
    for (t0, _, _), (t1, _, _) in zip(timeline, timeline[1:]):
        if t1 - t0 > MAX_GAP:
            partial = True
    if effective_end - timeline[-1][0] > MAX_GAP:
        partial = True

    byte_seconds: Dict[str, int] = {}
    for i, (taken_at, _, counts) in enumerate(timeline):
        seg_start = max(taken_at, start)
        seg_end = timeline[i + 1][0] if i + 1 < len(timeline) else effective_end
        seconds = int((seg_end - seg_start).total_seconds())
        if seconds <= 0:
            continue
        for key, count in counts.items():
            byte_seconds[key] = byte_seconds.get(key, 0) + count * seconds

    period_seconds = int((end - start).total_seconds())
    denominator = Decimal(BYTES_PER_GB * period_seconds)
    usages: Dict[str, str] = {}
    total = Decimal(0)
    with localcontext() as ctx:
        ctx.prec = 80
        for key in sorted(byte_seconds):
            value = (Decimal(byte_seconds[key]) / denominator).quantize(
                USAGE_QUANTUM, rounding=ROUND_HALF_EVEN)
            usages[key] = format(value, "f")
            total += value  # the total IS the sum of the rounded parts, so compute_shares never sees usage > total
        total_text = format(total.quantize(USAGE_QUANTUM), "f")
    return _observation(period, total_text, usages, "partial" if partial else "complete", source)


def _observation(period: str, total: Optional[str], usages: Dict[str, str],
                 coverage: str, source: str) -> dict:
    return {
        "kind": "usage",
        "resource_id": RESOURCE_ID,
        "period": period,
        "unit": UNIT,
        "total_usage": total,
        "usages": usages,
        "status": ESTIMATED,
        "coverage": coverage,
        "source": source,
    }
