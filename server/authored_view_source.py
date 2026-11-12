"""Read registered view bytes from a pinned tenant Git object, without execution."""
from __future__ import annotations

import hashlib
import json
import re

import customization_service as catalogs
import deps
import platform_link
import session_store
from platform_release_policy import normalize_path, PlatformReleasePolicyError

MAX_SOURCE_BYTES = 1024 * 1024


class AuthoredViewError(Exception):
    def __init__(self, status: int, code: str):
        self.status, self.code = status, code
        super().__init__("Authored view unavailable")


def _authority(tenant, project_id, session_id):
    session = platform_link.require_project_session_access(
        session_store.get_session(session_id), tenant, write=False)
    if (not session or not session.get("org_id") or not session.get("project_id")
            or str(session["project_id"]) != project_id
            or str(session["session_id"]) != session_id):
        raise AuthoredViewError(404, "authored_view_unavailable")
    org = platform_link.require_project_access(tenant, project_id, write=False)
    if str(session["org_id"]) != str(org):
        raise AuthoredViewError(404, "authored_view_unavailable")


def _blob(bare, commit, path, limit):
    # Resolve mode and size BEFORE the unbounded helper. Reading by immutable
    # blob OID after this check cannot race a ref update or follow a symlink.
    tree = catalogs._git(bare, "ls-tree", "-l", "-z", commit, "--", path)
    rows = [row for row in tree.split("\0") if row]
    if len(rows) != 1:
        raise AuthoredViewError(422, "authored_view_invalid_source")
    metadata, _, actual_path = rows[0].partition("\t")
    fields = metadata.split()
    if (actual_path != path or len(fields) != 4 or fields[0] not in {"100644", "100755"}
            or fields[1] != "blob" or not re.fullmatch(r"[0-9a-f]{40}", fields[2])
            or not fields[3].isdigit() or int(fields[3]) > limit):
        raise AuthoredViewError(422, "authored_view_invalid_source")
    raw = catalogs._git_blob(bare, fields[2])
    if len(raw) != int(fields[3]):
        raise AuthoredViewError(409, "authored_view_source_changed")
    return raw


def resolve_authored_view(tenant, *, project_id: str, session_id: str, name: str,
                          catalog_commit: str, effective_catalog_digest: str,
                          tool_manifest_sha256: str, catalog_digest: str):
    try:
        if (not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name)
                or not re.fullmatch(r"[0-9a-f]{40}", catalog_commit)
                or not re.fullmatch(r"[0-9a-f]{64}", effective_catalog_digest)
                or not re.fullmatch(r"sha256:[0-9a-f]{64}", tool_manifest_sha256)
                or catalog_digest != tool_manifest_sha256
                or not project_id or len(project_id) > 200
                or not session_id or len(session_id) > 200):
            raise AuthoredViewError(422, "authored_view_invalid_identity")
        _authority(tenant, project_id, session_id)
        tenant_id = str(tenant)
        expected_pin = dict(catalog_commit=catalog_commit, effective_catalog_digest=effective_catalog_digest)
        if catalogs.effective_catalog_pin(tenant_id) != expected_pin:
            raise AuthoredViewError(409, "authored_view_stale_pin")
        bare = catalogs._bare_repo(tenant_id)
        registry_bytes = _blob(bare, catalog_commit, "registry.json", 8 * MAX_SOURCE_BYTES)
        if hashlib.sha256(registry_bytes).hexdigest() != effective_catalog_digest:
            raise AuthoredViewError(409, "authored_view_registry_mismatch")
        registry = json.loads(registry_bytes.decode("utf-8"))
        rows = registry.get("tools") if isinstance(registry, dict) else None
        selected = [row for row in rows if isinstance(row, dict) and row.get("name") == name] if isinstance(rows, list) else []
        if len(selected) != 1:
            raise AuthoredViewError(404, "authored_view_unavailable")
        entry = selected[0]
        winners = [(tool, source) for tool, source in deps.effective_tools_with_provenance(tenant_id)
                   if tool.get("name") == name]
        if (len(winners) != 1 or winners[0][1] != deps.TOOL_SOURCE_TENANT_REPO
                or any(winners[0][0].get(key) != value for key, value in entry.items())
                or set(winners[0][0]) - set(entry) - {"graph_input"}
                or deps.catalog_tool_digest(winners[0][0]) != tool_manifest_sha256):
            raise AuthoredViewError(409, "authored_view_manifest_mismatch")
        if entry.get("kind") != "view":
            raise AuthoredViewError(422, "authored_view_wrong_kind")
        path = normalize_path(entry.get("entry", ""))
        if not path.startswith("tools/") or any(ord(char) < 32 for char in path):
            raise AuthoredViewError(422, "authored_view_invalid_source")
        raw = _blob(bare, catalog_commit, path, MAX_SOURCE_BYTES)
        html = raw.decode("utf-8", errors="strict")
        if catalogs.effective_catalog_pin(tenant_id) != expected_pin:
            raise AuthoredViewError(409, "authored_view_stale_pin")
        _authority(tenant, project_id, session_id)
        return dict(contract="leaf.authored-view.v1", route="view", kind="view", tool=name,
                    tenant_id=tenant_id, project_id=project_id, session_id=session_id, entry=path,
                    **expected_pin, tool_manifest_sha256=tool_manifest_sha256, catalog_digest=catalog_digest,
                    media_type="text/html", size_bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest(), html=html)
    except AuthoredViewError:
        raise
    except platform_link.ProjectSessionForbidden:
        raise AuthoredViewError(403, "authored_view_forbidden") from None
    except LookupError:
        raise AuthoredViewError(404, "authored_view_unavailable") from None
    except (ValueError, TypeError, UnicodeError, PlatformReleasePolicyError):
        raise AuthoredViewError(422, "authored_view_invalid_source") from None
    except Exception:
        # Private repo paths and authority diagnostics never cross this seam.
        raise AuthoredViewError(503, "authored_view_unavailable") from None
