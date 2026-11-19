"""Author one bounded edit to an inspection table and save it at its observed revision."""
import re


_NUMBERS = "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty".split()
_CONFLICT_CLAUSE = re.compile(
    r"(?:,?\s+(?:without overwriting another editor|and warn me if (?:the table|it) changed|"
    r"unless another editor saved first))?[.!?]*$", re.I)


def _edit(description):
    if not isinstance(description, str) or not 0 < len(description) <= 1000:
        return None
    text = _CONFLICT_CLAUSE.sub("", description.strip(), count=1)
    rename = re.fullmatch(
        r'(?:rename|retitle) (?:my |the |this )?inspection (?:table|recipe) (?:to|as) (.+)', text, re.I)
    if rename:
        title = rename[1].strip()
        if len(title) >= 2 and title[0] == title[-1] and title[0] in ("'", '"'):
            title = title[1:-1]
        if title and len(title) <= 120 and title.isprintable():
            return "title", title
    rows = re.fullmatch(
        r"(?:show|display) (\d{1,4}|[a-z]+) inspection rows per page|"
        r"set (?:my |the |this )?inspection (?:table|recipe) to (?:show |display )?"
        r"(\d{1,4}|[a-z]+) rows per page", text, re.I)
    if rows:
        raw = (rows[1] or rows[2]).casefold()
        if raw.isdecimal():
            count = int(raw)
        elif raw in _NUMBERS:
            count = _NUMBERS.index(raw)
        else:
            return None
        if 1 <= count <= 1000:
            return "row_limit", count
    order = re.fullmatch(
        r"sort (?:the |my )?inspection (?:rows by )?([a-z_][a-z0-9_]*) "
        r"(highest first|lowest first|ascending|descending)", text, re.I)
    if order:
        direction = "desc" if order[2].casefold() in ("highest first", "descending") else "asc"
        return "sort", (order[1].casefold(), direction)
    return None


def matches(description):
    return _edit(description) is not None


def build_body(description):
    edit = _edit(description)
    if edit is None:
        raise ValueError("Unsupported inspection table edit.")
    # repr escapes the literal argument before embedding it in a standalone script.
    return _BODY.replace("__EDIT__", repr(edit))


_BODY = r"""
import re


def run(intake, params):
    snapshot = intake.get("inspection_recipe") or {}
    element = snapshot.get("element") or {}
    revision = snapshot.get("revision")
    if type(revision) is not int or revision < 1:
        raise ValueError("Select a saved recipe revision before editing.")
    label = element.get("id", "") + " " + element.get("title", "")
    if element.get("kind") != "dataset.table" or "inspection" not in re.findall(r"[a-z0-9]+", label.lower()):
        raise ValueError("Select the inspection table to edit.")
    field, value = __EDIT__
    edited = dict(element)
    if field == "title":
        edited["title"] = value
    elif field == "row_limit":
        edited["props"] = {**element["props"], "row_limit": value}
    else:
        requested, direction = value
        query = element["query"]
        available = list(dict.fromkeys(query["fields"] + element["props"]["columns"]))
        # An exact field ID wins. A plural in the request may name a singular column.
        matches = [f for f in available if f.casefold() == requested]
        if not matches and requested.endswith("s"):
            matches = [f for f in available if f.casefold() == requested[:-1]]
        if len(matches) != 1:
            raise ValueError("Choose an existing unambiguous inspection field to sort.")
        column = matches[0]
        order = [{"field": column, "direction": direction}]
        order.extend(row for row in query.get("sort", []) if row["field"] != column)
        edited["query"] = {**query, "sort": order}
    return {"recipe": edited, "expectedRevision": revision}, None
"""


def prepare_inspection_edit(description, snapshot):
    """Author and execute a plan from a normalized parent without writing."""
    from integration_tool_service import ReadFailure, normalize
    import tool_loader
    import tools_fallback

    if not matches(description) or type(snapshot) is not dict or set(snapshot) != {"element", "revision"}:
        raise ReadFailure("invalid_input")
    selected = normalize("elements", {"recipe": snapshot["element"], "expectedRevision": snapshot["revision"]})
    package, source, _ = tools_fallback.author_tool(description)
    if package["engine_op"] != "inspection_recipe_edit":
        raise ReadFailure("invalid_input")
    intake = {"inspection_recipe": {"element": selected["recipe"], "revision": selected["expectedRevision"]}}
    status, payload = tool_loader._run_source_in_sandbox(source, "inspection_recipe.py", intake, {})
    if status != "ok":
        raise ReadFailure("invalid_input" if status == "tool_error" else "unavailable")
    args, _ = payload
    if (args.get("expectedRevision") != selected["expectedRevision"]
            or args.get("recipe", {}).get("id") != selected["recipe"]["id"]):
        raise ReadFailure("invalid_input")
    return args, intake["inspection_recipe"]


def edit_inspection_recipe(service, description, snapshot, tenant, session, turn, *,
                           issue_retry_key=False, retry_key=None, read_revision=None, record_attempt=None):
    """Prepare an edit offline; the existing guarded CAS service owns the write."""
    from integration_tool_service import ReadFailure, failure, normalize

    try:
        args, selected_snapshot = prepare_inspection_edit(description, snapshot)
    except ReadFailure as exc:
        return failure(str(exc))
    if not issue_retry_key and retry_key is None and record_attempt is None:
        return service.execute("elements", args, tenant, session, turn)

    from authored_recipe_retry import JOURNAL_FORMAT_VERSION, recover_recipe_save, request_retry_key

    try:
        authority = service._resolve_operation("elements", tenant, session, turn).authority
        request = normalize("elements", args)
    except ReadFailure as exc:
        return failure(str(exc))
    # This is a request identity. Authority still comes from the active grant.
    key = request_retry_key(authority, request)
    if retry_key is not None:
        if retry_key != key:
            return failure("conflict")
        # A retry only reads the immutable successor, even after the head advances.
        return recover_recipe_save(service, "Recover my recipe save receipt.", request,
                                   tenant, session, turn, read_revision=read_revision,
                                   expected_authority=authority)
    if record_attempt is not None:
        # The host commits this intent before dispatch. A crash can then lose
        # the entire reply without losing the identity needed for readback.
        try:
            record_attempt(authority, {"format_version": JOURNAL_FORMAT_VERSION,
                                       "description": description, "snapshot": selected_snapshot,
                                       "retry_key": key})
        except Exception:
            return failure("unavailable")
    outcome = service._execute("elements", request, tenant, session, turn, authority)
    if "result" in outcome or outcome.get("error", {}).get("code") == "outcome_unknown":
        return {**outcome, "retry_key": key}
    return outcome
