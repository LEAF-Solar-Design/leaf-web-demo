"""Synthetic synchronous Playwright protocol: no browser or private fixtures."""

import re

import pytest

from benches.x05_table_observer import observe_reconciliation


BASE = "https://platform-staging.leafdesign.ai"
TITLES = ["Panels", "Optimizers", "Inverters", "Strings", "kW DC"]
VALUES = dict(panels=10, optimizers=5, inverters=1, strings=2, kw_dc="4.2")
EXPECTED = [
    dict(metric="panels", studio="12", oracle="10", delta="2", verdict="FAIL"),
    dict(metric="optimizers", studio="", oracle="5", delta="", verdict="UNREADABLE"),
    dict(metric="inverters", studio="1", oracle="1", delta="0", verdict="PASS"),
    dict(metric="strings", studio="2", oracle="2", delta="0", verdict="PASS"),
    dict(metric="kw_dc", studio="4.2", oracle="4.2", delta="0", verdict="PASS"),
]
CSV = (
    'metric,studio,oracle,delta,verdict\r\n'
    '"panels","12","10","2","FAIL"\r\n'
    '"optimizers","","5","","UNREADABLE"\r\n'
    '"inverters","1","1","0","PASS"\r\n'
    '"strings","2","2","0","PASS"\r\n'
    '"kw_dc","4.2","4.2","0","PASS"\r\n'
)
UNREADABLE = {"status": "UNREADABLE", "reason": "RECONCILIATION_UI_UNREADABLE"}


class Locator:
    def __init__(self, page, kind, index=None, items=None):
        self.page, self.kind, self.index, self.items = page, kind, index, items

    @property
    def first(self):
        return self.nth(0)

    def nth(self, index):
        return self.items[index]

    def count(self):
        return len(self.items) if self.items is not None else 1

    def wait_for(self, *, state):
        assert state == "visible"
        if self.kind == "missing" or self.page.timeout:
            raise TimeoutError("secret project/version/path")
        if self.kind == "graph_revision":
            self.page.events.append(("wait_graph_revision",))
            if self.page.missing_graph_revision or self.page.same_source_invalidated:
                raise TimeoutError("private graph status")
            self.page.graph_ready = True

    def is_visible(self):
        return self.kind != "hidden"

    def click(self):
        p = self.page
        p.events.append(("click", self.kind, p.reopened))
        if self.kind == "read":
            p.reads += 1
            if p.reopened:
                # Reading must happen after draft capture and must not refill.
                assert p.captured == [0, 1, 2, 3, 4]
                assert p.fills == 5
        if self.kind == "download":
            assert p.expecting
            p.download_clicked = True

    def select_option(self, **kwargs):
        self.page.events.append(("select", self.kind, kwargs))
        if self.kind == "source":
            self.page.same_source_invalidated = self.page.membership_source == "manual"
            self.page.membership_source = "manual"

    def fill(self, value):
        assert self.kind == "input" and not self.page.reopened
        self.page.drafts[self.index] = value
        self.page.fills += 1

    def input_value(self):
        if self.kind == "source":
            return self.page.membership_source
        assert self.kind == "input"
        if self.page.reopened and self.page.reads == 1:
            self.page.captured.append(self.index)
        return self.page.drafts[self.index]

    def inner_text(self):
        if self.items is not None:
            assert len(self.items) == 1
            return self.items[0].inner_text()
        p, i = self.page, self.index
        if self.kind == "revision":
            return "Saved design revision 7."
        if self.kind == "header":
            return "secret unknown label" if p.bad_table == "label" and i == 0 else TITLES[i]
        if self.kind == "small":
            return ": Supply a valid engineering report/BOM value."
        if self.kind == "studio":
            return "secret DOM" if p.bad_table == "number" and i == 0 else ["12", "Unavailable", "1", "2", "4.2"][i]
        if self.kind == "delta":
            return "Unavailable" if p.reopened or i == 1 or p.drafts[i] == "" else ["2", "", "0", "0", "0"][i]
        if self.kind == "verdict":
            if p.bad_table == "verdict" and i == 0:
                return "PASS secret"
            return ("Unavailable: Supply a valid engineering report/BOM value."
                    if p.reopened or i == 1 or p.drafts[i] == "" else ["FAIL", "", "PASS", "PASS", "PASS"][i])
        raise AssertionError(self.kind)

    def locator(self, selector):
        p = self.page
        if self.kind == "table" and selector == "tbody tr":
            rows = [Locator(p, "row", i) for i in range(5)]
            if p.bad_table == "missing":
                rows.pop()
            return Locator(p, "list", items=rows)
        if self.kind == "verdict" and selector == "small":
            return Locator(p, "list", items=[Locator(p, "small")] if p.reopened or self.index == 1 or p.drafts[self.index] == "" else [])
        raise AssertionError((self.kind, selector))

    def get_by_text(self, name):
        if self.kind == "groups":
            assert isinstance(name, re.Pattern) and name.fullmatch("Graph revision 7")
            assert self.page.membership_source == "manual"
            return Locator(self.page, "graph_revision")
        assert self.kind == "region" and isinstance(name, re.Pattern)
        assert name.search("Saved design revision 7.")
        return Locator(self.page, "revision")

    def get_by_role(self, role, *, name=None, exact=False):
        p, i = self.page, self.index
        if self.kind == "region":
            if role == "button":
                assert exact
                return Locator(p, {"Read saved design": "read", "Download CSV": "download"}[name])
            if role == "textbox":
                assert exact
                return Locator(p, "input", [title + " engineering report/BOM" for title in TITLES].index(name))
            assert role == "table" and exact and name == "Saved Studio vs engineering report/BOM totals"
            return Locator(p, "table")
        if self.kind == "row":
            if role == "rowheader":
                return Locator(p, "header", i)
            assert role == "cell"
            cells = [Locator(p, kind, i) for kind in ("studio", "oracle", "delta", "verdict")]
            if p.bad_table == "shape" and i == 0:
                cells.pop()
            return Locator(p, "list", items=cells)
        if self.kind == "oracle":
            assert role == "textbox"
            return Locator(p, "input", i)
        if self.kind == "groups":
            if role == "combobox":
                assert name == "Membership source" and exact
                if p.missing_source:
                    raise TimeoutError("private form")
                return Locator(p, "source")
            assert role == "textbox" and name.fullmatch("Group 1 name")
            p.events.append(("count_proposals", p.graph_ready))
            if not p.graph_ready:
                return Locator(p, "list", items=[])
            return Locator(p, "list", items=[Locator(p, "proposal") for _ in range(p.groups)] + [Locator(p, "hidden")])
        raise AssertionError((self.kind, role, name))


class Download:
    def __init__(self, page):
        self.page = page

    def save_as(self, path):
        from pathlib import Path

        assert self.page.download_clicked
        self.page.saved.append(path)
        Path(path).write_bytes(self.page.payload)
        if self.page.fail_save:
            raise OSError("private download path")


class DownloadEvent:
    def __init__(self, page):
        self.page = page
        self.value = Download(page)

    def __enter__(self):
        self.page.expecting = True
        return self

    def __exit__(self, *args):
        self.page.expecting = False


class Page:
    def __init__(self):
        self.events, self.saved, self.captured = [], [], []
        self.drafts = [""] * 5
        self.payload = CSV.encode("utf-8")
        self.reopened = self.expecting = self.download_clicked = False
        self.timeout = self.fail_save = self.missing_source = False
        self.missing_graph_revision = self.graph_ready = self.fail_reopen = False
        self.bad_reopened_table = None
        self.bad_table = None
        self.membership_source, self.same_source_invalidated = "manual", False
        self.groups, self.fills, self.reads = 2, 0, 0

    def goto(self, url, *, wait_until):
        assert url == BASE + "/app?surface=solar" and wait_until == "domcontentloaded"
        self.events.append(("goto", url))

    def reload(self, *, wait_until):
        assert wait_until == "domcontentloaded"
        self.events.append(("reload",))
        if self.fail_reopen:
            raise TimeoutError("private reopen path/project/version")
        self.reopened = True
        self.drafts = [""] * 5
        self.bad_table = self.bad_reopened_table

    def get_by_role(self, role, *, name, exact=False):
        if role == "button" and isinstance(name, re.Pattern):
            assert name.search("Projects: change project. Current")
            return Locator(self, "projects")
        if role == "menuitem":
            assert name.search("Pinned Fixture [owned].*")
            assert not name.search("Fixture other")
            return Locator(self, "list", items=[Locator(self, "project"), Locator(self, "duplicate")])
        if role == "combobox":
            assert name == "Canonical drawing version" and exact
            return Locator(self, "version")
        if role == "region":
            assert exact
            return Locator(self, {"Engineering reconciliation": "region", "Panel groups": "groups"}[name])
        assert role == "button" and name == "Catalog grouping" and exact
        return Locator(self, "grouping")

    def expect_download(self):
        self.events.append(("expect_download",))
        return DownloadEvent(self)

    def close(self):
        raise AssertionError("Caller owns lifecycle")


def observe(page, tmp_path, **overrides):
    kwargs = dict(base=BASE, project_name="Fixture [owned].*", version_id="version-1",
                  oracle_values=VALUES, out_dir=tmp_path)
    kwargs.update(overrides)
    return observe_reconciliation(page, **kwargs)


def test_actual_download_and_normal_reopen_without_retyping(tmp_path):
    page = Page()
    result = observe(page, tmp_path)
    assert result["status"] == "OBSERVED"
    assert result["before_rows"] == EXPECTED
    assert result["csv_text"] == CSV
    assert (tmp_path / "x05-reconciliation.csv").read_bytes() == CSV.encode()
    assert len(page.saved) == 1 and page.download_clicked
    assert result["artifacts"] == ["x05-reconciliation.csv"]
    assert result["before_drafts"] == dict(panels="10", optimizers="5", inverters="1", strings="2", kw_dc="4.2")
    assert result["reopened_drafts"] == dict.fromkeys(VALUES, "")
    assert result["reopened_rows"] == [
        dict(metric="panels", studio="12", oracle="", delta="", verdict="UNREADABLE"),
        dict(metric="optimizers", studio="", oracle="", delta="", verdict="UNREADABLE"),
        dict(metric="inverters", studio="1", oracle="", delta="", verdict="UNREADABLE"),
        dict(metric="strings", studio="2", oracle="", delta="", verdict="UNREADABLE"),
        dict(metric="kw_dc", studio="4.2", oracle="", delta="", verdict="UNREADABLE"),
    ]
    assert result["reopen_observed"] is True
    assert result["saved_revision_before"] == result["saved_revision_after"] == 7
    assert result["recommendations"] is None and result["grouping_status"] == "not_observed"
    assert page.fills == 5 and page.reads == 2
    assert [(e[1], e[2]) for e in page.events if e[0] == "click" and e[1] == "project"] == [("project", False), ("project", True)]
    assert [e for e in page.events if e[:2] == ("select", "version")] == [
        ("select", "version", {"value": "version-1"}), ("select", "version", {"value": "version-1"})]


@pytest.mark.parametrize("groups,expected,status", [(2, [{"group_count": 2}], "proposals_shown"), (0, [], "no_proposals_shown")])
@pytest.mark.parametrize("source", ["manual", "discover"])
def test_only_visible_group_proposals(tmp_path, groups, expected, status, source):
    page = Page()
    page.groups = groups
    page.membership_source = source
    result = observe(page, tmp_path, grouping_label="Catalog grouping")
    assert result["status"] == "OBSERVED"
    assert result["recommendations"] == expected and result["grouping_status"] == status
    assert (("select", "source", {"label": "Use proposed memberships"}) in page.events) == (source != "manual")
    assert not page.same_source_invalidated
    assert page.events.index(("wait_graph_revision",)) < page.events.index(("count_proposals", True))
    assert not any(e[0] == "click" and e[1] == "Run" for e in page.events)


@pytest.mark.parametrize("missing", ["missing_source", "missing_graph_revision"])
def test_missing_group_form_retains_evidence_and_still_reopens(tmp_path, missing):
    page = Page()
    setattr(page, missing, True)
    unrelated = tmp_path / "unrelated.txt"
    unrelated.write_text("keep")
    result = observe(page, tmp_path, grouping_label="Catalog grouping")
    assert result["status"] == "UNREADABLE"
    assert result["reason"] == "RECONCILIATION_UI_UNREADABLE"
    assert result["grouping_status"] == "UNREADABLE"
    assert result["grouping_reason"] == "GROUPING_UI_UNREADABLE"
    assert result["recommendations"] is None
    assert result["before_rows"] == EXPECTED and result["csv_text"] == CSV
    assert result["artifacts"] == ["x05-reconciliation.csv"]
    assert (tmp_path / "x05-reconciliation.csv").read_bytes() == CSV.encode()
    assert unrelated.read_text() == "keep"
    assert result["reopen_observed"] is True
    assert result["reopened_drafts"] == dict.fromkeys(VALUES, "")
    assert page.fills == 5 and page.reads == 2
    assert not any(e[0] == "count_proposals" for e in page.events)
    assert "private" not in str(result) and "secret" not in str(result)


@pytest.mark.parametrize("failure", ["navigation", "table"])
def test_reopen_failure_retains_download_table_and_grouping(tmp_path, failure):
    page = Page()
    page.fail_reopen = failure == "navigation"
    page.bad_reopened_table = "number" if failure == "table" else None
    result = observe(page, tmp_path, grouping_label="Catalog grouping")
    assert result["status"] == "UNREADABLE"
    assert result["reason"] == "RECONCILIATION_UI_UNREADABLE"
    assert result["reopen_reason"] == "REOPEN_UI_UNREADABLE"
    assert result["reopen_observed"] is False and result["reopened_rows"] is None
    assert result["before_rows"] == EXPECTED and result["csv_text"] == CSV
    assert result["before_drafts"] == dict(panels="10", optimizers="5", inverters="1", strings="2", kw_dc="4.2")
    assert result["saved_revision_before"] == 7
    assert result["recommendations"] == [{"group_count": 2}]
    assert result["grouping_status"] == "proposals_shown"
    assert result["artifacts"] == ["x05-reconciliation.csv"]
    assert (tmp_path / "x05-reconciliation.csv").read_bytes() == CSV.encode()
    assert result["reopened_drafts"] == (None if failure == "navigation" else dict.fromkeys(VALUES, ""))
    assert page.fills == 5
    assert "private" not in str(result) and "secret" not in str(result)


def test_both_later_stages_fail_independently(tmp_path):
    page = Page()
    page.missing_source = page.fail_reopen = True
    result = observe(page, tmp_path, grouping_label="Catalog grouping")
    assert result["status"] == "UNREADABLE"
    assert result["grouping_reason"] == "GROUPING_UI_UNREADABLE"
    assert result["reopen_reason"] == "REOPEN_UI_UNREADABLE"
    assert ("reload",) in page.events
    assert result["before_rows"] == EXPECTED and result["csv_text"] == CSV
    assert (tmp_path / "x05-reconciliation.csv").read_bytes() == CSV.encode()


@pytest.mark.parametrize("payload", [
    b"private labels", CSV.replace("panels", "private-label").encode(),
    CSV.replace('"12"', '"private DOM"').encode(),
    CSV.replace("metric,studio", "private,studio").encode(),
    CSV.replace('"PASS"', '"PASS secret"').encode(),
    CSV.encode() + b'"unterminated', CSV.encode() + b"\r\n",
    CSV.encode() + CSV.encode(), b"x" * 16385, b"\xff", b'',
])
def test_malformed_actual_csv_is_sanitized_and_removed(tmp_path, payload):
    page = Page()
    page.payload = payload
    assert observe(page, tmp_path) == UNREADABLE
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("bad_table", ["missing", "shape", "label", "number", "verdict"])
def test_bad_table_never_becomes_pass(tmp_path, bad_table):
    page = Page()
    page.bad_table = bad_table
    assert observe(page, tmp_path) == UNREADABLE
    assert page.saved == []


@pytest.mark.parametrize("overrides", [
    {"base": BASE + "/"}, {"base": "https://evil.example"},
    {"project_name": ""}, {"version_id": " "}, {"project_name": "x" * 257},
    {"grouping_label": "\nprivate"}, {"oracle_values": {}},
    {"oracle_values": dict(VALUES, extra=1)}, {"oracle_values": dict(VALUES, panels=True)},
    {"oracle_values": dict(VALUES, panels=-1)}, {"oracle_values": dict(VALUES, panels=2**53)},
    {"oracle_values": dict(VALUES, panels="01")}, {"oracle_values": dict(VALUES, panels="1.0")},
    {"oracle_values": dict(VALUES, kw_dc="NaN")}, {"oracle_values": dict(VALUES, kw_dc="4.20")},
    {"oracle_values": dict(VALUES, kw_dc=4.2)},
])
def test_invalid_input_never_navigates(tmp_path, overrides):
    page = Page()
    assert observe(page, tmp_path, **overrides) == UNREADABLE
    assert page.events == []


def test_timeout_and_partial_download_failures_do_not_leak(tmp_path):
    page = Page()
    page.timeout = True
    assert observe(page, tmp_path) == UNREADABLE
    page = Page()
    page.fail_save = True
    assert observe(page, tmp_path) == UNREADABLE
    assert not list(tmp_path.iterdir())


def test_existing_artifact_is_not_overwritten(tmp_path):
    artifact = tmp_path / "x05-reconciliation.csv"
    artifact.write_bytes(b"caller evidence")
    page = Page()
    assert observe(page, tmp_path) == UNREADABLE
    assert artifact.read_bytes() == b"caller evidence" and page.events == []


def test_unknown_dc_is_filled_blank(tmp_path):
    page = Page()
    result = observe(page, tmp_path, oracle_values=dict(VALUES, kw_dc=None))
    assert result["status"] == "OBSERVED"
    assert result["before_drafts"]["kw_dc"] == ""
    assert result["before_rows"][4] == dict(metric="kw_dc", studio="4.2", oracle="", delta="", verdict="UNREADABLE")
    assert page.fills == 5


def test_csv_mismatch_is_preserved_for_caller_to_grade(tmp_path):
    page = Page()
    page.payload = CSV.replace('"12"', '"11"').encode()
    result = observe(page, tmp_path)
    assert result["status"] == "OBSERVED"
    assert result["before_rows"] == EXPECTED
    assert result["csv_text"] == page.payload.decode()
