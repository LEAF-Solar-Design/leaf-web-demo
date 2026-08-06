"""Canonical drawing identifiers shared by execution lanes.

The bundled demonstration drawing predates the version store. Its source file
is registered as ``rooftop_demo`` while the tenant-scoped version store calls
the same drawing ``demo``. Keep that compatibility mapping here so live APS,
batch, instant, and session code do not each invent a different translation.

Unknown identifiers pass through unchanged. This module does not resolve a
tenant-owned drawing or authorize access to one. The lane-specific store or
registry remains responsible for that fail-closed lookup.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import FrozenSet, Optional


@dataclass(frozen=True)
class DrawingIdentity:
    source_id: str
    store_id: str
    aliases: FrozenSet[str]


CURATED_DEMO = DrawingIdentity(
    source_id="rooftop_demo",
    store_id="demo",
    aliases=frozenset({"rooftop_demo", "rooftop-demo", "demo"}),
)


def curated_identity(drawing_id: object) -> Optional[DrawingIdentity]:
    if isinstance(drawing_id, str) and drawing_id in CURATED_DEMO.aliases:
        return CURATED_DEMO
    return None


def source_id(drawing_id: str) -> str:
    identity = curated_identity(drawing_id)
    return identity.source_id if identity else drawing_id


def store_id(drawing_id: str) -> str:
    identity = curated_identity(drawing_id)
    return identity.store_id if identity else drawing_id

