"""Apply host-selected, saved presentation edits to several table recipes using CAS."""
import json
import re


_COUNTS = {"both": 2, "two": 2, "three": 3, "four": 4, "five": 5}
_TARGET = re.compile(r"\b(both|two|three|four|five|\d+|all) (?:selected |inspection )*tables\b", re.I)


def _target(description):
    if not isinstance(description, str) or len(description) > 1000:
        return None
    text = description.strip()
    if not (re.match(r"(?:apply|update|use|copy|propagate)\b", text, re.I)
            and re.search(r"\bsaved (?:recipe|table) (?:edits?|changes?|settings?)\b", text, re.I)):
        return None
    target = _TARGET.search(text)
    if not target:
        return None
    count = target[1].lower()
    if count == "all":
        return 0
    size = int(count) if count.isdecimal() else _COUNTS[count]
    return size if 2 <= size <= 32 else None


def matches(description):
    return _target(description) is not None


def build_body(description):
    count = _target(description)
    if count is None:
        raise ValueError("Select multiple tables and a saved recipe edit.")
    return _BODY.replace("__COUNT__", str(count))


_BODY = """
def run(intake, params):
    if params:
        raise ValueError("Batch targets and saved edits must come from the selection.")
    selected = intake.get("selected_table_recipes")
    edits = intake.get("saved_table_edits")
    count = __COUNT__
    if (type(selected) is not list or not 2 <= len(selected) <= 32
            or (count and len(selected) != count)):
        raise ValueError("The table selection does not match the request.")
    if type(edits) is not dict or not edits or set(edits) - {"title", "row_limit", "sort"}:
        raise ValueError("Select saved table presentation edits.")
    plans = []
    seen = set()
    for record in selected:
        row = record["recipe"]
        if row["kind"] != "dataset.table" or row["id"] in seen:
            raise ValueError("Select distinct saved tables.")
        seen.add(row["id"])
        updated = dict(row)
        if "title" in edits:
            updated["title"] = edits["title"]
        if "row_limit" in edits:
            updated["props"] = {**row["props"], "row_limit": edits["row_limit"]}
        if "sort" in edits:
            keys = set(row["query"]["fields"]) | set(row["props"]["columns"])
            if any(order["field"] not in keys for order in edits["sort"]):
                raise ValueError("The selected table lacks a saved sort field.")
            updated["query"] = {**row["query"], "sort": edits["sort"]}
        plans.append({"recipe": updated, "expectedRevision": record["expectedRevision"]})
    return plans, None
"""


def edit_table_recipes(service, description, snapshots, saved_edits, tenant, session, turn):
    """Preflight all targets, then report the actual guarded result of each write."""
    from integration_tool_service import ReadFailure, failure, normalize
    import tool_loader
    import tools_fallback

    if not matches(description) or type(snapshots) is not list or not 2 <= len(snapshots) <= 32:
        return failure("invalid_input")
    try:
        encoded = json.dumps(saved_edits, allow_nan=False)
        if len(encoded) > 8192 or type(saved_edits) is not dict:
            return failure("invalid_input")
        saved_edits = json.loads(encoded)
    except (TypeError, ValueError, RecursionError):
        return failure("invalid_input")
    selected = []
    try:
        for snapshot in snapshots:
            if type(snapshot) is not dict or set(snapshot) != {"element", "revision"}:
                return failure("invalid_input")
            selected.append(normalize("elements", {"recipe": snapshot["element"],
                                                   "expectedRevision": snapshot["revision"]}))
    except ReadFailure:
        return failure("invalid_input")
    try:
        authority = service._resolve_operation("elements", tenant, session, turn).authority
    except ReadFailure as exc:
        return failure(str(exc))
    package, source, _ = tools_fallback.author_tool(description)
    if package["engine_op"] != "batch_recipe_edit":
        return failure("invalid_input")
    intake = {"selected_table_recipes": selected, "saved_table_edits": saved_edits}
    status, payload = tool_loader._run_source_in_sandbox(source, "batch_recipe_edit.py", intake, {})
    if status != "ok":
        return failure("invalid_input" if status == "tool_error" else "unavailable")
    plans, _ = payload
    if type(plans) is not list or len(plans) != len(selected):
        return failure("invalid_input")
    try:
        plans = [normalize("elements", plan) for plan in plans]
    except ReadFailure:
        return failure("invalid_input")
    for old, new in zip(selected, plans):
        if (new["recipe"]["id"] != old["recipe"]["id"]
                or new.get("expectedRevision") != old["expectedRevision"]):
            return failure("invalid_input")
    tables = []
    for plan in plans:
        outcome = service._execute("elements", plan, tenant, session, turn, authority)
        tables.append({"id": plan["recipe"]["id"], "expectedRevision": plan["expectedRevision"],
                       "outcome": outcome})
    saved = sum("result" in table["outcome"] for table in tables)
    status = "saved" if saved == len(tables) else "partial" if saved else "failed"
    if any(table["outcome"].get("error", {}).get("code") == "outcome_unknown" for table in tables):
        status = "outcome_unknown"
    return {"status": status, "tables": tables}
