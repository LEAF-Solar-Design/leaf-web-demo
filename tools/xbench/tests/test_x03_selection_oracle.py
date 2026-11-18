import pytest
from benches.x03_selection_oracle import selection_quality


@pytest.mark.parametrize("before,after,added,removed", [
    (["A","B"],["B","C"],["C"],["A"]), (["1B"],[],[],["1B"]), ([],["F","E"],["E","F"],[])])
def test_selection_revision_and_explanation(before, after, added, removed):
    saved = dict(drawing="roof", version="v1", rule_revision="new", handles=before)
    reverted = dict(saved, rule_revision="prior", handles=after)
    exports = [dict(saved), dict(reverted)]
    assert selection_quality(saved, reverted, exports, added, removed,
                             inventory=dict(drawing="roof",version="v1",live_handles=before+after)) == "PASS"
    for key in ("version", "drawing", "rule_revision"):
        assert selection_quality(saved, reverted, [saved, dict(reverted, **{key:"wrong"})], added, removed) == "FAIL"
    assert selection_quality(saved, reverted, exports, added+["unexplained"], removed) == "FAIL"
    assert selection_quality(saved, reverted, [dict(saved, handles=["wrong"]), reverted], added, removed) == "FAIL"


@pytest.mark.parametrize("bad", [None, ["FF"], "not-hex"])
def test_handle_case_and_malformed_evidence(bad):
    row = dict(drawing="roof",version="1",rule_revision="old",handles=["ab"])
    new = dict(row,rule_revision="new",handles=["cd"])
    exports = [dict(row,handles=["AB"]),dict(new,handles=["CD"])]
    assert selection_quality(row,new,exports,["CD"],["AB"],inventory=dict(drawing="roof",version="1",live_handles=["AB","CD"])) == "PASS"
    assert selection_quality(row,new,[dict(row,handles=[bad]),new],["cd"],["ab"]) == "FAIL"


@pytest.mark.parametrize("handle", ["AB","11","FF"])
def test_selections_require_live_handle_in_pinned_inventory(handle):
    saved = dict(drawing="d",version="v",rule_revision="one",handles=[handle])
    new = dict(saved,rule_revision="two")
    def check(inventory):
        return selection_quality(saved,new,[saved,new],[],[],inventory=inventory)
    assert check(None) == "INCOMPLETE"
    inventory = dict(drawing="d",version="v",live_handles=[handle])
    assert check(inventory) == "PASS"
    assert check(dict(inventory,version="old")) == "FAIL"
    assert check(dict(inventory,live_handles=[])) == "FAIL"
