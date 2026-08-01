"""
Write loop (M2): wire the proven drawing.write capability into the PRODUCT loop.

A registered `drawing.write` tool run produces a NEW immutable drawing version in
the versioned store (da/store.py), with undo/redo, working offline (APS_LIVE=0)
and live (APS_LIVE=1). This module is the WRITE BRANCH of the execution chain —
server/broker.py delegates here for any tool whose package declares
`capabilities: ["drawing.write"]`; every read tool takes the unchanged path.

Two representations of a stored version (documented in CONTRACT-ADDENDUM §11):

  * APS_LIVE=0 (mock): a version's payload IS the intake JSON. The write tool's
    `run(intake, params)` returns additive `result.mutations` with `added`,
    `removed`, and/or first-class `transforms`; the chain applies them to the
    CURRENT version's intake -> new intake -> put_drawing.
  * APS_LIVE=1 (live): a version's payload is real DWG bytes; a sibling
    `*.intake.json` cache key holds the re-extracted intake. The chain executes
    the authored planner in the sandbox, validates and lowers its mutation data,
    and sends only that closed plan to the fixed LeafApplyMutations Activity.

Credential discipline: this module NEVER imports da.* at top level. The live
path receives the credential-holding `da` client from the broker (the only
process allowed to hold it), mirroring server/tool_loader.py.
"""
from __future__ import annotations

import copy
import hashlib
import hmac
import json
import logging
import math
import os
import re
import secrets
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from envelopes import DEFAULT_HTTP_STATUS, ErrorCode, err_envelope, ok_envelope
from mutation_plan import (
    canonical_json_bytes,
    emit_plan,
    plan_sha256,
    validate_mutations,
)
from tenant_id_validator import validate_tenant_id

SERVER_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SERVER_DIR.parent
CACHED_INTAKE_PATH = PROJECT_ROOT / "data" / "rooftop_demo.intake.json"
DEMO_DWG_PATH = PROJECT_ROOT / "data" / "rooftop_demo.dwg"
LOGGER = logging.getLogger(__name__)

# Make da/store.py importable. APPEND (never front-insert) so a stdlib module is
# never shadowed by a da/ sibling (e.g. da/queue.py); `store` is da-only and still
# resolves. store.py itself front-inserts da/ once imported — the existing,
# accepted behavior of every code path that touches the store.
_DA_DIR = str(PROJECT_ROOT / "da")
if _DA_DIR not in sys.path:
    sys.path.append(_DA_DIR)

# Well-known demo drawing bootstrapped from the cached intake at APS_LIVE=0.
DEMO_DRAWING_ID = "demo"
# Reserved tenant-id namespace for ephemeral guest uploads (server/guest_uploads.py
# mints these; server-side ONLY). The prefix is load-bearing for two fail-closed
# rules: guest tenants store in an isolated, purgeable filesystem root
# (backend_for_tenant), and ensure_demo_drawing NEVER bootstraps a drawing for
# them (a guest has no drawings except what extraction actually produced).
GUEST_TENANT_PREFIX = "guest-"
# Fixed reviewed Activity. Tenant-authored code can produce data only; it can
# never select an Activity or supply executable input to Design Automation.
WRITE_ACTIVITY = "LeafApplyMutations"

USD_PER_HR = float(os.environ.get("APS_USD_PER_HR", "10"))


class ProofStateUnreadable(RuntimeError):
    """A version's cache-proof state could not be READ (transport), so the
    reader can neither trust nor refute the cache. Deliberately NOT a
    ValueError: the route family maps (KeyError, ValueError) to non-retryable
    404/400 ("that version does not exist"), and this is a different answer —
    the version exists, the store is just unreachable right now. Callers
    surface it as a retryable 503."""


# --------------------------------------------------------------------------- #
# tool classification + backend selection
# --------------------------------------------------------------------------- #
def is_write_tool(tool: Dict[str, Any]) -> bool:
    """True iff the tool package declares the drawing.write capability (§2)."""
    caps = (tool or {}).get("capabilities") or []
    return "drawing.write" in caps


def store_dir() -> str:
    """Local filesystem store root (gitignored). Env LEAF_STORE_DIR overrides so
    tests and the app+broker pair can share an isolated directory."""
    return os.environ.get("LEAF_STORE_DIR", str(SERVER_DIR / "drawings"))


def blob_store_mode() -> str:
    """Return the configured durable blob location, independent of APS use."""
    mode = os.environ.get("LEAF_BLOB_STORE", "legacy").strip().lower()
    if mode not in {"legacy", "filesystem", "aps_oss"}:
        raise RuntimeError(
            "LEAF_BLOB_STORE must be 'legacy', 'filesystem', or 'aps_oss'")
    return mode


def _cleanup_scratch_objects(da: Any, scratch_keys) -> None:
    """Best-effort immediate cleanup with nonsecret failure telemetry."""
    if not scratch_keys:
        return
    delete = (
        getattr(da, "delete_scratch_object", None)
        or getattr(da, "delete_object", None)
    )
    if delete is None:
        for key in scratch_keys:
            LOGGER.warning(
                "APS scratch cleanup unavailable",
                extra={
                    "scratch_key_sha256": hashlib.sha256(
                        str(key).encode("utf-8")
                    ).hexdigest(),
                    "error_type": "DeleteMethodUnavailable",
                },
            )
        return
    for key in scratch_keys:
        try:
            delete(key)
        except Exception as exc:  # noqa: BLE001 - cleanup stays best effort
            LOGGER.warning(
                "APS scratch cleanup failed",
                extra={
                    "scratch_key_sha256": hashlib.sha256(
                        str(key).encode("utf-8")
                    ).hexdigest(),
                    "error_type": type(exc).__name__,
                },
            )


def fence_open() -> bool:
    """Live cutover state from the shared EFS fence FILE alone.

    The fence is the one authority EVERY drawing-authority lane shares, so a
    storage cutover drains already-running app and broker tasks without waiting
    for an ECS rollout. No fence configured means no cutover is in progress.
    Missing, unreadable, or malformed fence state fails CLOSED.
    """
    fence = os.environ.get("LEAF_DRAWING_MUTATIONS_FENCE_FILE", "").strip()
    if not fence:
        return True
    try:
        return Path(fence).read_text(encoding="utf-8").strip() == "1"
    except (OSError, UnicodeError):
        return False


def drawing_mutations_enabled() -> bool:
    """Authored / checkout / broker-run lane: the env default AND the fence.

    Scoped to THAT lane on purpose. The upload/import lane has its own env gate
    (``upload_import_mutations_enabled``) and must not be closed by this one --
    see ``upload_mutation_commit_guard``.
    """
    if os.environ.get("LEAF_DRAWING_MUTATIONS_ENABLED", "1") != "1":
        return False
    return fence_open()


@contextmanager
def _fence_held(decide):
    """Hold the shared cutover fence across one durable drawing commit.

    Linux tasks take a shared flock.  The protected cutover control takes the
    matching exclusive lock before changing the fence, so it waits for every
    admitted commit and prevents a late old-task write from crossing the drain.

    ``decide`` is the lane's own open/closed rule, evaluated INSIDE the lock so
    the answer cannot go stale between the check and the commit.
    """
    fence = os.environ.get("LEAF_DRAWING_MUTATIONS_FENCE_FILE", "").strip()
    if not fence:
        yield decide()
        return
    lock_path = f"{fence}.lock"
    Path(lock_path).parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+b") as lock_file:
        try:
            import fcntl  # Linux deployment; unavailable on Windows unit hosts.
        except ImportError:
            yield False
            return
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_SH)
        try:
            yield decide()
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


@contextmanager
def drawing_mutation_commit_guard():
    """Commit guard for the authored / checkout / broker-run lane."""
    with _fence_held(drawing_mutations_enabled) as commit_enabled:
        yield commit_enabled


@contextmanager
def upload_mutation_commit_guard():
    """Commit guard for the upload / import lane: ONLY the shared fence.

    That lane's env gate is ``upload_import_mutations_enabled``, checked by the
    caller. Folding ``LEAF_DRAWING_MUTATIONS_ENABLED`` in here would let an
    authored-lane drain silently close uploads too -- exactly the coupling
    ``test_upload_gate_is_independent_from_authored_mutation_gate`` forbids.
    The fence FILE still applies, because a storage cutover really does drain
    every lane.
    """
    with _fence_held(fence_open) as commit_enabled:
        yield commit_enabled


def upload_import_mutations_enabled() -> bool:
    """Fail-closed gate for upload admission and canonical upload import."""
    return os.environ.get("LEAF_UPLOAD_IMPORT_MUTATIONS_ENABLED", "0") == "1"


def default_backend(*, aps_live: bool = False, da: Any = None):
    """Pick storage independently from whether execution uses live APS.

    ``aps_live`` remains for caller compatibility. ``LEAF_BLOB_STORE`` owns
    the storage choice. APS OSS is available only with a broker-side client.
    """
    import store  # lazy (store imports da/client)
    mode = blob_store_mode()
    if mode == "aps_oss" or (mode == "legacy" and aps_live and da is not None):
        if da is None:
            raise RuntimeError(
                "LEAF_BLOB_STORE=aps_oss requires the broker-side APS client")
        return store.OSSBackend()
    return store.FilesystemBackend(store_dir())


def guest_store_dir() -> str:
    """Isolated store root for GUEST tenants only (gitignored, env-overridable).

    Guests get their own filesystem root — never the default backend — because
    the retention promise ("deleted after N hours") is only honest on a store
    the purge job can provably delete from, and the StorageBackend interface
    has no delete: guest_uploads.purge_expired() deletes at the FILESYSTEM
    level, which is only sound when every guest artifact lives under this one
    root and nothing else does."""
    return os.environ.get("LEAF_GUEST_STORE_DIR", str(SERVER_DIR / "guest_drawings"))


def backend_for_tenant(tenant_id: str, *, aps_live: bool = False, da: Any = None):
    """The store backend for ONE tenant: guest tenants -> the isolated local
    guest store (always filesystem, even at APS_LIVE=1 — see guest_store_dir);
    everyone else -> default_backend, byte-identical to before this seam."""
    import store  # lazy (store imports da/client)
    if str(tenant_id).startswith(GUEST_TENANT_PREFIX):
        return store.FilesystemBackend(guest_store_dir())
    return default_backend(aps_live=aps_live, da=da)


def upload_backend_for_tenant(tenant_id: str):
    """Credential-free backend for staged upload state and extracted artifacts.

    The app and broker share ``LEAF_STORE_DIR`` and ``LEAF_GUEST_STORE_DIR`` on
    the durable drawings volume. APS extraction crosses the broker HTTP seam,
    but upload marker, raw file, and intake-cache persistence stays on that
    shared volume. The tenant-facing app must never construct an OSS backend,
    because doing so loads the broker-only APS credential.
    """
    return backend_for_tenant(tenant_id, aps_live=False, da=None)


def _drawing_id(params: Dict[str, Any]) -> str:
    did = (params or {}).get("drawing_id")
    return str(did) if did else DEMO_DRAWING_ID


def target_drawing_id(params: Dict[str, Any]) -> str:
    """The store drawing a run will WRITE, from its params.

    Public because the /api/run route has to resolve the same drawing this
    module will publish to, when it exchanges a checkout capability for the
    lock's identity. It must not re-derive that id: `req.dwg` is the INTAKE
    source (e.g. `rooftop_demo`) and is frequently a different drawing from
    `params.drawing_id`, so a route computing its own answer could verify a
    capability against one drawing and publish to another.
    """
    return _drawing_id(params)


# --------------------------------------------------------------------------- #
# intake representation helpers
# --------------------------------------------------------------------------- #
def intake_cache_key(tenant_id: str, drawing_id: str, version: int) -> str:
    """Sibling key holding a version's cached intake JSON (used on the live path
    where the version blob itself is DWG bytes, not intake)."""
    import store
    v = int(version)
    return (f"tenants/{store.sanitize_id(tenant_id)}/drawings/"
            f"{store.sanitize_id(drawing_id)}/v/{v:08d}.intake.json")


def intake_cache_proof_key(tenant_id: str, drawing_id: str, version: int) -> str:
    """Binding proof for a derived intake cache beside a raw DWG version."""
    return intake_cache_key(tenant_id, drawing_id, version) + ".proof.json"


def publish_intake_cache(backend, tenant_id: str, drawing_id: str, version: int,
                         source_bytes: bytes, intake: Dict[str, Any]) -> str:
    """Publish derived intake plus a binding to its immutable DWG source.

    A cache that is merely valid JSON can belong to another DWG. The proof
    binds its digest, source digest, key, and version. Readers fail closed if a
    crash leaves the cache without the proof or either artifact is replaced.
    """
    if not isinstance(intake, dict):
        raise ValueError("intake payload is not an object")
    cache_key = intake_cache_key(tenant_id, drawing_id, version)
    cache_bytes = json.dumps(intake, separators=(",", ":")).encode("utf-8")
    proof = {
        "schema": 1,
        "version": int(version),
        "source_sha256": hashlib.sha256(bytes(source_bytes)).hexdigest(),
        "intake_ref": cache_key,
        "intake_sha256": hashlib.sha256(cache_bytes).hexdigest(),
    }
    # The cache is the ordered publication boundary. PR #254 made a missing
    # proof readable through trust-on-first-read plus self-heal, so once these
    # exact cache bytes win the immutable key readers never observe a rejected
    # cache solely because the following proof write was interrupted.
    backend.put_if_absent_or_verify(cache_key, cache_bytes)
    proof_key = intake_cache_proof_key(tenant_id, drawing_id, version)
    proof_bytes = json.dumps(proof, separators=(",", ":")).encode("utf-8")
    try:
        backend.put_if_absent_or_verify(proof_key, proof_bytes)
    except Exception:  # noqa: BLE001
        # Do not guess whether this was transport or a conflicting winner.
        # The one authoritative reader either validates/self-heals the exact
        # source/cache pair or raises with PR #254's proof taxonomy.
        read_intake(backend, tenant_id, drawing_id, version)
    return cache_key


def upload_marker_key(tenant_id: str, drawing_id: str) -> str:
    """Sibling key holding an uploaded drawing's upload/extraction state
    (server/guest_uploads.py writes it; ensure_demo_drawing consults it as a
    fail-closed guard). Lives next to manifest.json so purge deletes it with
    the drawing directory."""
    import store
    return (f"tenants/{store.sanitize_id(tenant_id)}/drawings/"
            f"{store.sanitize_id(drawing_id)}/upload.state.json")


def read_intake(backend, tenant_id: str, drawing_id: str,
                version="head") -> Tuple[int, Dict[str, Any]]:
    """Resolve `version` and return (version_int, intake_dict).

    A JSON-object version blob is its own intake (mock representation). A raw
    DWG version may use a sibling cache only when an upload marker or cache proof
    binds that cache to the exact immutable source bytes. Raises KeyError or
    ValueError on a missing, corrupt, or unbound representation."""
    import store
    v, vkey = store.resolve_version(backend, tenant_id, drawing_id, version)
    ckey = intake_cache_key(tenant_id, drawing_id, v)
    source = backend.get(vkey)
    try:
        direct = json.loads(source.decode("utf-8"))
        if isinstance(direct, dict):
            return v, direct
    except (UnicodeDecodeError, ValueError):
        pass

    if not backend.exists(ckey):
        raise ValueError("raw DWG version has no intake cache")
    candidate = backend.get(ckey)
    source_digest = hashlib.sha256(source).hexdigest()
    intake_digest = hashlib.sha256(candidate).hexdigest()

    # Uploaded v1 already has a durable source/cache proof in its marker.
    import guest_uploads
    marker = guest_uploads.read_marker(backend, tenant_id, drawing_id)
    marker_applies = marker is not None and marker.get("extracted_version") == v
    marker_bound = False
    if marker_applies:
        if (
            marker.get("status") != "ready"
            or marker.get("intake_ref") != ckey
            or not hmac.compare_digest(
                str(marker.get("content_sha256") or ""), source_digest)
        ):
            raise ValueError("ready upload has no source-bound intake proof")
        if not hmac.compare_digest(
            str(marker.get("intake_sha256") or ""), intake_digest
        ):
            raise ValueError("ready upload intake does not match its source proof")
        marker_bound = True

    if not marker_bound:
        pkey = intake_cache_proof_key(tenant_id, drawing_id, v)
        expected = {
            "schema": 1,
            "version": int(v),
            "source_sha256": source_digest,
            "intake_ref": ckey,
            "intake_sha256": intake_digest,
        }
        try:
            proof_exists = backend.exists(pkey)
        except Exception as exc:  # noqa: BLE001 — transport, retryable
            raise ProofStateUnreadable(
                "raw DWG intake cache proof state is unreadable") from exc
        if not proof_exists:
            # PRE-PROOF-ERA version: the sidecar proof was introduced with the
            # restore lane (PR #243) and no backfill exists, so every live DWG
            # version created before it has a cache and NO proof. Refusing
            # here bricked all of them (head reads, write preflights,
            # undo/redo, restore). Trust-on-first-read: accept the cache
            # exactly as every reader did before the proof era — strictly
            # stronger than that status quo, never weaker for new versions —
            # and MINT the missing proof from what we just read, so this
            # version is bound from now on (self-healing backfill). The
            # backfill write is best-effort: a read must not fail because a
            # repair write could not land; the next read retries it.
            # Mint ladder, fail-closed on every DETECTED conflict:
            #  * put_if_absent_or_verify raising store.ImmutableConflict means
            #    a DIFFERENT proof already won this key — that is a detected
            #    conflict, never a transport blip, and must refuse the read.
            #    (A bare ValueError is NOT a conflict signal: OSS credential
            #    parsing or response decoding can raise it for transport
            #    reasons; those fall to the re-read.)
            #  * a transport failure during the mint proves nothing; the key
            #    state is then confirmed by a re-read, where ABSENT means the
            #    pure legacy case (serve, pre-proof status quo), a readable
            #    proof must match, and an UNREADABLE state refuses the read
            #    (retryable blip; serving past a possibly-conflicting proof
            #    is the round-3 reproduction).
            # When the mint returns cleanly it has already get-verified the
            # winner equals our bytes, so no re-read is needed. Note the OSS
            # backend's mint is exists->put->get (no compare-and-swap), so a
            # conflicting proof landing inside that window can be overwritten
            # undetected on the FIRST read of a legacy version; the served
            # intake is still exactly the bytes digested into our proof, and
            # every later read takes the strict path — the same
            # trust-on-first-read bound stated above.
            minted = False
            try:
                backend.put_if_absent_or_verify(
                    pkey,
                    json.dumps(expected, separators=(",", ":")).encode("utf-8"),
                )
                minted = True
            except store.ImmutableConflict as exc:
                raise ValueError(
                    "raw DWG intake cache does not match its source binding"
                ) from exc
            except Exception:  # noqa: BLE001 — transport only; re-read decides
                pass
            if not minted:
                try:
                    raw_proof = backend.get(pkey)
                except KeyError:
                    healed_proof = None  # pure legacy: nothing won the key
                except Exception as exc:  # noqa: BLE001 — transport, retryable
                    raise ProofStateUnreadable(
                        "raw DWG intake cache proof state is unreadable"
                    ) from exc
                else:
                    try:
                        healed_proof = json.loads(raw_proof.decode("utf-8"))
                    except (UnicodeDecodeError, ValueError) as exc:
                        # A READABLE but unparseable proof is corruption, not
                        # transport: fail closed non-retryably as before.
                        raise ValueError(
                            "raw DWG intake cache has no source binding"
                        ) from exc
                if healed_proof is not None and (
                    not isinstance(healed_proof, dict) or any(
                        not hmac.compare_digest(
                            str(healed_proof.get(key)), str(value)
                        )
                        for key, value in expected.items()
                    )
                ):
                    raise ValueError(
                        "raw DWG intake cache does not match its source binding"
                    )
        else:
            # A PRESENT proof is authoritative: corrupt or mismatched fails
            # closed exactly as before (a swapped cache must never be served).
            # Transport failures fetching it are NOT corruption: a backend
            # ValueError (OSS credential/response parsing) or OSError here
            # proves nothing about the proof's content and must stay
            # retryable rather than becoming a permanent refusal.
            try:
                raw_present_proof = backend.get(pkey)
            except Exception as exc:  # noqa: BLE001 — transport, retryable
                raise ProofStateUnreadable(
                    "raw DWG intake cache proof state is unreadable") from exc
            try:
                proof = json.loads(raw_present_proof.decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as exc:
                raise ValueError("raw DWG intake cache has no source binding") from exc
            if not isinstance(proof, dict) or any(
                not hmac.compare_digest(str(proof.get(key)), str(value))
                for key, value in expected.items()
            ):
                raise ValueError("raw DWG intake cache does not match its source binding")

    intake = json.loads(candidate.decode("utf-8"))
    if not isinstance(intake, dict):
        raise ValueError("intake payload is not an object")
    return v, intake


def _live_execution_source_bytes(stored_source: bytes) -> Tuple[bytes, bool]:
    """Return bytes safe to submit as HostDwg and whether they were bridged.

    Early live demo bootstraps stored the tracked rooftop intake JSON as v1.
    Bridge only that exact immutable artifact to its paired tracked DWG. Any
    other JSON object fails closed rather than being mislabeled as HostDwg.
    """
    try:
        decoded = json.loads(stored_source.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return stored_source, False
    if not isinstance(decoded, dict):
        return stored_source, False
    cached = CACHED_INTAKE_PATH.read_bytes()
    if not hmac.compare_digest(
        hashlib.sha256(stored_source).digest(),
        hashlib.sha256(cached).digest(),
    ):
        raise ValueError(
            "live write source is intake JSON without a canonical DWG binding")
    source = DEMO_DWG_PATH.read_bytes()
    if not source:
        raise ValueError("canonical demo DWG source is empty")
    return source, True


def _transform_number(value: Any, field: str, handle: str) -> float:
    """Return one finite transform number, rejecting JSON booleans as numbers."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"transform {handle!r} has non-numeric {field}")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"transform {handle!r} has non-finite {field}")
    return number


def _rounded_coordinate(value: float) -> float:
    """Round transformed XY values and canonicalize both forms of zero."""
    rounded = round(value, 9)
    return 0.0 if rounded == 0 else rounded


def _validated_transforms(intake: Dict[str, Any], mutations: Dict[str, Any]):
    """Validate the complete panel-transform/v1 batch before anything is copied.

    The returned map is keyed by handle, so transform declaration order cannot
    affect the resulting intake.
    """
    if "transforms" not in mutations:
        return {}
    transforms = mutations["transforms"]
    if not isinstance(transforms, list):
        raise ValueError("mutations.transforms must be a list")
    if not transforms:
        raise ValueError("mutations.transforms must not be empty")

    polylines = (intake or {}).get("polylines") or []
    by_handle = {}
    for polyline in polylines:
        if not isinstance(polyline, dict):
            continue
        handle = polyline.get("handle")
        if isinstance(handle, str) and handle:
            if handle in by_handle:
                by_handle[handle] = None
            else:
                by_handle[handle] = polyline

    validated = {}
    for index, transform in enumerate(transforms):
        if not isinstance(transform, dict):
            raise ValueError(f"transform at index {index} must be an object")
        handle = transform.get("handle")
        if not isinstance(handle, str) or not handle:
            raise ValueError(f"transform at index {index} has an invalid handle")
        if handle in validated:
            raise ValueError(f"duplicate transform handle {handle!r}")
        polyline = by_handle.get(handle)
        if polyline is None:
            if handle in by_handle:
                raise ValueError(f"transform handle {handle!r} is ambiguous")
            raise ValueError(f"unknown transform handle {handle!r}")

        if "dx" not in transform or "dy" not in transform:
            raise ValueError(f"transform {handle!r} requires dx and dy")
        dx = _transform_number(transform["dx"], "dx", handle)
        dy = _transform_number(transform["dy"], "dy", handle)
        rotation = _transform_number(
            transform.get("rotation_deg", 0), "rotation_deg", handle)
        if abs(dx) > 10000 or abs(dy) > 10000:
            raise ValueError(f"transform {handle!r} translation exceeds 10000")
        if not -360 <= rotation <= 360:
            raise ValueError(f"transform {handle!r} rotation is outside [-360, 360]")

        points = polyline.get("pts")
        if not isinstance(points, list) or len(points) < 3:
            raise ValueError(f"transform {handle!r} requires at least 3 points")
        for point_index, point in enumerate(points):
            if not isinstance(point, list) or len(point) < 2:
                raise ValueError(
                    f"transform {handle!r} has malformed point {point_index}")
            _transform_number(point[0], f"pts[{point_index}].x", handle)
            _transform_number(point[1], f"pts[{point_index}].y", handle)
        validated[handle] = (dx, dy, rotation)
    return validated


def apply_mutations(intake: Dict[str, Any], mutations: Dict[str, Any]) -> Dict[str, Any]:
    """Apply additive, remove, and panel-transform/v1 mutations to a copy.

    A transform rotates an existing polyline in XY about its source vertex
    centroid, then translates it. Z coordinates and all other data are retained.
    The entire transform batch is validated before mutation, so failures are
    atomic from the caller's perspective.
    """
    if "transforms" in mutations and mutations.get("transforms") == []:
        raise ValueError("mutations.transforms must not be empty")
    mutations = validate_mutations(
        intake, mutations, allow_transforms=True, allow_xdata=True,
        reject_noop=False)
    transforms = _validated_transforms(intake, mutations)
    new = copy.deepcopy(intake or {})
    if transforms:
        for polyline in new.get("polylines") or []:
            handle = polyline.get("handle")
            if handle not in transforms:
                continue
            dx, dy, rotation = transforms[handle]
            points = polyline["pts"]
            center_x = sum(float(point[0]) for point in points) / len(points)
            center_y = sum(float(point[1]) for point in points) / len(points)
            radians = math.radians(rotation)
            cosine, sine = math.cos(radians), math.sin(radians)
            transformed_points = []
            for point in points:
                relative_x = float(point[0]) - center_x
                relative_y = float(point[1]) - center_y
                transformed = list(point)
                transformed[0] = _rounded_coordinate(
                    center_x + relative_x * cosine - relative_y * sine + dx)
                transformed[1] = _rounded_coordinate(
                    center_y + relative_x * sine + relative_y * cosine + dy)
                transformed_points.append(transformed)
            polyline["pts"] = transformed_points

    removed = {str(h) for h in (mutations.get("removed") or [])}
    if removed:
        new["polylines"] = [p for p in (new.get("polylines") or [])
                            if str(p.get("handle")) not in removed]
    added = mutations.get("added") or []
    if added:
        polys = new.setdefault("polylines", [])
        layers = new.setdefault("layers", [])
        for e in added:
            polys.append(e)
            lyr = e.get("layer")
            if lyr and lyr not in layers:
                layers.append(lyr)
    return new


def _put_bytes_version(backend, tenant_id: str, drawing_id: str, data: bytes,
                       parent_version: int, meta: Dict[str, Any], *,
                       holder: Optional[str] = None,
                       fence: Optional[int] = None,
                       require_parent_is_head: bool = False) -> int:
    """put_drawing takes a local path (immutability + sha are computed there), so
    stage `data` to a temp file and append the new version.

    `holder`/`fence` are the caller's single-writer identity, forwarded verbatim
    so the store can refuse a write published under another session's checkout
    (da/store.py put_drawing). `require_parent_is_head` forwards the store's
    compare-and-set for callers whose contract is parent = current head."""
    import store
    fd, tmp = tempfile.mkstemp(suffix=".blob")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(bytes(data))
        return store.put_drawing(backend, tenant_id, drawing_id, tmp,
                                 parent_version=parent_version, meta=meta,
                                 holder=holder, fence=fence,
                                 require_parent_is_head=require_parent_is_head)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# demo bootstrap
# --------------------------------------------------------------------------- #
def ensure_demo_drawing(backend, tenant_id: str, drawing_id: str) -> None:
    """Bootstrap ANY first-seen `drawing_id` on first use.

    At APS_LIVE=0, v1 remains the cached intake JSON via the identical
    `store.ingest_drawing` path the well-known
    `demo` drawing has always used — `demo` is now just one instance of this
    general rule (its resulting v1 is byte-identical to before: same source file,
    same ingest call). Generalizes the earlier demo-only special case per
    leaf-backend-gaps.md §"Any plausible drawing_id ... should resolve to a
    provisioned drawing" (fix (a): extend auto-bootstrap to any first-seen id).

    `drawing_id` must satisfy the ONE shared slug-safe id rule
    (tenant_id_validator.validate_tenant_id — lowercase alnum/`_`/`-`, must start
    alphanumeric, 1..63 chars) so a path-y id (contains `/`, `.`, `..`, uppercase,
    unicode, or is empty) is REJECTED with ValueError up front, before any
    store/filesystem access — defense in depth: this is the auto-provisioning
    gate, not just store.sanitize_id's (later, key-construction-time) rejection
    of the same characters. This never rejects a REAL, already-provisioned
    drawing: every id that can exist in the store was itself sanitize_id-checked
    against this identical pattern at ingest time (store.ingest_drawing /
    put_drawing), so any id reachable via `backend.exists(...)` below already
    passes here too.
    """
    import store
    validate_tenant_id(drawing_id, kind="drawing id")  # reject path-y / malformed ids, up front
    upload_marker_exists = False
    if store.authority_mode() == "postgres":
        try:
            store.load_manifest(backend, tenant_id, drawing_id)
            return
        except KeyError:
            pass
        # PostgreSQL upload authority lives in drawing_upload_attempts, not in
        # the compatibility filesystem marker. Check the authoritative row
        # before demo bootstrap so an intake read cannot race extraction and
        # publish the bundled rooftop as the uploaded drawing's version 1.
        import guest_uploads
        if guest_uploads.upload_store_mode() == "postgres":
            upload_marker_exists = (
                guest_uploads.read_marker(backend, tenant_id, drawing_id)
                is not None
            )
    elif backend.exists(store.manifest_key(tenant_id, drawing_id)):
        return
    # FAIL-CLOSED GUARDS (guest-upload lane, CONTRACT-ADDENDUM §19): the
    # auto-bootstrap below serves the CACHED DEMO ROOF's geometry. For an
    # uploaded drawing that is fabricated data — worse than an error — so two
    # classes of id must never reach it:
    #   (a) ANY drawing under a guest tenant: guests own nothing except what
    #       extraction actually produced. Unknown -> KeyError (routes 404).
    #   (b) ANY drawing with an upload marker but no manifest: its extraction
    #       is pending or failed. ValueError names the state (routes 404 with
    #       the message; the honest status lives at .../upload-status).
    if str(tenant_id).startswith(GUEST_TENANT_PREFIX):
        raise KeyError(f"unknown drawing {drawing_id!r} for guest tenant "
                       f"(guest drawings exist only via upload + extraction)")
    if (
        upload_marker_exists
        or backend.exists(upload_marker_key(tenant_id, drawing_id))
    ):
        raise ValueError(
            f"drawing {drawing_id!r} was uploaded but extraction has not "
            f"produced geometry (see /api/drawings/{drawing_id}/upload-status); "
            f"refusing the demo-intake bootstrap")
    with drawing_mutation_commit_guard() as commit_enabled:
        if not commit_enabled:
            raise ValueError(
                "drawing mutations are temporarily disabled for a storage cutover")
        if backend.exists(store.manifest_key(tenant_id, drawing_id)):
            return
        live = os.environ.get("APS_LIVE", "0").strip() == "1"
        source_path = DEMO_DWG_PATH if live else CACHED_INTAKE_PATH
        data = source_path.read_bytes()
        fd, tmp = tempfile.mkstemp(suffix=".dwg" if live else ".intake.json")
        ingested = False
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            store.ingest_drawing(backend, tenant_id, tmp, drawing_id=drawing_id)
            ingested = True
        except ValueError:
            pass  # race: another request bootstrapped it first
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
        if ingested and live:
            intake = json.loads(CACHED_INTAKE_PATH.read_text(encoding="utf-8"))
            publish_intake_cache(
                backend, tenant_id, drawing_id, 1, data, intake)


# --------------------------------------------------------------------------- #
# router-facing views (GET intake / POST undo / POST redo)
# --------------------------------------------------------------------------- #
def intake_view(tenant_id: str, drawing_id: str, version="head", *, backend=None) -> Dict[str, Any]:
    import store
    backend = backend or default_backend()
    ensure_demo_drawing(backend, tenant_id, drawing_id)
    v, intake = read_intake(backend, tenant_id, drawing_id, version)
    m = store.load_manifest(backend, tenant_id, drawing_id)
    return {"intake": intake, "version": v, "head": int(m["head"]), "latest": int(m["latest"])}


def undo_view(tenant_id: str, drawing_id: str, *, backend=None,
              holder: Optional[str] = None,
              fence: Optional[int] = None) -> Dict[str, Any]:
    """Step head back, then re-read the intake at the new head.

    ``holder``/``fence`` are the caller's single-writer identity, forwarded
    verbatim to ``store.undo`` — which applies the SAME check as a version
    publish, under the same row lock. Both default to None (no check), the shape
    every non-product caller uses.
    """
    import store
    backend = backend or default_backend()
    ensure_demo_drawing(backend, tenant_id, drawing_id)
    with drawing_mutation_commit_guard() as commit_enabled:
        if not commit_enabled:
            raise ValueError(
                "drawing mutations are temporarily disabled for a storage cutover")
        new_head = store.undo(backend, tenant_id, drawing_id,
                              holder=holder, fence=fence)
    v, intake = read_intake(backend, tenant_id, drawing_id, "head")
    m = store.load_manifest(backend, tenant_id, drawing_id)
    return {"version": new_head, "head": int(m["head"]), "latest": int(m["latest"]), "intake": intake}


def redo_view(tenant_id: str, drawing_id: str, *, backend=None,
              holder: Optional[str] = None,
              fence: Optional[int] = None) -> Dict[str, Any]:
    """Step head forward. Same single-writer forwarding as ``undo_view``."""
    import store
    backend = backend or default_backend()
    ensure_demo_drawing(backend, tenant_id, drawing_id)
    with drawing_mutation_commit_guard() as commit_enabled:
        if not commit_enabled:
            raise ValueError(
                "drawing mutations are temporarily disabled for a storage cutover")
        new_head = store.redo(backend, tenant_id, drawing_id,
                              holder=holder, fence=fence)
    v, intake = read_intake(backend, tenant_id, drawing_id, "head")
    m = store.load_manifest(backend, tenant_id, drawing_id)
    return {"version": new_head, "head": int(m["head"]), "latest": int(m["latest"]), "intake": intake}


# --------------------------------------------------------------------------- #
# the WRITE BRANCH of the execution chain (called by server/broker.py)
# --------------------------------------------------------------------------- #
def _status_for(env: Dict[str, Any]) -> int:
    if env.get("ok"):
        return 200
    code = (env.get("error") or {}).get("error_code", ErrorCode.INTERNAL)
    return DEFAULT_HTTP_STATUS.get(code, 500)


def _named_or_anonymous(store, holder: Optional[str]) -> str:
    """Normalize a MISSING single-writer identity to the reserved anonymous id.

    This is the one chokepoint every product write passes through, so doing it
    here covers every way an identity can go missing rather than one caller at a
    time: a pre-rollout job row recovered after a restart (its execution context
    has no such key), an in-process retry or local fallback, an older app that
    sends no `holder`, and an older broker that drops the field. Each of those
    would otherwise arrive as None and skip the check entirely — publishing under
    whatever lock happened to be open, which is the bug this module closes.

    `None` keeps its meaning for direct `store.put_drawing` callers that are NOT
    product writes (ingest, the offline harness, the store's own tests); they do
    not come through here.

    Anonymous is refused against any ACTIVE lock and publishes normally on an
    unlocked drawing — the honest reading of a write whose submitter never named
    itself."""
    return holder if holder else store.ANONYMOUS_HOLDER


def _checkout_denied(exc: Exception, name: Any, tool_version: Any) -> Tuple[Dict[str, Any], int]:
    """A write refused because the caller does not hold the single-writer lock.

    FORBIDDEN/403 deliberately, matching DELETE .../checkout — the caller is
    authenticated and entitled, it just does not own this drawing right now. It
    is NOT retryable: retrying changes nothing until the lock is taken or the
    lease lapses, so a client that treats retryable errors as transient must not
    spin on it."""
    return (err_envelope(ErrorCode.FORBIDDEN, str(exc), retryable=False,
                         tool=name, version=tool_version),
            DEFAULT_HTTP_STATUS[ErrorCode.FORBIDDEN])


def run_write_mock(tool: Dict[str, Any], params: Dict[str, Any], tenant_id: str, *,
                   backend, t0: float, run_tool_dynamic_fn,
                   degraded: bool = False, version="head",
                   holder: Optional[str] = None,
                   fence: Optional[int] = None) -> Tuple[Dict[str, Any], int]:
    """APS_LIVE=0 write: run the tool file for its mutations, apply them to the
    BASE version's intake (``version``; default "head", unchanged), persist a new
    version whose parent is that base, stamp result.new_version.

    ``holder``/``fence`` are the caller's single-writer identity; the persist below
    is refused (403) when another session holds the checkout."""
    import store
    holder = _named_or_anonymous(store, holder)
    name = tool.get("name")
    tool_version = tool.get("version", "1.0.0")
    drawing_id = _drawing_id(params)
    try:
        ensure_demo_drawing(backend, tenant_id, drawing_id)
        head_v, cur_intake = read_intake(backend, tenant_id, drawing_id, version)
    except ProofStateUnreadable as exc:
        # The base version exists; its proof state is unreachable right now.
        # Retryable — nothing has been written yet at this point.
        return (err_envelope(ErrorCode.INTERNAL, str(exc),
                             retryable=True, tool=name, version=tool_version),
                503)
    except (KeyError, ValueError) as exc:
        return (err_envelope(ErrorCode.BAD_PARAMS, f"drawing/version unavailable: {exc}",
                             retryable=False, tool=name, version=tool_version),
                DEFAULT_HTTP_STATUS[ErrorCode.BAD_PARAMS])

    env = run_tool_dynamic_fn(tool, cur_intake, dict(params or {}),
                              aps_live=False, da=None, t0=t0,
                              tenant_id=tenant_id)
    if not env.get("ok"):
        return env, _status_for(env)

    result = env.get("result") or {}
    mutations = result.get("mutations") or {}
    if params.get("dry_run") is True:
        # Design-time validation and user previews must never advance drawing
        # history. Return the deterministic proposal exactly as the tool
        # computed it, but stop before applying mutations or publishing vN+1.
        result["dry_run"] = True
        env["result"] = result
        if degraded:
            env["degraded_mode"] = True
        return env, 200
    try:
        new_intake = apply_mutations(cur_intake, mutations)
        with drawing_mutation_commit_guard() as commit_enabled:
            if not commit_enabled:
                return (err_envelope(
                    ErrorCode.APS_UNAVAILABLE,
                    "drawing mutations were drained before write commit",
                    retryable=True, tool=name, version=tool_version,
                ), 503)
            new_v = _put_bytes_version(
                backend, tenant_id, drawing_id,
                json.dumps(new_intake, separators=(",", ":")).encode("utf-8"),
                parent_version=head_v,
                meta={"tool": name, "note": "mock write (intake payload)"},
                holder=holder, fence=fence)
    except store.CheckoutDenied as exc:
        # Caught BEFORE the blanket handler below: publishing under another
        # session's lock is an authorization answer (403), not a persist fault
        # (500). The tool already ran, but nothing was written.
        return _checkout_denied(exc, name, tool_version)
    except Exception as exc:  # noqa: BLE001
        return (err_envelope(ErrorCode.INTERNAL, f"write persist failed: {type(exc).__name__}: {exc}",
                             retryable=False, tool=name, version=tool_version),
                DEFAULT_HTTP_STATUS[ErrorCode.INTERNAL])

    result["new_version"] = {"drawing_id": drawing_id, "version": new_v, "parent": head_v}
    env["result"] = result
    if degraded:
        env["degraded_mode"] = True
    return env, 200


def _accepts_on_submitted(fn: Any) -> bool:
    """Whether `fn` can take an `on_submitted` kwarg.

    Mirrors the broker-side guard. `da` is a test double in most suites, so the
    kwarg is offered only to implementations that declare it (explicitly or via
    **kwargs); an older/stubbed submit_workitem keeps its exact call signature.
    """
    import inspect
    try:
        sig_params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    if "on_submitted" in sig_params:
        return True
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in sig_params.values())


def _protected_live_posture() -> bool:
    return (
        os.environ.get("LEAF_RUNTIME_ENV", "").strip().lower() == "production"
        and os.environ.get("LEAF_AUTHORED_EXECUTION", "").strip().lower()
        in {"1", "true", "yes", "on"}
    )


def _valid_microvm_provenance(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and value.get("contract") == "leaf.tool-execution.v1"
        and value.get("provider") == "e2b"
        and value.get("isolation") == "microvm"
        and value.get("passed") is True
        and isinstance(value.get("source_sha256"), str)
        and re.fullmatch(r"[0-9a-f]{64}", value["source_sha256"]) is not None
    )


def _scratch_upload_bytes(da: Any, key: str, data: bytes, suffix: str) -> None:
    fd, path = tempfile.mkstemp(suffix=suffix)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        if hasattr(da, "upload_scratch_object"):
            da.upload_scratch_object(path, key)
        else:
            da.upload_object(path, key)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def _scratch_download_url(da: Any, key: str) -> str:
    if hasattr(da, "scratch_signed_download_url"):
        return da.scratch_signed_download_url(key)
    return da.signed_download_url(key)


def _scratch_upload_url(da: Any, key: str):
    if hasattr(da, "scratch_signed_upload_url"):
        return da.scratch_signed_upload_url(key)
    return da.signed_upload_url(key)


def _scratch_download_bytes(da: Any, key: str, upload_key: str) -> bytes:
    if hasattr(da, "finalize_scratch_upload"):
        da.finalize_scratch_upload(key, upload_key)
        return da.download_scratch_object(key)
    da.finalize_upload(key, upload_key)
    return da.download_object(key)


def _point_close(left: Any, right: Any, tolerance: float = 1e-5) -> bool:
    if not isinstance(left, list) or not isinstance(right, list):
        return False
    if len(left) < 2 or len(right) < 2:
        return False
    width = max(len(left), len(right), 3)
    lvals = list(left) + [0.0] * (width - len(left))
    rvals = list(right) + [0.0] * (width - len(right))
    try:
        return all(
            math.isclose(float(lvals[index]), float(rvals[index]),
                         rel_tol=0.0, abs_tol=tolerance)
            for index in range(width)
        )
    except (TypeError, ValueError):
        return False


def _polyline_effect_matches(expected: Dict[str, Any], actual: Dict[str, Any]) -> bool:
    if expected.get("layer") != actual.get("layer"):
        return False
    if actual.get("closed") is not True:
        return False
    expected_points = expected.get("pts") or []
    actual_points = actual.get("pts") or []
    return (
        len(expected_points) == len(actual_points)
        and all(_point_close(left, right)
                for left, right in zip(expected_points, actual_points))
    )


def verify_live_mutation_effects(
    base: Dict[str, Any], actual: Dict[str, Any], canonical: Dict[str, Any],
) -> None:
    """Refuse publication unless extraction proves exactly the proposed effects."""
    expected = apply_mutations(base, canonical)
    base_polylines = base.get("polylines") or []
    actual_polylines = actual.get("polylines") or []
    if not isinstance(actual_polylines, list):
        raise ValueError("re-extracted output has no polyline list")
    expected_count = (
        len(base_polylines) - len(canonical.get("removed", []))
        + len(canonical.get("added", []))
    )
    if len(actual_polylines) != expected_count:
        raise ValueError("re-extracted output entity count does not match the plan")
    if any(
        not isinstance(entity, dict)
        or not isinstance(entity.get("handle"), str)
        or not entity["handle"]
        for entity in actual_polylines
    ):
        raise ValueError(
            "every re-extracted output polyline must have a nonempty handle")
    actual_by_handle = {
        str(entity.get("handle")): entity
        for entity in actual_polylines
        if isinstance(entity, dict) and entity.get("handle") is not None
    }
    actual_handles = [
        str(entity.get("handle")) for entity in actual_polylines
        if isinstance(entity, dict) and entity.get("handle") is not None
    ]
    if len(actual_handles) != len(set(actual_handles)):
        raise ValueError("re-extracted output contains duplicate handles")
    for handle in canonical.get("removed", []):
        if handle in actual_by_handle:
            raise ValueError(f"removed handle {handle!r} remains in output")
    removed = set(canonical.get("removed", []))
    for entity in base_polylines:
        if not isinstance(entity, dict) or entity.get("handle") is None:
            continue
        handle = str(entity["handle"])
        if handle not in removed and handle not in actual_by_handle:
            raise ValueError(f"unchanged handle {handle!r} is missing from output")
        if (handle not in removed
                and not _polyline_effect_matches(entity, actual_by_handle[handle])):
            raise ValueError(f"unchanged handle {handle!r} was modified in output")
    base_handles = {
        str(entity.get("handle")) for entity in base_polylines
        if isinstance(entity, dict) and entity.get("handle") is not None
    }
    unmatched = [
        entity for entity in actual_polylines
        if isinstance(entity, dict)
        and str(entity.get("handle")) not in base_handles
    ]
    if len(unmatched) != len(canonical.get("added", [])):
        raise ValueError("re-extracted output has unexpected new entities")
    for entity in canonical.get("added", []):
        match_index = next(
            (index for index, candidate in enumerate(unmatched)
             if isinstance(candidate, dict)
             and _polyline_effect_matches(entity, candidate)),
            None,
        )
        if match_index is None:
            raise ValueError(f"added polyline {entity['handle']!r} is missing from output")
        unmatched.pop(match_index)
    if unmatched:
        raise ValueError("re-extracted output has unmatched new entities")
    # Compute the expected representation here as an additional structural
    # check and to keep mock/live validation on one implementation.
    if len(expected.get("polylines") or []) != expected_count:
        raise ValueError("canonical mutation application produced an invalid count")


def _run_write_live_legacy(tool: Dict[str, Any], params: Dict[str, Any], tenant_id: str, *,
                   backend, da: Any, t0: float,
                   ledger_entry: Optional[Dict[str, Any]] = None,
                   version="head", holder: Optional[str] = None,
                   fence: Optional[int] = None,
                   on_submitted=None) -> Tuple[Dict[str, Any], int]:
    """Retired pre-authored-plan live write implementation.

    APS_LIVE=1 write formerly ran a fixed probe Activity on the BASE
    version's DWG (``version``; default "head", unchanged), store output.dwg as a
    new version whose parent is that base, re-extract for the intake cache, stamp
    result.new_version. `da` is the credential-holding client.

    ``holder``/``fence`` are the caller's single-writer identity. They are checked
    TWICE: once up front (below, before the WorkItem is submitted, so an
    unauthorized writer never reaches a billable engine second) and again inside
    put_drawing at commit time, which is the authoritative check because it runs
    under the store's row lock."""
    raise RuntimeError("retired live write path must never be called")
    import store
    holder = _named_or_anonymous(store, holder)
    name = tool.get("name")
    tool_version = tool.get("version", "1.0.0")
    drawing_id = _drawing_id(params)
    scratch_keys = []
    try:
        try:
            store.load_manifest(backend, tenant_id, drawing_id)
        except KeyError:
            return (err_envelope(ErrorCode.BAD_PARAMS,
                                 f"drawing not in store: {tenant_id}/{drawing_id} "
                                 f"(ingest it before a live write)", retryable=False,
                                 tool=name, version=tool_version),
                    DEFAULT_HTTP_STATUS[ErrorCode.BAD_PARAMS])
        # §19 fail-closed guard (review round 3, MAJOR): a live write signs the
        # version blob as HostDwg — that is only truthful when the blob IS DWG
        # bytes. An UPLOADED drawing records its source format in the upload
        # marker; anything non-.dwg (a DXF's intake-JSON mock blob) must refuse
        # honestly here rather than hand APS a mislabeled file.
        import guest_uploads
        marker = guest_uploads.read_marker(
            backend, tenant_id, drawing_id)
        if (
            marker is None
            and guest_uploads.upload_store_mode() == "legacy"
            and backend.exists(upload_marker_key(tenant_id, drawing_id))
        ):
            # A present legacy marker that cannot be decoded is not the same
            # as no upload marker. Its source format is unknown, so refuse it
            # before any paid APS work instead of treating the drawing as a
            # trusted native DWG.
            marker = {}
        if marker is not None:
            source_ext = str(marker.get("source_ext") or "")
            if not source_ext:
                # Pre-round-4 markers (schema 1, no source_ext field): fall
                # back to the recorded filename's extension — present in every
                # marker since the feature commit — instead of bricking live
                # writes on already-persisted DWG uploads (review round 4,
                # MINOR: compatibility path for old markers).
                source_ext = os.path.splitext(
                    str(marker.get("filename") or ""))[1].lower()
            if source_ext != ".dwg":
                return (err_envelope(
                    ErrorCode.BAD_PARAMS,
                    f"live writes need a DWG source; drawing {drawing_id!r} was "
                    f"uploaded as {source_ext or 'an unknown format'} (its store "
                    f"blob is the extracted intake, not DWG bytes)",
                    retryable=False, tool=name, version=tool_version),
                    DEFAULT_HTTP_STATUS[ErrorCode.BAD_PARAMS])
        # Authorize BEFORE spending. Everything past this point costs money:
        # scratch uploads, signed URLs, a submitted WorkItem, polled engine
        # seconds. The commit inside _put_bytes_version re-checks under the
        # store's row lock and is the authoritative guard; this one exists so a
        # write published under another session's checkout is refused for free
        # instead of billed and then rejected.
        store.authorize_checkout(backend, tenant_id, drawing_id, holder, fence)
        ts = int(time.time())
        run_nonce = secrets.token_hex(8)
        head_v, vkey = store.resolve_version(backend, tenant_id, drawing_id, version)
        stored_source = backend.get(vkey)
        execution_source, bridged_legacy_bootstrap = (
            _live_execution_source_bytes(stored_source))
        if isinstance(backend, store.OSSBackend) and not bridged_legacy_bootstrap:
            in_url = da.signed_download_url(vkey)
        else:
            # The persistent drawing blob lives on shared EFS. Stage only this
            # WorkItem input in broker-owned APS scratch storage, then keep the
            # resulting immutable version in the configured persistent backend.
            input_key = (
                da.ephemeral_input_key(
                    f"{store.sanitize_id(drawing_id)}_v{head_v}_{run_nonce}.dwg",
                    tenant_id=tenant_id,
                    ts=ts,
                )
                if hasattr(da, "ephemeral_input_key")
                else f"t/{store.sanitize_id(tenant_id)}/in/{ts}_"
                     f"{store.sanitize_id(drawing_id)}_v{head_v}_{run_nonce}.dwg"
            )
            fd, staged_input = tempfile.mkstemp(suffix=".dwg")
            try:
                with os.fdopen(fd, "wb") as fh:
                    fh.write(execution_source)
                if hasattr(da, "upload_scratch_object"):
                    da.upload_scratch_object(staged_input, input_key)
                else:
                    da.upload_object(staged_input, input_key)
                scratch_keys.append(input_key)
            finally:
                try:
                    os.remove(staged_input)
                except OSError:
                    pass
            in_url = (
                da.scratch_signed_download_url(input_key)
                if hasattr(da, "scratch_signed_download_url")
                else da.signed_download_url(input_key)
            )
        output_name = f"{store.sanitize_id(drawing_id)}_write_{run_nonce}"
        out_key = (
            da.ephemeral_output_key(
                output_name, tenant_id=tenant_id, ts=ts, suffix=".dwg")
            if hasattr(da, "ephemeral_output_key")
            else f"t/{store.sanitize_id(tenant_id)}/out/{ts}_{output_name}.dwg"
        )
        scratch_keys.append(out_key)
        up_key, out_url = (
            da.scratch_signed_upload_url(out_key)
            if hasattr(da, "scratch_signed_upload_url")
            else da.signed_upload_url(out_key)
        )
        activity_id = da.activity_qualified(WRITE_ACTIVITY)
        w_args = {"HostDwg": {"url": in_url, "verb": "get"},
                  "Result": {"url": out_url, "verb": "put"}}
        submit_kwargs: Dict[str, Any] = {"dry_run": False, "poll": True,
                                         "tenant_id": tenant_id}
        # Report the live WorkItem id before the poll blocks, so a tab closed
        # mid-write has something to cancel (see broker._record_active_workitem).
        if on_submitted is not None and _accepts_on_submitted(da.submit_workitem):
            submit_kwargs["on_submitted"] = on_submitted
        status = da.submit_workitem(activity_id, w_args, **submit_kwargs)
        if status.get("status") != "success":
            return (err_envelope(ErrorCode.WORKITEM_FAILED,
                                 f"write WorkItem {status.get('id')} status={status.get('status')} "
                                 f"report={status.get('reportUrl')}", retryable=True,
                                 tool=name, version=tool_version),
                    DEFAULT_HTTP_STATUS[ErrorCode.WORKITEM_FAILED])
        if hasattr(da, "finalize_scratch_upload"):
            da.finalize_scratch_upload(out_key, up_key)
            out_bytes = da.download_scratch_object(out_key)
        else:
            da.finalize_upload(out_key, up_key)
            out_bytes = da.download_object(out_key)
        if not out_bytes:
            return (err_envelope(ErrorCode.WORKITEM_FAILED, "write produced 0-byte output.dwg",
                                 retryable=True, tool=name, version=tool_version),
                    DEFAULT_HTTP_STATUS[ErrorCode.WORKITEM_FAILED])

        # re-extract the new version's DWG for the intake cache
        fd, tmp = tempfile.mkstemp(suffix=".dwg")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(out_bytes)
            intake = da.extract(tmp)
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
        wi_id = status.get("id")
        # Everything fallible that is not publication belongs before the
        # immutable commit. Once new_v exists, retrying can repeat paid work.
        eng_s = da._engine_seconds(status)
        cost = None if eng_s is None else {
            "engine_seconds": eng_s,
            "usd_est": round(eng_s / 3600.0 * USD_PER_HR, 4)}
        if ledger_entry is not None and isinstance(cost, dict):
            ledger_entry["engine_seconds"] = cost["engine_seconds"]
            ledger_entry["usd_est"] = cost["usd_est"]

        with drawing_mutation_commit_guard() as commit_enabled:
            if not commit_enabled:
                return (err_envelope(
                    ErrorCode.APS_UNAVAILABLE,
                    "drawing mutations were drained before write commit",
                    retryable=True, tool=name, version=tool_version,
                ), 503)
            new_v = _put_bytes_version(
                backend, tenant_id, drawing_id, out_bytes,
                parent_version=head_v,
                meta={"tool": name, "workitem_id": wi_id,
                      "note": "live write (output.dwg)"},
                holder=holder, fence=fence)
            new_version_readable = False
            try:
                publish_intake_cache(
                    backend, tenant_id, drawing_id, new_v, out_bytes, intake)
                read_intake(backend, tenant_id, drawing_id, new_v)
                new_version_readable = True
            except Exception:  # noqa: BLE001
                # The immutable version is already the head. Report that
                # committed truth as success and let the product enter its
                # recovery-only lock. A retry could repeat the paid WorkItem.
                new_version_readable = False

        result = {
            "new_version": {"drawing_id": drawing_id, "version": new_v, "parent": head_v},
            "new_version_readable": new_version_readable,
            "workitem_id": wi_id,
            "retired_probe_layer": "retired",
            "output_dwg_bytes": len(out_bytes),
        }
        env = ok_envelope(name, tool_version, result,
                          overlay=None,
                          timing_ms=int((time.perf_counter() - t0) * 1000),
                          cost=cost, degraded_mode=False)
        return env, 200
    except store.CheckoutDenied as exc:
        # BEFORE both handlers below. CheckoutDenied is deliberately not a
        # ValueError, but ordering it explicitly here keeps the 403 answer from
        # depending on that fact: publishing under another session's lock is an
        # authorization result, never a 400 bad-drawing or a 502 WorkItem fault.
        return _checkout_denied(exc, name, tool_version)
    except FileNotFoundError as exc:  # creds missing
        return (err_envelope(ErrorCode.APS_UNAVAILABLE, str(exc), retryable=False,
                             tool=name, version=tool_version),
                DEFAULT_HTTP_STATUS[ErrorCode.APS_UNAVAILABLE])
    except (KeyError, ValueError) as exc:
        # unknown drawing/BASE version (da/store.py resolve_version, or a missing
        # manifest key on a race) — a CLIENT input error, not a WorkItem failure.
        # MUST be caught here, before the broad Exception handler below, so an
        # unknown `version` surfaces as BAD_PARAMS/non-retryable (review 2026-07-22
        # finding: it was previously falling through to WORKITEM_FAILED/retryable,
        # i.e. an HTTP 502 for what is really a 400) — matches run_write_mock's own
        # (KeyError, ValueError) -> BAD_PARAMS surfacing exactly.
        return (err_envelope(ErrorCode.BAD_PARAMS, f"drawing/version unavailable: {exc}",
                             retryable=False, tool=name, version=tool_version),
                DEFAULT_HTTP_STATUS[ErrorCode.BAD_PARAMS])
    except Exception as exc:  # noqa: BLE001
        return (err_envelope(ErrorCode.WORKITEM_FAILED, f"{type(exc).__name__}: {exc}",
                             retryable=True, tool=name, version=tool_version),
                DEFAULT_HTTP_STATUS[ErrorCode.WORKITEM_FAILED])
    finally:
        # Best effort only. The dedicated transient bucket still expires a copy
        # if an immediate delete is interrupted.
        _cleanup_scratch_objects(da, scratch_keys)


class LiveMutationEffectMismatch(RuntimeError):
    pass


def run_write_live(tool: Dict[str, Any], params: Dict[str, Any], tenant_id: str, *,
                   backend, da: Any, t0: float, run_tool_dynamic_fn=None,
                   ledger_entry: Optional[Dict[str, Any]] = None,
                   version="head", holder: Optional[str] = None,
                   fence: Optional[int] = None,
                   on_submitted=None) -> Tuple[Dict[str, Any], int]:
    """Execute an authored planner, then apply its validated data plan in APS."""
    import store
    holder = _named_or_anonymous(store, holder)
    name = tool.get("name")
    tool_version = tool.get("version", "1.0.0")
    drawing_id = _drawing_id(params)
    scratch_keys = []
    try:
        try:
            store.load_manifest(backend, tenant_id, drawing_id)
        except KeyError:
            return (err_envelope(
                ErrorCode.BAD_PARAMS,
                f"drawing not in store: {tenant_id}/{drawing_id} "
                f"(ingest it before a live write)", retryable=False,
                tool=name, version=tool_version,
            ), DEFAULT_HTTP_STATUS[ErrorCode.BAD_PARAMS])

        import guest_uploads
        marker = guest_uploads.read_marker(backend, tenant_id, drawing_id)
        if (marker is None and guest_uploads.upload_store_mode() == "legacy"
                and backend.exists(upload_marker_key(tenant_id, drawing_id))):
            marker = {}
        if marker is not None:
            source_ext = str(marker.get("source_ext") or "")
            if not source_ext:
                source_ext = os.path.splitext(
                    str(marker.get("filename") or ""))[1].lower()
            if source_ext != ".dwg":
                return (err_envelope(
                    ErrorCode.BAD_PARAMS,
                    f"live writes need a DWG source; drawing {drawing_id!r} was "
                    f"uploaded as {source_ext or 'an unknown format'}",
                    retryable=False, tool=name, version=tool_version,
                ), DEFAULT_HTTP_STATUS[ErrorCode.BAD_PARAMS])

        # Both controls precede tenant execution and every APS-capable call.
        if not drawing_mutations_enabled():
            return (err_envelope(
                ErrorCode.APS_UNAVAILABLE,
                "drawing mutations are temporarily disabled",
                retryable=True, tool=name, version=tool_version,
            ), 503)
        store.authorize_checkout(backend, tenant_id, drawing_id, holder, fence)

        head_v, vkey = store.resolve_version(
            backend, tenant_id, drawing_id, version)
        base_v, base_intake = read_intake(
            backend, tenant_id, drawing_id, head_v)
        if base_v != head_v:
            raise ValueError("base intake resolved to a different version")
        stored_source = backend.get(vkey)
        execution_source, bridged_legacy_bootstrap = (
            _live_execution_source_bytes(stored_source))
        base_sha = hashlib.sha256(execution_source).hexdigest()

        if run_tool_dynamic_fn is None:
            raise ValueError("live authored write requires a sandboxed planner")
        planner_env = run_tool_dynamic_fn(
            tool, base_intake, dict(params or {}), aps_live=False, da=None,
            t0=t0, tenant_id=tenant_id,
        )
        if not isinstance(planner_env, dict) or not planner_env.get("ok"):
            if isinstance(planner_env, dict):
                return planner_env, _status_for(planner_env)
            return (err_envelope(
                ErrorCode.INTERNAL, "authored planner returned no envelope",
                retryable=False, tool=name, version=tool_version,
            ), DEFAULT_HTTP_STATUS[ErrorCode.INTERNAL])
        provenance = planner_env.get("execution_provenance")
        if _protected_live_posture() and not _valid_microvm_provenance(provenance):
            return (err_envelope(
                ErrorCode.INTERNAL,
                "protected live write requires successful E2B microvm provenance",
                retryable=False, tool=name, version=tool_version,
            ), DEFAULT_HTTP_STATUS[ErrorCode.INTERNAL])
        planner_result = planner_env.get("result")
        if not isinstance(planner_result, dict):
            raise ValueError("authored planner result must be an object")
        canonical = validate_mutations(
            base_intake, planner_result.get("mutations"),
            allow_transforms=False, allow_xdata=False,
        )
        # Planar lowering happens during emission, still before any APS call.
        plan_bytes = emit_plan(canonical, base_sha256=base_sha)
        plan_digest = plan_sha256(plan_bytes)
        tool_definition = {
            key: value for key, value in tool.items()
            if key not in {"catalog_digest", "tool_manifest_sha256"}
        }
        tool_digest = hashlib.sha256(
            canonical_json_bytes(tool_definition)).hexdigest()
        binding = {
            "drawing_id": drawing_id,
            "base_version": head_v,
            "base_source_sha256": base_sha,
            "tool_manifest_sha256": tool_digest,
            "tool_source_sha256": (
                provenance.get("source_sha256")
                if isinstance(provenance, dict) else None
            ),
            "plan_sha256": plan_digest,
        }
        planner_result["mutations"] = canonical
        planner_result["mutation_binding"] = binding
        if params.get("dry_run") is True:
            planner_result["dry_run"] = True
            planner_env["result"] = planner_result
            return planner_env, 200

        ts = int(time.time())
        run_nonce = secrets.token_hex(8)
        if isinstance(backend, store.OSSBackend) and not bridged_legacy_bootstrap:
            in_url = da.signed_download_url(vkey)
        else:
            input_key = (
                da.ephemeral_input_key(
                    f"{store.sanitize_id(drawing_id)}_v{head_v}_{run_nonce}.dwg",
                    tenant_id=tenant_id, ts=ts,
                ) if hasattr(da, "ephemeral_input_key")
                else f"t/{store.sanitize_id(tenant_id)}/in/{ts}_"
                     f"{store.sanitize_id(drawing_id)}_v{head_v}_{run_nonce}.dwg"
            )
            _scratch_upload_bytes(da, input_key, execution_source, ".dwg")
            scratch_keys.append(input_key)
            in_url = _scratch_download_url(da, input_key)

        plan_key = (
            da.ephemeral_input_key(
                f"{store.sanitize_id(drawing_id)}_{run_nonce}_mutation-plan.txt",
                tenant_id=tenant_id, ts=ts,
            ) if hasattr(da, "ephemeral_input_key")
            else f"t/{store.sanitize_id(tenant_id)}/in/{ts}_"
                 f"{store.sanitize_id(drawing_id)}_{run_nonce}_mutation-plan.txt"
        )
        _scratch_upload_bytes(da, plan_key, plan_bytes, ".txt")
        scratch_keys.append(plan_key)
        plan_url = _scratch_download_url(da, plan_key)

        output_name = f"{store.sanitize_id(drawing_id)}_write_{run_nonce}"
        out_key = (
            da.ephemeral_output_key(
                output_name, tenant_id=tenant_id, ts=ts, suffix=".dwg")
            if hasattr(da, "ephemeral_output_key")
            else f"t/{store.sanitize_id(tenant_id)}/out/{ts}_{output_name}.dwg"
        )
        scratch_keys.append(out_key)
        upload_key, out_url = _scratch_upload_url(da, out_key)
        arguments = {
            "HostDwg": {"url": in_url, "verb": "get"},
            "Plan": {"url": plan_url, "verb": "get"},
            "Result": {"url": out_url, "verb": "put"},
        }
        submit_kwargs: Dict[str, Any] = {
            "dry_run": False, "poll": True, "tenant_id": tenant_id,
        }
        if on_submitted is not None and _accepts_on_submitted(da.submit_workitem):
            submit_kwargs["on_submitted"] = on_submitted
        status = da.submit_workitem(
            da.activity_qualified(WRITE_ACTIVITY), arguments, **submit_kwargs)
        if status.get("status") != "success":
            return (err_envelope(
                ErrorCode.WORKITEM_FAILED,
                "APS write WorkItem did not succeed", retryable=True,
                tool=name, version=tool_version,
            ), DEFAULT_HTTP_STATUS[ErrorCode.WORKITEM_FAILED])
        out_bytes = _scratch_download_bytes(da, out_key, upload_key)
        if not out_bytes:
            raise LiveMutationEffectMismatch("write produced 0-byte output.dwg")

        fd, temp_output = tempfile.mkstemp(suffix=".dwg")
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(out_bytes)
            output_intake = da.extract(temp_output)
        finally:
            try:
                os.remove(temp_output)
            except OSError:
                pass
        if not isinstance(output_intake, dict):
            raise LiveMutationEffectMismatch("re-extracted output is not an intake object")
        output_intake["dwg"] = drawing_id
        normalized_base = copy.deepcopy(base_intake)
        normalized_base["dwg"] = drawing_id
        try:
            verify_live_mutation_effects(normalized_base, output_intake, canonical)
        except ValueError as exc:
            raise LiveMutationEffectMismatch(str(exc)) from exc

        engine_seconds = da._engine_seconds(status)
        cost = None if engine_seconds is None else {
            "engine_seconds": engine_seconds,
            "usd_est": round(engine_seconds / 3600.0 * USD_PER_HR, 4),
        }
        if ledger_entry is not None and isinstance(cost, dict):
            ledger_entry.update(cost)
        with drawing_mutation_commit_guard() as commit_enabled:
            if not commit_enabled:
                return (err_envelope(
                    ErrorCode.APS_UNAVAILABLE,
                    "drawing mutations were drained before write commit",
                    retryable=True, tool=name, version=tool_version,
                ), 503)
            new_v = _put_bytes_version(
                backend, tenant_id, drawing_id, out_bytes,
                parent_version=head_v,
                meta={"tool": name, "workitem_id": status.get("id"),
                      "plan_sha256": plan_digest,
                      "note": "live authored mutation plan"},
                holder=holder, fence=fence, require_parent_is_head=True,
            )
            readable = False
            try:
                publish_intake_cache(
                    backend, tenant_id, drawing_id, new_v, out_bytes, output_intake)
                read_intake(backend, tenant_id, drawing_id, new_v)
                readable = True
            except Exception:  # noqa: BLE001
                readable = False

        planner_result.update({
            "new_version": {
                "drawing_id": drawing_id, "version": new_v, "parent": head_v,
            },
            "new_version_readable": readable,
            "workitem_id": status.get("id"),
            "output_dwg_bytes": len(out_bytes),
        })
        planner_env["result"] = planner_result
        planner_env["cost"] = cost
        planner_env["timing_ms"] = int((time.perf_counter() - t0) * 1000)
        planner_env["degraded_mode"] = False
        return planner_env, 200
    except store.CheckoutDenied as exc:
        return _checkout_denied(exc, name, tool_version)
    except ProofStateUnreadable as exc:
        return (err_envelope(ErrorCode.INTERNAL, str(exc), retryable=True,
                             tool=name, version=tool_version), 503)
    except LiveMutationEffectMismatch as exc:
        return (err_envelope(
            ErrorCode.WORKITEM_FAILED, str(exc), retryable=False,
            tool=name, version=tool_version,
        ), DEFAULT_HTTP_STATUS[ErrorCode.WORKITEM_FAILED])
    except FileNotFoundError as exc:
        return (err_envelope(ErrorCode.APS_UNAVAILABLE, str(exc), retryable=False,
                             tool=name, version=tool_version),
                DEFAULT_HTTP_STATUS[ErrorCode.APS_UNAVAILABLE])
    except (KeyError, ValueError) as exc:
        return (err_envelope(
            ErrorCode.BAD_PARAMS, f"drawing/mutation unavailable: {exc}",
            retryable=False, tool=name, version=tool_version,
        ), DEFAULT_HTTP_STATUS[ErrorCode.BAD_PARAMS])
    except Exception as exc:  # noqa: BLE001
        # Transport exceptions can contain signed APS or object-store URLs.
        # Keep their text out of the client envelope. The exception class is
        # enough to group the failure without persisting credential-shaped data.
        LOGGER.error("live authored write failed: %s", type(exc).__name__)
        return (err_envelope(
            ErrorCode.WORKITEM_FAILED, "live drawing mutation failed",
            retryable=True, tool=name, version=tool_version,
        ), DEFAULT_HTTP_STATUS[ErrorCode.WORKITEM_FAILED])
    finally:
        _cleanup_scratch_objects(da, scratch_keys)
