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


def tombstone_replay_quality(events, recreation_grants, receiver_states):
    """Compare each receiver existence snapshot to monotonic, per-drawing revision replay.

    The initial inventory is empty. Grants identify exact (drawing, revision)
    recreation events independently of the deliveries.
    """
    if not isinstance(events, list) or not events or not isinstance(receiver_states, list) or recreation_grants is None:
        return "INCOMPLETE"
    if len(events) != len(receiver_states):
        return "INCOMPLETE"
    if not isinstance(recreation_grants, (set, frozenset)):
        return "INCOMPLETE"
    latest, history, live, deleted = {}, {}, set(), set()
    for event, snapshot in zip(events, receiver_states):
        if not isinstance(event, dict) or any(event.get(k) is None for k in ("drawing", "revision", "action")):
            return "INCOMPLETE"
        drawing, rev, action = event["drawing"], event["revision"], event["action"]
        if (not isinstance(drawing, str) or not drawing or type(rev) is not int or rev < 0
                or action not in ("upsert", "delete", "recreate")):
            return "FAIL"
        if history.setdefault((drawing, rev), action) != action:
            return "FAIL"
        if rev > latest.get(drawing, -1):
            if action == "delete":
                live.discard(drawing)
                deleted.add(drawing)
            elif drawing not in deleted or (action == "recreate" and (drawing, rev) in recreation_grants):
                live.add(drawing)
                deleted.discard(drawing)
            latest[drawing] = rev
        if not isinstance(snapshot, list) or not all(isinstance(k, str) for k in snapshot):
            return "INCOMPLETE"
        if len(snapshot) != len(live) or set(snapshot) != live:
            return "FAIL"
    return "PASS"


def batch_ack_quality(expected, acknowledgements, effects):
    """Match accepted item revisions to independent, unique committed effects across retries."""
    if not all(isinstance(rows, list) for rows in (expected, acknowledgements, effects)) or not expected:
        return "INCOMPLETE"
    wanted, accepted, committed = set(), set(), set()
    for rows, target in ((expected, wanted), (acknowledgements, accepted), (effects, committed)):
        for row in rows:
            if not isinstance(row, dict) or not all(isinstance(row.get(k), str) and row[k] for k in ("item", "revision")):
                return "INCOMPLETE"
            key = row["item"], row["revision"]
            if target is not wanted and key not in wanted:
                return "FAIL"
            if target is accepted:
                if row.get("state") in ("pending", "rejected"):
                    continue
                if row.get("state") != "accepted":
                    return "INCOMPLETE"
            else:
                if key in target:
                    return "FAIL"
                if target is committed and row.get("state") != "committed":
                    return "INCOMPLETE"
            target.add(key)
    return "PASS" if accepted == committed == wanted else "INCOMPLETE"


def predecessor_quality(events, final_revision):
    """A complete delivery has one explicit root and a single unbroken successor chain."""
    if not isinstance(events, list) or not events or not isinstance(final_revision, str):
        return "INCOMPLETE"
    parents = {}
    for row in events:
        if not isinstance(row, dict) or "predecessor" not in row or not isinstance(row.get("revision"), str):
            return "INCOMPLETE"
        revision, parent = row["revision"], row["predecessor"]
        if not revision or (parent is not None and (not isinstance(parent, str) or not parent)):
            return "FAIL"
        if revision in parents and parents[revision] != parent:
            return "FAIL"
        parents[revision] = parent
    children = {}
    for node, parent in parents.items():
        if parent is not None and parent not in parents:
            return "INCOMPLETE"
        if parent in children:
            return "FAIL"
        children[parent] = node
    visited = set()
    node = None
    while node in children:
        node = children[node]
        if node in visited:
            return "FAIL"
        visited.add(node)
    return "PASS" if visited == parents.keys() and node == final_revision else "FAIL"


def atomic_batch_quality(manifests, previous, target, events):
    """Read a recorded stream of commit markers and consumer-visible snapshots."""
    import re
    if not isinstance(manifests, dict) or not isinstance(events, list) or not events:
        return "INCOMPLETE"
    for revision in (previous, target):
        members = manifests.get(revision)
        if not isinstance(members, dict) or not members:
            return "INCOMPLETE"
        if not all(isinstance(v, str) and re.fullmatch("[0-9a-f]{64}", v) for v in members.values()):
            return "FAIL"
    visible = previous
    committed = observed = False
    for event in events:
        if not isinstance(event, dict) or not event.get("kind"):
            return "INCOMPLETE"
        if event["kind"] == "commit":
            if event.get("revision") is None:
                return "INCOMPLETE"
            if event["revision"] != target:
                return "FAIL"
            visible, committed = target, True
        elif event["kind"] == "snapshot":
            if not isinstance(event.get("members"), dict) or event.get("revision") is None:
                return "INCOMPLETE"
            if event["revision"] != visible or event["members"] != manifests[visible]:
                return "FAIL"
            observed |= committed
        else:
            return "FAIL"
    return "PASS" if observed else "INCOMPLETE"


def generation_receipt_quality(events):
    """Check receipt credit against the active generation of each drawing at delivery time."""
    if not isinstance(events, list) or not events:
        return "INCOMPLETE"
    active, seen, credited = {}, set(), set()
    receipts = 0
    for row in events:
        if not isinstance(row, dict) or not all(isinstance(row.get(k), str) and row[k] for k in ("drawing", "generation", "kind")):
            return "INCOMPLETE"
        drawing, generation = row["drawing"], row["generation"]
        identity = drawing, generation
        if row["kind"] == "create":
            if drawing in active or identity in seen:
                return "FAIL"
            active[drawing] = generation
            seen.add(identity)
        elif row["kind"] == "delete":
            if active.get(drawing) != generation:
                return "FAIL"
            del active[drawing]
        elif row["kind"] == "receipt":
            if type(row.get("credited")) is not bool or not isinstance(row.get("effect"), str) or not row["effect"]:
                return "INCOMPLETE"
            if identity not in seen:
                return "INCOMPLETE"
            key = drawing, generation, row["effect"]
            eligible = active.get(drawing) == generation and key not in credited
            if row["credited"] != eligible:
                return "FAIL"
            if eligible:
                credited.add(key)
            receipts += 1
        else:
            return "INCOMPLETE"
    return "PASS" if receipts else "INCOMPLETE"
