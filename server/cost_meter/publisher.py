"""Publish one month of the resource share ledger (TCM-09a).

Transparency of Leaf's real cost only, never billing: nothing here feeds Stripe,
quotas or caps. build_period joins the collectors' JSON-able usage and cost
observations (aws_import, vendors, storage, direct_usage.aps_usage_observation)
by resource_id into (ResourcePeriod, shares) pairs; publish_period appends each
as a revision (identical content is a no-op, per CostLedgerStore) and freezes the
month with store.publish.

Beside the store's immutable manifests, publish_period writes two small files:
  publication-meta/<publication_id>.json  which sources were used and missing (atomic replace)
  latest/<YYYY-MM>.json                   the newest publication of the month (atomic replace)
so a reader finds the current publication in O(1) instead of scanning every manifest.

Fails closed: an observation of the wrong kind or period, a float amount or a
malformed participant key raises ValueError or TypeError, and nothing is written.
"""
from __future__ import annotations

import json
import logging
import math
import os
import socket
import time
import uuid
from contextlib import contextmanager, nullcontext
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .ledger import (
    COVERAGES, ESTIMATED, LEAF, MEASURED, PERIOD_RE, RESOURCE_ID_RE, SHARE_ONE, STATUSES,
    ResourcePeriod, ShareEntry, compute_shares, to_decimal,
)
from .store import PUBLICATION_ID_RE, CostLedgerStore, _canon_json, _write_atomic

META_SCHEMA = "leaf.cost-share-publication-meta.v1"
LATEST_SCHEMA = "leaf.cost-share-latest.v1"
META_DIR = "publication-meta"
LATEST_DIR = "latest"
PUBLICATIONS_DIR = "publications"
UNMETERED_UNIT = "unmetered"  # a resource with cost but no usage observation
MAX_OBSERVATIONS = 100_000  # bounds one publish; exceeding it raises, never truncates
MAX_SCAN_PUBLICATIONS = 5_000  # bounds the fallback manifest scan when no latest pointer exists
_MAX_META_BYTES = 256 * 1024
_MAX_SOURCES = 64
_MAX_PHYSICAL_USAGE = 256  # resources with a physical quantity in one publication
_MAX_UNIT = 64
PUBLISH_LOCK = ".publisher.lock"
_LOG = logging.getLogger(__name__)


class LedgerBusy(RuntimeError):
    """Another publisher holds the ledger past the configured wait timeout."""


def _pid_alive(pid: int) -> bool:
    if os.name == "nt":
        # os.kill(pid, 0) can terminate a Windows process; query its handle instead.
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return ctypes.get_last_error() != 87  # only invalid PID proves absence
        try:
            code = wintypes.DWORD()
            kernel.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
            return not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True  # permission failures are not proof of death
    return True


@contextmanager
def _lock_guard(root: Path):
    """Serialize lock-file changes, including competing stale takeovers.

    This empty guard inode is permanent: deleting it would split OS lock holders.
    The OS releases its byte lock automatically if a process crashes.
    """
    with open(root / ".publisher.guard", "a+b") as fh:
        if os.name == "nt":
            import msvcrt
            try:
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                yield False
                return
            try:
                yield True
            finally:
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
            try:
                yield True
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _remove_owned_lock(path: Path, content: bytes, fh) -> None:
    """Remove matching content only on the inode whose OS lock we hold."""
    try:
        fh.seek(0)
        if os.path.samestat(path.stat(), os.fstat(fh.fileno())) and fh.read() == content:
            path.unlink()
    except FileNotFoundError:
        pass


def _open_lock_file(path: Path, create: bool = False):
    if os.name == "nt":
        # Deletion must remain possible while our byte lock is held.
        import ctypes
        import msvcrt
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                      wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                                      wintypes.HANDLE)
        kernel.CreateFileW.restype = wintypes.HANDLE
        handle = kernel.CreateFileW(str(path), 0xC0000000, 7, None,
                                    1 if create else 3, 0x80, None)
        if handle == wintypes.HANDLE(-1).value:
            error = ctypes.get_last_error()
            if error in (80, 183):
                raise FileExistsError(str(path))
            if error in (2, 3):
                raise FileNotFoundError(str(path))
            raise ctypes.WinError(error)
        fd = msvcrt.open_osfhandle(handle, os.O_RDWR | os.O_BINARY)
    else:
        flags = os.O_RDWR | (os.O_CREAT | os.O_EXCL if create else 0)
        fd = os.open(path, flags, 0o600)
    return os.fdopen(fd, "r+b")


def _try_owner_lock(fh) -> bool:
    fh.seek(0)
    if os.name == "nt":
        import msvcrt
        # Windows byte locks also prohibit I/O through other descriptors. Lock
        # beyond the bounded metadata so readers can still inspect the owner.
        fh.seek(_MAX_META_BYTES)
        try:
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        finally:
            fh.seek(0)
    else:
        import fcntl
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
    return True


def _lock_expired(owner: Optional[dict], stale_after: float) -> bool:
    if owner is None:
        return False
    started = owner.get("start_time")
    return (type(started) in (int, float) and math.isfinite(started)
            and time.time() - started > stale_after)


def _stale_lock(owner: Optional[dict], stale_after: float) -> bool:
    if owner is None:
        return False
    pid = owner.get("pid")
    return (owner.get("host") == socket.gethostname()
            and type(pid) is int and 0 < pid <= 0xFFFFFFFF
            and _lock_expired(owner, stale_after) and not _pid_alive(pid))


@contextmanager
def _publisher_lock(root: Path, timeout: float, stale_after: float):
    if any(not math.isfinite(v) or v < 0 for v in (timeout, stale_after)):
        raise ValueError("lock timeout and stale threshold must be finite and nonnegative")
    root.mkdir(parents=True, exist_ok=True)
    path = root / PUBLISH_LOCK
    deadline = time.monotonic() + timeout
    delay = 0.01
    content = _canon_json({"pid": os.getpid(), "host": socket.gethostname(),
                           "start_time": time.time(), "token": uuid.uuid4().hex})
    while True:
        acquired = False
        # A pre-existing fresh owner without a guard must also time out without
        # creating any files. Normal publishers already have the permanent guard.
        held_without_guard = (not (root / ".publisher.guard").exists() and path.exists()
                              and not _lock_expired(_read_json(path), stale_after))
        with (nullcontext(False) if held_without_guard else _lock_guard(root)) as guarded:
            if guarded:
                old = _read_json(path)
                if _stale_lock(old, stale_after) or _lock_expired(old, stale_after):
                    try:
                        previous = _open_lock_file(path)
                    except FileNotFoundError:
                        pass
                    else:
                        with previous:
                            if _try_owner_lock(previous) and _lock_expired(
                                    _read_json(path), stale_after):
                                _remove_owned_lock(path, previous.read(), previous)
                                _LOG.warning("Taking over stale publisher lock: pid=%s host=%s start_time=%s",
                                             old.get("pid"), old.get("host"), old.get("start_time"))
                try:
                    fh = _open_lock_file(path, create=True)
                except FileExistsError:
                    pass
                else:
                    owner_locked = False
                    try:
                        owner_locked = _try_owner_lock(fh)
                        if not owner_locked:
                            raise LedgerBusy("Could not lock newly created publisher file")
                        fh.write(content)
                        fh.flush()
                        os.fsync(fh.fileno())
                    except BaseException:
                        # Only unlink if this descriptor owns the OS lock.
                        if owner_locked:
                            path.unlink()
                        fh.close()
                        raise
                    acquired = True
        if acquired:
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise LedgerBusy(f"Cost ledger publisher busy at {root}; timed out after {timeout}s")
        time.sleep(min(delay, remaining))
        delay = min(delay * 2, 0.5)
    try:
        yield
    finally:
        try:
            while True:
                with _lock_guard(root) as guarded:
                    if guarded:
                        _remove_owned_lock(path, content, fh)
                        break
                time.sleep(0.01)
        finally:
            fh.close()  # releases the OS lock, including on process death

# Weakest first: cost coverage is capped by the selected usage observation.
_COVERAGE_RANK = {"unknown": 0, "partial": 1, "complete": 2}


def _check_period(period: Any) -> str:
    if not isinstance(period, str) or not PERIOD_RE.fullmatch(period):
        raise ValueError(f"period must be YYYY-MM, got {period!r}")
    return period


def _usage_map(usages: Any, resource_id: str) -> Dict[Tuple[str, str], Decimal]:
    """{"participant|dimension": amount} to {(participant, dimension): Decimal}. Fails closed."""
    if not isinstance(usages, Mapping):
        raise TypeError(f"{resource_id} usages must be an object")
    out: Dict[Tuple[str, str], Decimal] = {}
    for key, value in usages.items():
        if not isinstance(key, str) or key.count("|") != 1:
            raise ValueError(f"{resource_id} usage key must be 'participant|dimension', got {key!r}")
        participant, dimension = key.split("|")
        out[(participant, dimension)] = to_decimal(value, f"{resource_id} usage {key}")
    return out


def _pick_usage(candidates: Sequence[Mapping[str, Any]]) -> Optional[Mapping[str, Any]]:
    """At most one usage observation per resource: MEASURED over ESTIMATED, then the first."""
    for obs in candidates:
        if obs.get("status") == MEASURED:
            return obs
    return candidates[0] if candidates else None


def _validated(observations: Iterable[Any], period: str) -> List[Mapping[str, Any]]:
    out: List[Mapping[str, Any]] = []
    for index, obs in enumerate(observations):
        if index >= MAX_OBSERVATIONS:
            raise ValueError(f"more than {MAX_OBSERVATIONS} observations in one publish")
        if not isinstance(obs, Mapping):
            raise TypeError(f"observation {index} must be an object")
        kind = obs.get("kind")
        if kind not in ("usage", "cost"):
            raise ValueError(f"observation {index} kind must be usage or cost, got {kind!r}")
        if obs.get("period") != period:
            raise ValueError(f"observation {index} is for {obs.get('period')!r}, not {period}")
        if not isinstance(obs.get("resource_id"), str):
            raise TypeError(f"observation {index} has no resource_id")
        if kind == "usage" and obs.get("status") not in STATUSES:
            raise ValueError(f"observation {index} status must be one of {sorted(STATUSES)}")
        out.append(obs)
    return out


def build_period(period: str, observations: Iterable[Any]) -> List[Tuple[ResourcePeriod, List[ShareEntry]]]:
    """Join usage to cost by resource_id; one (ResourcePeriod, shares) per resource, sorted by id.

    cost: gross and credits summed over the resource's cost observations, coverage the
    weakest of them, every source batch listed. usage: one observation (MEASURED first).
    Cost without usage puts the whole resource on (leaf, unattributed) ESTIMATED; usage
    without cost is gross 0 with coverage unknown, and still appears.
    """
    period = _check_period(period)
    costs: Dict[str, List[Mapping[str, Any]]] = {}
    usages: Dict[str, List[Mapping[str, Any]]] = {}
    for obs in _validated(observations, period):
        (costs if obs["kind"] == "cost" else usages).setdefault(obs["resource_id"], []).append(obs)

    out: List[Tuple[ResourcePeriod, List[ShareEntry]]] = []
    for resource_id in sorted(set(costs) | set(usages)):
        cost_obs = costs.get(resource_id, [])
        usage = _pick_usage(usages.get(resource_id, []))
        gross = Decimal(0)
        credits = Decimal(0)
        batches: List[str] = []
        coverage = "unknown"
        if cost_obs:
            coverage = "complete"
            seen = set()
            batch_amounts = {}
            for obs in cost_obs:
                obs_gross = to_decimal(obs.get("gross_cost_usd"), f"{resource_id} gross_cost_usd")
                obs_credits = to_decimal(obs.get("credits_usd", "0"), f"{resource_id} credits_usd")
                obs_coverage = obs.get("coverage")
                if obs_coverage not in _COVERAGE_RANK:
                    raise ValueError(f"{resource_id} coverage must be one of {sorted(_COVERAGE_RANK)}")
                if _COVERAGE_RANK[obs_coverage] < _COVERAGE_RANK[coverage]:
                    coverage = obs_coverage
                batch = obs.get("source_batch_id")
                if isinstance(batch, str) and batch.strip():
                    amounts = (obs_gross, obs_credits)
                    if batch in batch_amounts:
                        if batch_amounts[batch] != amounts:
                            raise ValueError(f"{resource_id} conflicting amounts for batch {batch}")
                        continue
                    batch_amounts[batch] = amounts
                fingerprint = _canon_json(obs)
                if fingerprint in seen:
                    continue
                seen.add(fingerprint)
                gross += obs_gross
                credits += obs_credits
                batch = obs.get("source_batch_id")
                if isinstance(batch, str) and batch.strip() and batch not in batches:
                    batches.append(batch)
        if usage is not None:
            usage_coverage = usage.get("coverage", "unknown")
            if usage_coverage not in _COVERAGE_RANK:
                raise ValueError(f"{resource_id} usage coverage must be one of {sorted(_COVERAGE_RANK)}")
            if _COVERAGE_RANK[usage_coverage] < _COVERAGE_RANK[coverage]:
                coverage = usage_coverage
        unit = usage.get("unit") if usage is not None else UNMETERED_UNIT
        resource_period = ResourcePeriod(
            resource_id=resource_id,
            period=period,
            unit=unit if isinstance(unit, str) and unit.strip() else UNMETERED_UNIT,
            total_usage=usage.get("total_usage") if usage is not None else None,
            gross_cost_usd=gross,
            credits_usd=credits,
            source_batch_ids=tuple(batches),
            coverage=coverage,
        )
        if usage is None:
            shares = [ShareEntry(LEAF, "unattributed", None, SHARE_ONE, ESTIMATED)]
        else:
            shares = compute_shares(resource_period, _usage_map(usage.get("usages") or {}, resource_id),
                                    usage["status"])
        out.append((resource_period, shares))
    return out


# --------------------------------------------------------------------------- #
# publication metadata: which sources went missing, and the month's newest id
# --------------------------------------------------------------------------- #
def _root(store: CostLedgerStore) -> Path:
    return store._require_enabled()


def _source_list(values: Iterable[Any], what: str) -> List[str]:
    out: List[str] = []
    for value in values:
        if not isinstance(value, str) or not value.strip() or len(value) > 256:
            raise ValueError(f"{what} entries must be 1..256 character strings")
        if value not in out:
            out.append(value)
        if len(out) > _MAX_SOURCES:
            raise ValueError(f"more than {_MAX_SOURCES} {what}")
    return sorted(out)


def _physical_usage_entry(entry: Any) -> Optional[Dict[str, str]]:
    """{quantity, unit, coverage} with a nonnegative decimal-string quantity, else None."""
    if not isinstance(entry, Mapping) or set(entry) != {"quantity", "unit", "coverage"}:
        return None
    quantity, unit, coverage = entry["quantity"], entry["unit"], entry["coverage"]
    if not isinstance(quantity, str) or not isinstance(unit, str):
        return None
    if not unit.strip() or len(unit) > _MAX_UNIT or coverage not in COVERAGES:
        return None
    try:
        if to_decimal(quantity, "quantity") < 0:
            return None
    except (TypeError, ValueError, ArithmeticError):
        return None
    return {"quantity": quantity, "unit": unit, "coverage": coverage}


def _physical_usage_map(value: Any) -> Dict[str, Dict[str, str]]:
    """{resource_id: {quantity, unit, coverage}} sorted by resource_id. Fails closed."""
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise TypeError("physical_usage must be an object")
    if len(value) > _MAX_PHYSICAL_USAGE:
        raise ValueError(f"physical_usage names more than {_MAX_PHYSICAL_USAGE} resources")
    out: Dict[str, Dict[str, str]] = {}
    for resource_id, entry in value.items():
        if not isinstance(resource_id, str) or not RESOURCE_ID_RE.fullmatch(resource_id):
            raise ValueError(f"physical_usage resource id is invalid: {resource_id!r}")
        checked = _physical_usage_entry(entry)
        if checked is None:
            raise ValueError(f"{resource_id} physical_usage must be {{quantity, unit, coverage}} "
                             "with a nonnegative decimal-string quantity")
        out[resource_id] = checked
    return dict(sorted(out.items()))


def publish_period(store: CostLedgerStore, period: str, observations: Iterable[Any], reason: str, *,
                   sources: Iterable[str] = (), missing_sources: Iterable[str] = (),
                   physical_usage: Optional[Mapping[str, Any]] = None,
                   lock_timeout: float = 300, stale_after: float = 1800) -> str:
    """Append every resource's revision, then publish the month. Returns the publication id.

    Re-publishing identical input appends nothing and returns the same id. The
    publication's membership defines its immutable identity; source health metadata
    is replaced on every publish, and latest/<period>.json atomically points at the id.
    physical_usage ({resource_id: {quantity, unit, coverage}}) is display metadata
    only: it lands in the publication metadata and never touches shares, amounts,
    coverage or revision digests. An empty map clears earlier physical usage metadata.
    lock_timeout and stale_after are seconds. Expired locks from any host are
    reclaimed only while holding their OS lock; live owners cannot be displaced.
    """
    period = _check_period(period)
    used = _source_list(sources, "sources")
    missing = _source_list(missing_sources, "missing sources")
    physical = _physical_usage_map(physical_usage)
    pairs = build_period(period, observations)
    root = _root(store)
    with _publisher_lock(root, lock_timeout, stale_after):
        return _publish_period_locked(store, period, pairs, reason, used, missing, physical)


def _publish_period_locked(store: CostLedgerStore, period: str,
                           pairs: List[Tuple[ResourcePeriod, List[ShareEntry]]],
                           reason: str, used: List[str], missing: List[str],
                           physical: Optional[Dict[str, Dict[str, str]]] = None) -> str:
    root = _root(store)
    current_ids = {rp.resource_id for rp, _ in pairs}
    previous_id = latest_publication_id(store, period) if missing else None
    carried = [r for r in store.read_publication(previous_id)
               if r.resource_id not in current_ids] if previous_id else []
    revision_ids = [r.revision_id for r in carried]
    for resource_period, shares in pairs:
        revision_ids.append(store.append_revision(resource_period, shares, reason))
    metadata = {"sources": used, "missing_sources": missing,
                "carried_forward": sorted(r.resource_id for r in carried)}
    if physical:
        metadata["physical_usage"] = physical
    publication_id = store.publish(period, revision_ids=revision_ids)

    meta_path = root / META_DIR / f"{publication_id}.json"
    _write_atomic(meta_path, _canon_json({
        "schema": META_SCHEMA,
        "publication_id": publication_id,
        "period": period,
        **metadata,
    }) + b"\n")
    _write_atomic(root / LATEST_DIR / f"{period}.json", _canon_json({
        "schema": LATEST_SCHEMA,
        "period": period,
        "publication_id": publication_id,
    }) + b"\n")
    return publication_id


def _read_json(path: Path) -> Optional[dict]:
    """A small JSON object, or None when absent, oversized or malformed."""
    try:
        with open(path, "rb") as fh:
            raw = fh.read(_MAX_META_BYTES + 1)
    except OSError:
        return None
    if len(raw) > _MAX_META_BYTES:
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _publication_id_ok(publication_id: Any, period: str) -> bool:
    if not isinstance(publication_id, str):
        return False
    match = PUBLICATION_ID_RE.fullmatch(publication_id)
    return bool(match) and match.group(1) == period


def latest_publication_id(store: CostLedgerStore, period: str) -> Optional[str]:
    """The newest publication of the month, or None. Disabled store reads as None.

    The latest pointer answers in O(1); a month published without publish_period
    falls back to one bounded scan of its manifests, newest published_at wins.
    """
    period = _check_period(period)
    if not store.enabled:
        return None
    root = _root(store)
    pointer = _read_json(root / LATEST_DIR / f"{period}.json")
    if pointer is not None and _publication_id_ok(pointer.get("publication_id"), period):
        if (root / PUBLICATIONS_DIR / f"{pointer['publication_id']}.json").is_file():
            return pointer["publication_id"]
    best: Optional[Tuple[str, int, str]] = None
    prefix = f"pub-{period}-"
    try:
        with os.scandir(root / PUBLICATIONS_DIR) as it:
            for count, entry in enumerate(it):
                if count >= MAX_SCAN_PUBLICATIONS:
                    break
                stem = entry.name[:-5] if entry.name.endswith(".json") else ""
                if not stem.startswith(prefix) or not _publication_id_ok(stem, period):
                    continue
                manifest = _read_json(Path(entry.path))
                published_at = manifest.get("published_at") if manifest else None
                if not isinstance(published_at, str):
                    continue
                key = (published_at, entry.stat().st_mtime_ns, stem)
                if best is None or key > best:
                    best = key
    except OSError:
        return None
    return best[2] if best else None


def publication_info(store: CostLedgerStore, publication_id: str) -> Dict[str, Any]:
    """published_at from the manifest, and the sources and physical usage recorded beside it
    (empty when unrecorded)."""
    root = _root(store)
    match = PUBLICATION_ID_RE.fullmatch(publication_id or "")
    if not match:
        raise ValueError(f"invalid publication id: {publication_id!r}")
    manifest = _read_json(root / PUBLICATIONS_DIR / f"{publication_id}.json") or {}
    meta = _read_json(root / META_DIR / f"{publication_id}.json") or {}
    if meta.get("publication_id") != publication_id:
        legacy = manifest.get("metadata")
        meta = {**legacy, "publication_id": publication_id} if isinstance(legacy, dict) else {}
    published_at = manifest.get("published_at")
    missing = meta.get("missing_sources") if meta.get("publication_id") == publication_id else None
    sources = meta.get("sources") if meta.get("publication_id") == publication_id else None
    carried = meta.get("carried_forward") if meta.get("publication_id") == publication_id else None
    physical = meta.get("physical_usage") if meta.get("publication_id") == publication_id else None
    physical_usage: Dict[str, Dict[str, str]] = {}
    if isinstance(physical, dict):
        for resource_id, entry in physical.items():
            checked = _physical_usage_entry(entry)
            if isinstance(resource_id, str) and checked is not None:
                physical_usage[resource_id] = checked
    return {
        "published_at": published_at if isinstance(published_at, str) else None,
        "missing_sources": [s for s in missing if isinstance(s, str)] if isinstance(missing, list) else [],
        "sources": [s for s in sources if isinstance(s, str)] if isinstance(sources, list) else [],
        "carried_forward": [s for s in carried if isinstance(s, str)] if isinstance(carried, list) else [],
        "physical_usage": physical_usage,
    }
