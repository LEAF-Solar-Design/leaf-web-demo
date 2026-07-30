"""Scheduled production reconciler for expired or idle instant bindings."""
from __future__ import annotations

from datetime import timedelta
import time
from typing import Callable

from .production import ProductionConfigurationError, create_control_plane, load_production_settings


def run_reaper(control_plane, *, interval_seconds: int,
               idle_timeout_seconds: int | None = None,
               accounting_stale_timeout_seconds: int = 120,
               sleep: Callable[[float], None] = time.sleep,
               max_cycles: int | None = None) -> int:
    """Run bounded reconciliation passes and retry failures after one interval.

    ``max_cycles`` exists for deterministic unit tests. Production leaves it as
    ``None`` and stops through normal process supervision or KeyboardInterrupt.
    """
    if interval_seconds < 1 or interval_seconds > 300:
        raise ValueError("interval_seconds must be between 1 and 300")
    if max_cycles is not None and max_cycles < 1:
        raise ValueError("max_cycles must be positive")
    idle_timeout = timedelta(seconds=idle_timeout_seconds) if idle_timeout_seconds else None
    if accounting_stale_timeout_seconds < 30 or accounting_stale_timeout_seconds > 3600:
        raise ValueError("accounting_stale_timeout_seconds must be between 30 and 3600")
    accounting_stale_timeout = timedelta(seconds=accounting_stale_timeout_seconds)
    cycles = 0
    try:
        while max_cycles is None or cycles < max_cycles:
            try:
                control_plane.reconcile(idle_timeout=idle_timeout)
                recover = getattr(control_plane, "recover_stale_accounting", None)
                if recover is not None:
                    recover(stale_timeout=accounting_stale_timeout)
            except Exception:
                # A durable release outbox keeps retries safe. Do not log the
                # exception because drivers may include connection credentials.
                pass
            cycles += 1
            if max_cycles is None or cycles < max_cycles:
                sleep(interval_seconds)
    except KeyboardInterrupt:
        return 0
    return 0


def main() -> int:
    try:
        settings = load_production_settings()
        control_plane = create_control_plane()
    except ProductionConfigurationError:
        return 2
    return run_reaper(
        control_plane,
        interval_seconds=settings.reaper_interval_seconds,
        idle_timeout_seconds=settings.reaper_idle_timeout_seconds,
        accounting_stale_timeout_seconds=settings.accounting_stale_timeout_seconds,
    )


if __name__ == "__main__":
    raise SystemExit(main())
