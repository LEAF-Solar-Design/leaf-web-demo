"""Page one bounded authored result. Provider cursors remain inside the catalog scan."""
from dataclasses import asdict
import hashlib
import hmac
import json
import re

from integration_tool_service import ReadFailure


def fingerprint(value):
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def paged_report(service, description, tenant, session, turn, *, limit, page_size, sort, cursor):
    if (type(page_size) is not int or not 1 <= page_size <= 100
            or sort not in ("catalog", "title", "-title") or type(description) is not str):
        raise ReadFailure("invalid_input")
    authority = service._resolve_operation("datasets", tenant, session, turn).authority
    scope = fingerprint([asdict(authority), description, sort, page_size, limit])

    def signature(position, digest):
        message = f"{scope}:{position}:{digest}".encode("ascii")
        return hmac.digest(service._dataset_cursor_key, message, "sha256").hex()

    offset = 0
    previous = None
    if cursor is not None:
        parts = re.fullmatch(r"([1-9][0-9]{0,8}):([0-9a-f]{64}):([0-9a-f]{64})", cursor) if type(cursor) is str else None
        if not parts:
            raise ReadFailure("cursor_mismatch")
        position, previous, tag = parts.groups()
        if not hmac.compare_digest(tag, signature(position, previous)):
            raise ReadFailure("cursor_mismatch")
        offset = int(position)

    result = service._run_catalog_report("datasets", description, tenant, session, turn, limit,
                                         expected_authority=authority)
    rows = result["datasets"]
    if sort != "catalog":
        rows.sort(key=lambda row: (row["title"], row["dataset_ref"]["id"]), reverse=sort == "-title")
    digest = fingerprint(rows)
    if previous is not None and digest != previous:
        raise ReadFailure("snapshot_changed")
    end = offset + page_size
    next_cursor = f"{end}:{digest}:{signature(end, digest)}" if end < len(rows) else None
    return {**result, "datasets": rows[offset:end], "next_cursor": next_cursor}
