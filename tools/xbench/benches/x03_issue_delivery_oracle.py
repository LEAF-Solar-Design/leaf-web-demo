"""Reconcile requests and retries to independently recorded receiver effects."""
import hashlib


def delivery_quality(attempts, effects):
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
            if key in index and (unique or index[key] != digest):
                return "FAIL"
            index[key] = digest
    return "PASS" if wanted == received else "FAIL"
