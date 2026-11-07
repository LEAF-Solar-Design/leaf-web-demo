"""Runtime posture readers must never install hooks or import broker."""
import os


def _production_runtime() -> bool:
    return os.environ.get("LEAF_RUNTIME_ENV", "").strip().lower() == "production"


def _deployed_runtime() -> bool:
    """True when LEAF_RUNTIME_ENV names a real deployed posture.

    Staging and production are the two deployed postures (the same primary
    signal routers/demand.py::_deployed_runtime uses; the deploy contract
    requires LEAF_RUNTIME_ENV in every deployed task definition). Local, demo,
    dev, and test runs leave the variable unset or use another value and never
    enter the deployed branch.
    """
    return os.environ.get("LEAF_RUNTIME_ENV", "").strip().lower() in (
        "staging", "production")


def _authored_execution_enabled() -> bool:
    raw = os.environ.get("LEAF_AUTHORED_EXECUTION")
    if raw is None:
        return not _production_runtime()
    return raw.strip().lower() in ("1", "true", "yes", "on")
