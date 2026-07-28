"""Deterministic capacity selection tests for the hermetic store."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import unittest

from executor.control_plane.store import InMemoryStore, NoCapacity, StaleFence


class Clock:
    def __init__(self):
        self.now = datetime(2026, 7, 28, 16, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now


class CapacitySelectionTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.store = InMemoryStore(clock=self.clock, heartbeat_timeout=timedelta(seconds=30))

    def _ready(self, host_id: str, slot_ids: list[str]) -> None:
        self.store.register_host(host_id, f"https://{host_id}.test", slot_ids)
        for slot_id in slot_ids:
            self.store.set_readiness(host_id, slot_id, True, "code", "runtime")

    def test_stale_missing_or_invalid_heartbeat_is_excluded(self):
        self._ready("executor-stale", ["slot-1"])
        self.clock.now += timedelta(seconds=31)

        with self.assertRaises(NoCapacity):
            self.store.candidate()

        self.store._last_heartbeat.pop("executor-stale")
        with self.assertRaises(NoCapacity):
            self.store.candidate()

        self.store._last_heartbeat["executor-stale"] = self.clock() + timedelta(seconds=1)
        with self.assertRaises(NoCapacity):
            self.store.candidate()

    def test_fresh_heartbeat_selects_ready_host(self):
        self._ready("executor-fresh", ["slot-1"])
        self.clock.now += timedelta(seconds=31)
        self.store.heartbeat("executor-fresh")

        slot = self.store.candidate()

        self.assertEqual((slot.executor_id, slot.slot_id), ("executor-fresh", "slot-1"))

    def test_candidates_rotate_across_ready_slots(self):
        self._ready("executor-one", ["slot-1", "slot-2", "slot-3"])

        selected = [self.store.candidate().slot_id for _ in range(4)]

        self.assertEqual(selected, ["slot-1", "slot-2", "slot-3", "slot-1"])

    def test_atomic_claim_uses_another_ready_slot_after_a_cas_collision(self):
        self._ready("executor-one", ["slot-1", "slot-2"])
        raced = self.store.candidate()
        self.store.claim_slot(raced.executor_id, raced.slot_id, raced.version, self.clock(), timedelta(seconds=60))

        slot, claim = self.store.claim_ready_slot(self.clock(), timedelta(seconds=60))

        self.assertEqual((slot.slot_id, claim.slot_id), ("slot-2", "slot-2"))
        with self.assertRaises(StaleFence):
            self.store.claim_slot(raced.executor_id, raced.slot_id, raced.version, self.clock(), timedelta(seconds=60))

    def test_atomic_claim_reports_true_exhaustion(self):
        self._ready("executor-one", ["slot-1"])
        slot, _ = self.store.claim_ready_slot(self.clock(), timedelta(seconds=60))
        self.assertEqual(slot.state, "CLAIMED")

        with self.assertRaises(NoCapacity):
            self.store.claim_ready_slot(self.clock(), timedelta(seconds=60))


if __name__ == "__main__":
    unittest.main()
