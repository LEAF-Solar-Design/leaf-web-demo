"""Leaf mirror of the reviewed tenant-broker standard-service wire contract."""
from __future__ import annotations

import re
from typing import Any


ARTIFACT_ID = re.compile(r"^[A-Za-z0-9_-]{16,256}$")
MAX_ARTIFACT_IDS = 64


def valid_artifact_ids(value: Any) -> bool:
    return (
        isinstance(value, list)
        and len(value) <= MAX_ARTIFACT_IDS
        and all(
            isinstance(artifact_id, str)
            and ARTIFACT_ID.fullmatch(artifact_id) is not None
            for artifact_id in value
        )
    )
