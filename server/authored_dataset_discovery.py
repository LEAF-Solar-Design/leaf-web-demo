"""Author a read-only report over catalog metadata bound by the integration host."""
import re


def matches(description):
    prompt = description.casefold()
    catalog = re.search(r"\b(?:datasets?|connected data)\b", prompt)
    discovery = re.search(r"\b(?:show|list|which|discover|enumerate|report|identify)\b", prompt)
    return bool(catalog and discovery)


_STATUS_NAMES = {"ready": "ready", "empty": "empty", "stale": "stale", "outdated": "stale",
                 "unavailable": "unavailable", "offline": "unavailable"}
_WORD = r"\b(?:" + "|".join(_STATUS_NAMES) + r")\b"
_NEGATIVE = r"not|excluding|exclude|except(?: for)?|without|other than|neither"
_FLAG = r"flag|mark|label"
_ITEM = _WORD + r"(?:\s+(?:ones|datasets|results))?"
_STATUS_GROUP = re.compile(
    r"(?:\b(?P<mode>" + _NEGATIVE + "|" + _FLAG + r"|including only|include only|including|include)\s+"
    r"(?:(?:the|those|ones|datasets|that|which|are|is|only)\s+)*)?"
    r"(?P<items>" + _ITEM + r"(?:\s*(?:,\s*(?:(?:and|or)\s+)?|\b(?:and|or|nor)\s+)"
    + _ITEM + r")*)")


def requires_snapshot(description):
    return bool(re.search(r"\bsnapshot\b|\b(?:consistent|same|single) (?:catalog |inventory )?"
                          r"(?:view|version|revision|inventory|catalog)\b", description, re.I))


def selected_statuses(description):
    prompt = description.casefold()
    selected, excluded, additional = set(), set(), set()
    for group in _STATUS_GROUP.finditer(prompt):
        mode = group["mode"] or ""
        statuses = {_STATUS_NAMES[word] for word in re.findall(_WORD, group["items"])}
        if re.fullmatch(_NEGATIVE, mode):
            excluded.update(statuses)
        elif mode == "including" or (mode == "include" and re.search(r"\ball\s+(?:connected\s+)?datasets\b", prompt)):
            additional.update(statuses)
        elif not re.fullmatch(_FLAG, mode):
            selected.update(statuses)
    if not selected:
        selected = set(_STATUS_NAMES.values())
        if re.search(r"\bcan read\b|\breadable\b", prompt):
            selected.discard("unavailable")
    return tuple(sorted((selected | additional) - excluded))


def provider_status_filter(description):
    # Reports normally filter the complete inventory locally. Only explicit
    # source-side filtering changes the contract for each returned page.
    location = r"(?:provider|source|server)"
    modifier = r"\b" + location + r"[- ]side\s+filter(?:s|ing)?\b"
    directive = r"\bfilter(?:ed|ing)?\s+(?:at|by|on)\s+(?:the\s+)?" + location + r"\b"
    if re.search(modifier + "|" + directive, description, re.I):
        return selected_statuses(description)
    return None


TIMESTAMPS = ("fetched_at", "source_updated_at", "synced_at")
REPORT_COLUMNS = {"dataset_ref", "title", "status", "fields", "freshness", "needs_attention", *TIMESTAMPS}


def report_columns(description):
    clause = re.search(r"\b(?:with|return|show|include)\s+(?:the\s+)?(?:columns|fields)\s*:?\s+([^.!?]+)",
                       description, re.I)
    if not clause:
        return None
    names = re.split(r"\s*,\s*(?:and\s+)?|\s+and\s+", clause[1].strip().casefold())
    names = [name.strip().replace(" ", "_") for name in names]
    if any(name not in REPORT_COLUMNS for name in names):
        return ()
    return tuple(dict.fromkeys(["dataset_ref", *names]))


def build_body(description):
    return _BODY.replace("__STATUSES__", repr(selected_statuses(description))).replace(
        "__REQUIRE_SNAPSHOT__", repr(requires_snapshot(description))).replace(
        "__COLUMNS__", repr(report_columns(description)))


_BODY = """
def run(intake, params):
    catalog = intake.get("project_dataset_catalog")
    if catalog is None:
        raise ValueError("This report needs the current project's turn-bound integration catalog.")
    if __REQUIRE_SNAPSHOT__ and catalog.get("snapshot_verified") is not True:
        raise ValueError("This report needs a verified catalog snapshot.")
    statuses = __STATUSES__
    columns = __COLUMNS__
    if columns == ():
        raise ValueError("Unknown dataset report column.")
    rows = []
    for dataset in catalog["datasets"]:
        status = dataset["status"]
        if status in statuses:
            row = {**dataset, "needs_attention": status in ("stale", "unavailable")}
            rows.append(row if columns is None else {key: row[key] for key in columns if key in row})
    return ({"datasets": rows, "datasets_scanned": len(catalog["datasets"]),
             "pages_read": catalog["pages_read"]}, None)
"""
