"""Monthly managed-run allowance for the Free plan, dark by default."""
from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path

from da import usage


FREE_TIER = "hosted_free"
_PRICEBOOK = Path(__file__).resolve().parent.parent / "platform" / "pricebook.json"


class AllowanceConfigError(ValueError):
    """The Free plan allowance could not be read safely."""


def enforcement_enabled(env=os.environ):
    return env.get("LEAF_FREE_ALLOWANCE_ENFORCE") == "1"


@lru_cache(maxsize=None)
def load_monthly_limit(path=_PRICEBOOK):
    """Read and cache a non-negative integer allowance per price book path."""
    try:
        limit = json.loads(Path(path).read_text(encoding="utf-8"))["plans"]["free"]["runs_per_month"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise AllowanceConfigError("Free allowance is unavailable") from exc
    if type(limit) is not int or limit < 0:
        raise AllowanceConfigError("Free allowance must be a non-negative integer")
    return limit


def monthly_refusal(tenant_id, limit, used, tool_name):
    env = usage.daily_quota_envelope(tenant_id, FREE_TIER, limit, used, tool=tool_name)
    message = (f"Managed run allowance reached: the Free plan includes {limit} "
               "managed run(s) a month. Upgrade or bring your own account to continue.")
    env.update({"message": message, "window": "month"})
    env["error"].update({"message": message, "window": "month"})
    return env
