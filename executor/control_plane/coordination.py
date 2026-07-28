"""Best-effort Redis coordination. It never stores calls or result payloads."""
from __future__ import annotations

import json
import secrets
from datetime import datetime


class CoordinationUnavailable(RuntimeError):
    pass


class RedisCoordination:
    """Small adapter for locks, liveness hints, and outbox notifications only."""
    def __init__(self, client) -> None:
        self.client = client

    def _call(self, method, *args, **kwargs):
        try:
            return getattr(self.client, method)(*args, **kwargs)
        except Exception as exc:
            raise CoordinationUnavailable("Redis coordination is unavailable") from exc

    def check_available(self) -> None:
        self._call("ping")

    def acquire_claim_lock(self, executor_id: str, slot_id: str) -> str:
        value = secrets.token_urlsafe(32)
        if not self._call("set", f"ix:claim-lock:{executor_id}:{slot_id}", value, nx=True, ex=10):
            raise CoordinationUnavailable("claim lock is contended")
        return value

    def release_claim_lock(self, executor_id: str, slot_id: str, value: str) -> None:
        # Atomic compare-delete works with either byte or string response mode.
        key = f"ix:claim-lock:{executor_id}:{slot_id}"
        self._call("eval", "if redis.call('get',KEYS[1]) == ARGV[1] then return redis.call('del',KEYS[1]) else return 0 end", 1, key, value)

    def publish_hint(self, kind: str, entity_id: str, version: int) -> None:
        self._call("xadd", "ix:events:control", {"kind": kind, "entity_id": entity_id, "version": str(version)}, maxlen=100000, approximate=True)

    def host_live(self, executor_id: str, epoch: int) -> None:
        self._call("set", f"ix:host:{executor_id}:live", json.dumps({"host_epoch": epoch, "seen_at": datetime.utcnow().isoformat()}), ex=30)
