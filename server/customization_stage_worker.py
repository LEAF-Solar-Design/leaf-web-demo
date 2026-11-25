"""Durable worker for queued customization stage requests."""
from __future__ import annotations

import logging
import math
import os
import socket
import threading
import uuid
from typing import Any

from customization_models import ChangeState
from customization_service import (
    CustomizationService, CustomizationServiceError, finish_stage_authority_for_change,
)


_LOG = logging.getLogger(__name__)


DEFAULT_LEASE_SECONDS = 30.0
MAX_STAGE_WORKERS = 4
_claim_lock = threading.Lock()
_active_tenants: set[str] = set()


def worker_count() -> int:
    value = os.environ.get("LEAF_CUSTOMIZATION_STAGE_WORKERS", "").strip()
    if not value:
        return 1
    if len(value) == 1 and "1" <= value <= str(MAX_STAGE_WORKERS):
        return int(value)
    return 1


def fair_claims() -> bool:
    return os.environ.get("LEAF_CUSTOMIZATION_STAGE_FAIR", "").strip() == "1" or worker_count() > 1


def queue_log_interval() -> float | None:
    try:
        value = float(os.environ.get("LEAF_CUSTOMIZATION_STAGE_QUEUE_LOG_S", ""))
    except ValueError:
        return None
    return max(10.0, min(value, 3600.0)) if math.isfinite(value) and value > 0 else None


def _queue_log_loop(stop_event: threading.Event, interval: float) -> None:
    while not stop_event.wait(interval):
        try:
            stats = CustomizationService.configured().store.stage_queue_stats()
            _LOG.info(
                "customization_stage_queue depth=%d running=%d oldest_wait_s=%d",
                stats["depth"], stats["running"], stats["oldest_wait_s"],
            )
        except Exception as exc:
            _LOG.warning("customization_stage_queue exception=%s", type(exc).__name__)


def start_workers(stop_event: threading.Event) -> list[threading.Thread]:
    value = os.environ.get("LEAF_CUSTOMIZATION_STAGE_WORKERS", "").strip()
    if value and not (len(value) == 1 and "1" <= value <= str(MAX_STAGE_WORKERS)):
        _LOG.warning("invalid LEAF_CUSTOMIZATION_STAGE_WORKERS")
    threads = []
    for index in range(worker_count()):
        thread = threading.Thread(
            target=serve, kwargs={"stop_event": stop_event}, daemon=True,
            name="customization-stage-worker" + (f"-{index + 1}" if index else ""),
        )
        thread.start()
        threads.append(thread)
    interval = queue_log_interval()
    if interval is not None:
        thread = threading.Thread(
            target=_queue_log_loop, args=(stop_event, interval), daemon=True,
            name="customization-stage-queue-log",
        )
        thread.start()
        threads.append(thread)
    return threads


# The harness tenant-repository lease defaults to 120 seconds. An ambiguous
# transport loss must not create a second author session while that lease can
# still be live, so every redispatch waits beyond that horizon.
DEFAULT_RETRY_SECONDS = 135.0


def worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4()}"


class StageLeaseGuard:
    def __init__(
        self, store: Any, change: Any, owner: str, lease_seconds: float
    ) -> None:
        self.store = store
        self.change = change
        self.owner = owner
        self.lease_seconds = lease_seconds
        self.stop = threading.Event()
        self.lost = threading.Event()
        self.thread = threading.Thread(
            target=self._renew,
            name=f"customization-stage-lease-{change.change_set_id}",
            daemon=True,
        )

    def start(self) -> None:
        self.thread.start()

    def close(self) -> None:
        self.stop.set()
        self.thread.join(timeout=max(self.lease_seconds / 3.0, 0.1) + 1.0)

    def _renew(self) -> None:
        interval = max(min(self.lease_seconds / 3.0, 10.0), 0.05)
        while not self.stop.wait(interval):
            try:
                if not self.store.heartbeat_stage(
                    tenant_id=self.change.tenant_id,
                    change_set_id=self.change.change_set_id,
                    owner=self.owner,
                    lease_seconds=self.lease_seconds,
                ):
                    self.lost.set()
                    return
            except Exception:  # noqa: BLE001 - loss of custody fails closed
                self.lost.set()
                return


def run_once(
    owner: str, *, service: CustomizationService | None = None,
    lease_seconds: float = DEFAULT_LEASE_SECONDS,
) -> bool:
    service = service or CustomizationService.configured()
    if not fair_claims():
        change = service.store.claim_stage(owner=owner, lease_seconds=lease_seconds)
        if change is None:
            return False
        return _run_claimed(service, change, owner, lease_seconds)
    with _claim_lock:
        change = service.store.claim_stage(
            owner=owner, lease_seconds=lease_seconds, fair=True,
            exclude_tenants=tuple(sorted(_active_tenants)),
        )
        if change is not None:
            _active_tenants.add(change.tenant_id)
    if change is None:
        return False
    try:
        return _run_claimed(service, change, owner, lease_seconds)
    finally:
        with _claim_lock:
            _active_tenants.remove(change.tenant_id)


def _run_claimed(service: CustomizationService, change: Any, owner: str, lease_seconds: float) -> bool:
    guard = StageLeaseGuard(service.store, change, owner, lease_seconds)
    guard.start()
    try:
        body = service.dispatch_stage(change)
        guard.close()
        durable = service.store.get_change_set(
            tenant_id=change.tenant_id, change_set_id=change.change_set_id
        )
        if durable.state is ChangeState.STAGED:
            return True
        if guard.lost.is_set():
            return True
        service.reconcile_stage_worker(
            change, body, lease_owner=owner,
            lease_attempt=change.stage_attempt,
        )
    except CustomizationServiceError as exc:
        guard.close()
        durable = service.store.get_change_set(
            tenant_id=change.tenant_id, change_set_id=change.change_set_id
        )
        if durable.state is ChangeState.STAGED:
            return True
        if exc.code == "customization_harness_unavailable":
            if change.stage_attempt >= 3:
                _LOG.warning(
                    "customization_stage_failed: tenant=%s change_set=%s "
                    "code=%s attempts=%s",
                    change.tenant_id, change.change_set_id, exc.code,
                    change.stage_attempt,
                )
                service.store.fail_stage_claim(
                    tenant_id=change.tenant_id,
                    change_set_id=change.change_set_id,
                    owner=owner,
                    reason_code=exc.code,
                    retryable=True,
                )
            else:
                service.store.defer_stage_claim(
                    tenant_id=change.tenant_id,
                    change_set_id=change.change_set_id,
                    owner=owner,
                    reason_code=exc.code,
                    delay_seconds=DEFAULT_RETRY_SECONDS,
                )
        else:
            # Any other refusal is terminal for this change set NOW — most
            # importantly a harness-reported authoring-job failure, whose
            # reason string rides along so stage status can answer WHY.
            reason_message = getattr(exc, "harness_reason", None)
            _LOG.warning(
                "customization_stage_failed: tenant=%s change_set=%s code=%s "
                "reason=%s",
                change.tenant_id, change.change_set_id, exc.code,
                reason_message or "-",
            )
            service.store.fail_stage_claim(
                tenant_id=change.tenant_id,
                change_set_id=change.change_set_id,
                owner=owner,
                reason_code=exc.code,
                retryable=exc.status_code >= 500,
                reason_message=reason_message,
            )
        return True
    except Exception:
        guard.close()
        service.store.defer_stage_claim(
            tenant_id=change.tenant_id,
            change_set_id=change.change_set_id,
            owner=owner,
            reason_code="customization_stage_worker_failed",
            delay_seconds=DEFAULT_RETRY_SECONDS,
        )
        return True
    finally:
        guard.close()
        try:
            terminal = service.store.get_change_set(
                tenant_id=change.tenant_id, change_set_id=change.change_set_id,
            )
            finish_stage_authority_for_change(terminal)
        except Exception as exc:
            _LOG.warning("stage_authority_cleanup_read_failed: exception=%s", type(exc).__name__)
    service.store.complete_stage_claim(
        tenant_id=change.tenant_id,
        change_set_id=change.change_set_id,
        owner=owner,
    )
    return True


def serve(
    *, poll_seconds: float = 1.0, lease_seconds: float = DEFAULT_LEASE_SECONDS,
    stop_event: threading.Event | None = None,
) -> None:
    owner = worker_id()
    stop = stop_event or threading.Event()
    while not stop.is_set():
        try:
            claimed = run_once(owner, lease_seconds=lease_seconds)
        except Exception:  # noqa: BLE001 - transient store outage; retry boundedly
            stop.wait(max(poll_seconds, 0.05))
            continue
        if not claimed:
            stop.wait(max(poll_seconds, 0.05))


def main() -> None:
    serve(
        poll_seconds=float(os.environ.get("LEAF_CUSTOMIZATION_STAGE_POLL_S", "1")),
        lease_seconds=float(os.environ.get(
            "LEAF_CUSTOMIZATION_STAGE_LEASE_S", str(DEFAULT_LEASE_SECONDS)
        )),
    )


if __name__ == "__main__":
    main()
