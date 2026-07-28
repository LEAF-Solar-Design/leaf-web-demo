"""State records used by the control plane."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass
class Slot:
    executor_id: str
    slot_id: str
    endpoint: str
    host_epoch: int = 1
    slot_epoch: int = 1
    state: str = "STARTING"
    code_digest: str | None = None
    runtime_digest: str | None = None
    version: int = 1


@dataclass
class Claim:
    claim_id: str
    executor_id: str
    slot_id: str
    claim_epoch: int
    expires_at: datetime
    state: str = "ACTIVE"


@dataclass
class Session:
    assignment_id: str
    tenant_id: str
    session_id: str
    executor_id: str
    slot_id: str
    claim_id: str
    binding_epoch: int
    catalog_digest: str
    code_digest: str
    artifact_digest: str
    runtime_digest: str
    capability: dict
    state: str = "BINDING"
    lease_id: str | None = None
    lease_sequence: int = 0
    expires_at: datetime | None = None
    last_activity_at: datetime | None = None


@dataclass
class Lease:
    lease_id: str
    session_id: str
    executor_id: str
    slot_id: str
    claim_id: str
    host_epoch: int
    slot_epoch: int
    claim_epoch: int
    binding_epoch: int
    sequence: int
    not_before: datetime
    expires_at: datetime
    state: str = "ACTIVE"
