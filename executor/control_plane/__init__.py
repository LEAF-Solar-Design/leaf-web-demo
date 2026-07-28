"""Instant execution control-plane prototype.

This package allocates warm executor slots and signs short lived leases.  It
does not load, import, execute, or proxy user code or invocation payloads.
"""

from .service import ControlPlane, ControlPlaneError

__all__ = ["ControlPlane", "ControlPlaneError"]
