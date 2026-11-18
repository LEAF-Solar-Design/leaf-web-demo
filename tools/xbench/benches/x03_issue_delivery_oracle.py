"""Reconcile requests and retries to independently recorded receiver effects."""
import hashlib


def delivery_quality(attempts, effects, *, revision_order=None, final_state=None):
    wanted, received = {}, {}
    for rows, index, unique in ((attempts, wanted, False), (effects, received, True)):
        if not isinstance(rows, list) or not rows:
            return "INCOMPLETE"
        for row in rows:
            fields = ("drawing", "revision", "payload_digest", "destination")
            if not isinstance(row, dict) or not all(isinstance(row.get(k), str) and row[k] for k in fields):
                return "INCOMPLETE"
            key = row["destination"], row["drawing"], row["revision"]
            digest = row["payload_digest"]
            payload = row.get("payload_bytes")
            if not isinstance(payload, bytes):
                return "INCOMPLETE"
            if hashlib.sha256(payload).hexdigest() != digest:
                return "FAIL"
            if unique:
                # Legacy effects are committed receiver records. Explicit lifecycle
                # events count only once the receiver attests application.
                if "http_status" in row and "state" not in row:
                    return "INCOMPLETE"
                state = row.get("state", "applied")
                if state in ("accepted", "pending"):
                    continue
                if state != "applied":
                    return "INCOMPLETE"
            if key in index and (unique or index[key] != digest):
                return "FAIL"
            index[key] = digest
    if wanted != received:
        return "FAIL"
    if revision_order is None and final_state is None:
        return "PASS"
    if not isinstance(revision_order, list) or not isinstance(final_state, list):
        return "INCOMPLETE"
    if not all(isinstance(r, str) for r in revision_order) or len(set(revision_order)) != len(revision_order):
        return "INCOMPLETE"
    rank = {r: i for i, r in enumerate(revision_order)}
    latest = {}
    for (dest, drawing, rev), digest in wanted.items():
        if rev not in rank:
            return "INCOMPLETE"
        key = dest, drawing
        if key not in latest or rank[rev] > rank[latest[key][0]]:
            latest[key] = rev, digest
    actual = {}
    for row in final_state:
        if not isinstance(row, dict) or not all(isinstance(row.get(k),str) and row[k]
                for k in ("destination","drawing","revision","payload_digest")):
            return "INCOMPLETE"
        key = row["destination"], row["drawing"]
        if key in actual:
            return "FAIL"
        actual[key] = row["revision"], row["payload_digest"]
    return "PASS" if actual == latest else "FAIL"
