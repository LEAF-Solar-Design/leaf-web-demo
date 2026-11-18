import pytest
from benches.x03_selection_oracle import selection_quality


@pytest.mark.parametrize("before,after,added,removed", [
    (["A","B"],["B","C"],["C"],["A"]), (["1B"],[],[],["1B"]), ([],["F","E"],["E","F"],[])])
def test_selection_revision_and_explanation(before, after, added, removed):
    saved = dict(scope="selected-only",drawing="roof", version="v1", rule_revision="new", handles=before)
    reverted = dict(saved, rule_revision="prior", handles=after)
    exports = [dict(saved), dict(reverted)]
    assert selection_quality(saved, reverted, exports, added, removed,
                             requested_scope="selected-only",inventory=dict(scope="selected-only",drawing="roof",version="v1",live_handles=before+after)) == "PASS"
    for key in ("version", "drawing", "rule_revision"):
        assert selection_quality(saved, reverted, [saved, dict(reverted, **{key:"wrong"})], added, removed) == "FAIL"
    assert selection_quality(saved, reverted, exports, added+["unexplained"], removed) == "FAIL"
    assert selection_quality(saved, reverted, [dict(saved, handles=["wrong"]), reverted], added, removed) == "FAIL"


@pytest.mark.parametrize("bad", [None, ["FF"], "not-hex"])
def test_handle_case_and_malformed_evidence(bad):
    row = dict(scope="selected-only",drawing="roof",version="1",rule_revision="old",handles=["ab"])
    new = dict(row,rule_revision="new",handles=["cd"])
    exports = [dict(row,handles=["AB"]),dict(new,handles=["CD"])]
    assert selection_quality(row,new,exports,["CD"],["AB"],requested_scope="selected-only",inventory=dict(scope="selected-only",drawing="roof",version="1",live_handles=["AB","CD"])) == "PASS"
    assert selection_quality(row,new,[dict(row,handles=[bad]),new],["cd"],["ab"]) == "FAIL"


@pytest.mark.parametrize("handle", ["AB","11","FF"])
def test_selections_require_live_handle_in_pinned_inventory(handle):
    saved = dict(scope="selected-only",drawing="d",version="v",rule_revision="one",handles=[handle])
    new = dict(saved,rule_revision="two")
    def check(inventory):
        return selection_quality(saved,new,[saved,new],[],[],requested_scope="selected-only",inventory=inventory)
    assert check(None) == "INCOMPLETE"
    inventory = dict(scope="selected-only",drawing="d",version="v",live_handles=[handle])
    assert check(inventory) == "PASS"
    assert check(dict(inventory,version="old")) == "FAIL"
    assert check(dict(inventory,live_handles=[])) == "FAIL"


@pytest.mark.parametrize("scope,handles", [("selected-only",["A"]),("visible-only",["A","B"]),("full-drawing",["A","B","C"])])
def test_export_scope(scope,handles):
    row = dict(drawing="d",version="1",rule_revision="r",handles=["A"])
    inv = dict(drawing="d",version="1",live_handles=["A","B","C"],visible_handles=["A","B"])
    def check(export):
        return selection_quality(row,row,[export,export],[],[],inventory=inv,requested_scope=scope)
    assert check(dict(row,scope=scope,handles=handles)) == "PASS"
    assert check(dict(row,scope=scope,handles=[])) == "FAIL"
    assert check(row) == "INCOMPLETE"

@pytest.mark.parametrize("scope", ["selected-only", "visible-only", "full-drawing"])
def test_conflicting_handle_states(scope):
    row = dict(drawing="d", version="v", rule_revision="r", handles=["AB"], scope=scope)
    for live in (["AB", "00ab"], ["00ab", "AB"]):
        inv = dict(drawing="d", version="v", live_handles=live, visible_handles=["ab"])
        for deleted, verdict in ((["CD"], "PASS"), (["CD", "0Ab"], "FAIL"), (["0Ab", "CD"], "FAIL")):
            assert selection_quality(row, row, [row, row], [], [], requested_scope=scope,
                                     inventory=dict(inv, deleted_handles=deleted)) == verdict
