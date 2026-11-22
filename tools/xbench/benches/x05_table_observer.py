"""Bounded normal-UI observations; ownership of the synchronous page stays with caller."""

import csv
from io import StringIO
from pathlib import Path
import re


BASE = "https://platform-staging.leafdesign.ai"
METRICS = ("panels", "optimizers", "inverters", "strings", "kw_dc")
TITLES = ("Panels", "Optimizers", "Inverters", "Strings", "kW DC")
COLUMNS = ("metric", "studio", "oracle", "delta", "verdict")
CSV_NAME = "x05-reconciliation.csv"
MAX_BYTES = 16384


def _bounded_text(value):
    return (type(value) is str and 0 < len(value) <= 256 and value.strip() == value
            and not any(ord(c) < 32 or ord(c) == 127 for c in value))


def _number(value, *, count=False, signed=False, canonical=False):
    if type(value) is not str or len(value) > 38:
        raise ValueError
    pattern = r"-?" if signed else ""
    pattern += r"[0-9]{1,18}" + ("" if count else r"(?:\.[0-9]{1,18})?")
    if re.fullmatch(pattern, value) is None:
        raise ValueError
    negative = value.startswith("-")
    whole, _, fraction = value.lstrip("-").partition(".")
    whole = whole.lstrip("0") or "0"
    fraction = fraction.rstrip("0")
    normalized = whole + ("." + fraction if fraction else "")
    if count and int(whole) > 2**53 - 1:
        raise ValueError
    if negative and normalized != "0":
        normalized = "-" + normalized
    if canonical and normalized != value:
        raise ValueError
    return normalized


def _inputs(base, project_name, version_id, oracle_values, grouping_label):
    if (base != BASE or not _bounded_text(project_name) or not _bounded_text(version_id)
            or (grouping_label is not None and not _bounded_text(grouping_label))
            or type(oracle_values) is not dict or set(oracle_values) != set(METRICS)):
        raise ValueError
    values = {}
    for key in METRICS:
        value = oracle_values[key]
        if key == "kw_dc" and value is None:
            values[key] = ""
        else:
            if type(value) is int:
                value = str(value)
            values[key] = _number(value, count=key != "kw_dc", canonical=True)
    return values


def _select(page, project_name, version_id):
    page.get_by_role("button", name=re.compile(r"^Projects: change project\.")).click()
    page.get_by_role("menuitem", name=re.compile(re.escape(project_name))).first.click()
    page.get_by_role("combobox", name="Canonical drawing version", exact=True).select_option(value=version_id)
    region = page.get_by_role("region", name="Engineering reconciliation", exact=True)
    region.wait_for(state="visible")
    return region


def _read(region):
    region.get_by_role("button", name="Read saved design", exact=True).click()
    status = region.get_by_text(re.compile(r"^Saved design revision [0-9]+\.$"))
    status.wait_for(state="visible")
    match = re.fullmatch(r"Saved design revision ([0-9]+)\.", status.inner_text())
    if match is None or int(match[1]) > 2**53 - 1:
        raise ValueError
    return int(match[1])


def _drafts(region):
    result = {}
    for key, title in zip(METRICS, TITLES):
        value = region.get_by_role("textbox", name=title + " engineering report/BOM", exact=True).input_value()
        result[key] = "" if value == "" else _number(value, count=key != "kw_dc")
    return result


def _rows(region):
    table = region.get_by_role("table", name="Saved Studio vs engineering report/BOM totals", exact=True)
    body = table.locator("tbody tr")
    if body.count() != 5:
        raise ValueError
    rows, seen = [], set()
    for index in range(5):
        row = body.nth(index)
        headers, cells = row.get_by_role("rowheader"), row.get_by_role("cell")
        if headers.count() != 1 or cells.count() != 4:
            raise ValueError
        title = headers.inner_text()
        if title not in TITLES or title in seen:
            raise ValueError
        seen.add(title)
        key = METRICS[TITLES.index(title)]
        studio, delta = cells.nth(0).inner_text(), cells.nth(2).inner_text()
        oracle = cells.nth(1).get_by_role("textbox").input_value()
        # Read the verdict independently of the small explanatory reason.
        verdict_cell = cells.nth(3)
        text = verdict_cell.inner_text()
        small = verdict_cell.locator("small")
        if small.count() > 1:
            raise ValueError
        if small.count():
            reason = small.inner_text()
            if not text.endswith(reason):
                raise ValueError
            text = text[:-len(reason)] if reason else text
        text = text.strip()
        if text not in ("PASS", "FAIL", "Unavailable"):
            raise ValueError
        rows.append(dict(metric=key,
                         studio="" if studio == "Unavailable" else _number(studio, signed=True, canonical=True),
                         oracle="" if oracle == "" else _number(oracle, count=key != "kw_dc"),
                         delta="" if delta == "Unavailable" else _number(delta, signed=True, canonical=True),
                         verdict="UNREADABLE" if text == "Unavailable" else text))
    return sorted(rows, key=lambda row: METRICS.index(row["metric"]))


def _validate_csv(text):
    records = list(csv.reader(StringIO(text, newline=""), strict=True))
    if len(records) != 6 or records[0] != list(COLUMNS):
        raise ValueError
    seen = set()
    for record in records[1:]:
        if len(record) != 5 or any(len(cell) > 256 for cell in record):
            raise ValueError
        metric, studio, oracle, delta, verdict = record
        if metric not in METRICS or metric in seen or verdict not in ("PASS", "FAIL", "UNREADABLE"):
            raise ValueError
        seen.add(metric)
        for value, signed, count in ((studio, True, False), (oracle, False, metric != "kw_dc"), (delta, True, False)):
            if value:
                _number(value, signed=signed, count=count)


def _grouping(page, label):
    button = page.get_by_role("button", name=label, exact=True)
    button.wait_for(state="visible")
    button.click()
    region = page.get_by_role("region", name="Panel groups", exact=True)
    region.wait_for(state="visible")
    source = region.get_by_role("combobox", name="Membership source", exact=True)
    if source.input_value() != "manual":
        source.select_option(label="Use proposed memberships")
    region.get_by_text(re.compile(r"^Graph revision [0-9]+\.?$")).wait_for(state="visible")
    proposals = region.get_by_role("textbox", name=re.compile(r"^Group [1-9][0-9]* name$"))
    count = sum(proposals.nth(i).is_visible() for i in range(proposals.count()))
    # Expose only a fixed observation of the expected form, never raw status text.
    return ([{"group_count": count}] if count else []), ("proposals_shown" if count else "no_proposals_shown")


def observe_reconciliation(page, *, base, project_name, version_id, oracle_values,
                           out_dir, grouping_label=None, save_report=False):
    """Return sanitized observations, not a reconciliation or persistence verdict.

    The output directory must already exist. Existing artifacts are never replaced.
    No screenshot is taken: this observer makes no viewport coverage assertion.
    """
    owned = None
    try:
        if type(save_report) is not bool:
            raise ValueError
        report_saved = False
        values = _inputs(base, project_name, version_id, oracle_values, grouping_label)
        target = Path(out_dir) / CSV_NAME
        if target.exists() or target.is_symlink() or not target.parent.is_dir():
            raise ValueError
        page.goto(base + "/app?surface=solar", wait_until="domcontentloaded")
        region = _select(page, project_name, version_id)
        revision_before = _read(region)
        for key, title in zip(METRICS, TITLES):
            region.get_by_role("textbox", name=title + " engineering report/BOM", exact=True).fill(values[key])
        before_drafts, before_rows = _drafts(region), _rows(region)
        if save_report:
            save = region.get_by_role("button", name="Save engineering totals", exact=True)
            if save.count() > 1:
                raise ValueError
            if save.count() == 1:
                save.click()
                region.get_by_text("Engineering totals saved.", exact=True).wait_for(state="visible")
                report_saved = True
        with page.expect_download() as event:
            region.get_by_role("button", name="Download CSV", exact=True).click()
        owned = target
        event.value.save_as(str(target))
        with target.open("rb") as stream:
            payload = stream.read(MAX_BYTES + 1)
        if len(payload) > MAX_BYTES:
            raise ValueError
        csv_text = payload.decode("utf-8", errors="strict")
        _validate_csv(csv_text)
    except Exception:
        if owned is not None:
            try:
                owned.unlink(missing_ok=True)
            except OSError:
                pass
        return {"status": "UNREADABLE", "reason": "RECONCILIATION_UI_UNREADABLE"}

    # The validated download and initial observations remain evidence even if a
    # later, independent UI stage cannot be observed.
    result = dict(status="OBSERVED", before_rows=before_rows, csv_text=csv_text,
                  before_drafts=before_drafts, report_saved=report_saved, reopened_rows=None,
                  reopened_drafts=None, reopen_observed=False,
                  saved_revision_before=revision_before, saved_revision_after=None,
                  recommendations=None, grouping_status="not_observed",
                  artifacts=[CSV_NAME])
    if grouping_label is not None:
        try:
            result["recommendations"], result["grouping_status"] = _grouping(page, grouping_label)
        except Exception:
            result.update(status="UNREADABLE", reason="RECONCILIATION_UI_UNREADABLE",
                          grouping_status="UNREADABLE", grouping_reason="GROUPING_UI_UNREADABLE")
    try:
        page.reload(wait_until="domcontentloaded")
        reopened = _select(page, project_name, version_id)
        result["drafts_before_reopen_read"] = _drafts(reopened)
        result["reopened_drafts"] = result["drafts_before_reopen_read"]
        result["saved_revision_after"] = _read(reopened)
        if report_saved:
            reopened.get_by_text("Saved engineering totals loaded.", exact=True).wait_for(state="visible")
            result["reopened_drafts"] = _drafts(reopened)
        result["reopened_rows"] = _rows(reopened)
        result["reopen_observed"] = True
    except Exception:
        result.update(status="UNREADABLE", reason="RECONCILIATION_UI_UNREADABLE",
                      reopen_reason="REOPEN_UI_UNREADABLE")
    return result
