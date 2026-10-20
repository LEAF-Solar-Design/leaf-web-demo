"""Narrow local W1 execution policy, separate from generic tier entitlements."""
from demo_actor import is_demo_context


DEMO_SOLAR_TOOLS = frozenset({
    "solar-settings", "solar-panels-from-drawing", "solar-size-strings",
    "solar-combiners", "solar-feeders", "solar-homeruns", "solar-schedule",
    "solar-string-add", "solar-assign-equipment", "solar-string-multi-add",
    "solar-string-conductors", "solar-panel-groups", "solar-correct-string",
})


def demo_run_verdict(tenant_context, tool_name: str, params: dict) -> str | None:
    if not is_demo_context(tenant_context) or tool_name not in DEMO_SOLAR_TOOLS:
        return "demo_tool_not_allowed"
    if tool_name == "solar-size-strings" and params.get("mode") == "global":
        return "demo_paid_service_not_allowed"
    return None
