"""Author a read-only report over catalog metadata bound by the integration host."""
import re


def matches(description):
    prompt = description.casefold()
    catalog = re.search(r"\b(?:datasets?|connected data)\b", prompt)
    discovery = re.search(r"\b(?:show|list|which|discover|enumerate|report|identify)\b", prompt)
    return bool(catalog and discovery)


def build_body(description):
    prompt = description.casefold()
    statuses = []
    for status, words in (("stale", "stale|outdated"), ("unavailable", "unavailable|offline"),
                          ("empty", "empty"), ("ready", "ready")):
        if re.search(r"\b(?:" + words + r")\b", prompt):
            statuses.append(status)
    # Flagging a state or explicitly including it in a list keeps the other rows.
    if re.search(r"\b(?:includ(?:e|es|ing)|flag|mark|label)\b", prompt):
        statuses = []
    if not statuses and re.search(r"\bcan read\b|\breadable\b", prompt):
        statuses = ["ready", "empty", "stale"]
    return _BODY.replace("__STATUSES__", repr(tuple(statuses)))


_BODY = """
def run(intake, params):
    catalog = intake.get("project_dataset_catalog")
    if catalog is None:
        raise ValueError("This report needs the current project's turn-bound integration catalog.")
    statuses = __STATUSES__
    rows = []
    for dataset in catalog["datasets"]:
        status = dataset["status"]
        if not statuses or status in statuses:
            rows.append({**dataset, "needs_attention": status in ("stale", "unavailable")})
    return ({"datasets": rows, "datasets_scanned": len(catalog["datasets"]),
             "pages_read": catalog["pages_read"]}, None)
"""
