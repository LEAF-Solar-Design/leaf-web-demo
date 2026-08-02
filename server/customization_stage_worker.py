"""Durable worker for queued customization stage requests."""
from __future__ import annotations

import os
import socket
import threading
import uuid
from typing import Any

from customization_models import ChangeState
from customization_service import CustomizationService, CustomizationServiceError


DEFAULT_LEASE_SECONDS = 30.0
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
    change = service.store.claim_stage(owner=owner, lease_seconds=lease_seconds)
    if change is None:
        return False
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
            service.store.fail_stage_claim(
                tenant_id=change.tenant_id,
                change_set_id=change.change_set_id,
                owner=owner,
                reason_code=exc.code,
                retryable=exc.status_code >= 500,
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
