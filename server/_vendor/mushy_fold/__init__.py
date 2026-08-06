"""mushy-fold — the runtime fold side of the mushy-codebase pattern.

A CONSUMER (leaf-web-demo calls it a tenant; the ops brain calls it a
workspace) owns a git repo of AI-authored deterministic artifacts. This
package resolves consumer -> repo directory and folds the repo's registry
into a catalog at CALL time, so a commit to the repo IS the deployment.
Execution is deterministic: zero LLM anywhere in this package.

Extracted from leaf-web-demo (consumer #1): server/tenant_paths.py,
deps.load_tenant_repo_tools, and tool_loader's F4-hardened entry containment.
Every security property carried over is documented on the function it guards.
"""

from .ids import CONSUMER_ID_PATTERN, is_valid_consumer_id, validate_consumer_id
from .paths import resolve_consumer_repo_dir, safe_component
from .registry import load_repo_registry_tools
from .entry import is_unsafe_ref, resolve_within, resolve_entry

__all__ = [
    "CONSUMER_ID_PATTERN",
    "is_valid_consumer_id",
    "validate_consumer_id",
    "resolve_consumer_repo_dir",
    "safe_component",
    "load_repo_registry_tools",
    "is_unsafe_ref",
    "resolve_within",
    "resolve_entry",
]
