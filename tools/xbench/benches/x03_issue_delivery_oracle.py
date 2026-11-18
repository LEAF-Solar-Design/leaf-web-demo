"""Reconcile requests and retries to independently recorded receiver effects."""
def delivery_quality(attempts, effects):
    wanted, received = {}, {}
    for rows, index, unique in ((attempts, wanted, False), (effects, received, True)):
        if not isinstance(rows, list) or not rows:
            return "INCOMPLETE"
        for row in rows:
            fields = ("drawing", "revision", "payload_digest")
            if not isinstance(row, dict) or not all(isinstance(row.get(k), str) and row[k] for k in fields):
                return "INCOMPLETE"
            key = row["drawing"], row["revision"]
            digest = row["payload_digest"]
            if key in index and (unique or index[key] != digest):
                return "FAIL"
            index[key] = digest
    return "PASS" if wanted == received else "FAIL"
