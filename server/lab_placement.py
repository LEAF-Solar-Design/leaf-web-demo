"""Mirror of web/src/lab/runtime/placement.js; placement is derived, not supplied."""
from lab_ops import has_operator

SETTINGS_DIGEST_ID = "kashbot2-config"
PLACEMENTS = ("here", "browser", "server", "bot", "host")
HOST_BLOCK_IDS = frozenset({"studio-jobs", "studio-capabilities", "host-source", "host-intent"})


def placement(manifest, *, trusted_ids=None) -> str:
    if trusted_ids is None:
        # The evaluator imports placement; resolve its snapshot only at call time.
        from lab_evaluate import trusted_registry
        trusted_ids = {item["id"] for item in trusted_registry().values()}
    block_id = manifest["id"]
    if manifest.get("residency") == "host" or block_id in HOST_BLOCK_IDS:
        return "host"
    operator = (manifest.get("kind") == "builtin" and manifest.get("effect") == "pure"
                and has_operator(block_id))
    if ((operator and block_id in trusted_ids)
            or (block_id == SETTINGS_DIGEST_ID and manifest.get("kind") == "settings"
                and manifest.get("effect") == "pure")):
        return "here"
    if operator:
        return "browser"
    return "bot" if block_id in trusted_ids else "server"
