"""Independent logical-entity ledger versus every subset of seeded sets.

The empty subset checks listing mode. Every nonempty subset checks a physical
union derived from generator events, never from the product's count or handles.
"""
from copy import deepcopy
import itertools
import json
from pathlib import Path
import random

import pytest

import tool_loader


TOOL = next(tool for tool in json.loads(
    (Path(__file__).resolve().parents[2] / "engine" / "registry.json").read_text()
)["tools"] if tool["name"] == "count-modules")


def synthetic_drawing(seed):
    rng = random.Random(seed)
    intake = {"inserts": [], "polylines": []}
    ledger = {group: set() for group in range(4)}

    def append(group, handle=None, paper=False):
        row = {"layer": f"Set {group}"}
        if handle is not None:
            row["handle"] = handle
        if paper:
            row.update({"space": "paper"} if group % 2 == 0 else {"layout": "Sheet 1"})
        if group < 2:
            row["name"] = f"Panel {group}"
            intake["inserts"].append(row)
        else:
            row.update(closed=True, pts=[[0, 0], [2, 0], [2, 1], [0, 1]])
            intake["polylines"].append(row)

    for physical in range(10):
        for group in rng.sample(range(4), rng.randint(1, 3)):
            ledger[group].add(("known", physical))
            handle = format(physical + 16, "X")
            append(group, handle)
            if rng.random() < 0.5:
                append(group, "0x" + handle.lower())
    for group in range(4):
        for index in range(2):
            ledger[group].add(("anonymous", group, index))
            append(group)
        append(group, f"PAPER{group}", paper=True)
    for rows in intake.values():
        rng.shuffle(rows)
    return intake, ledger


@pytest.mark.parametrize("seed", [7, 23, 211])
def test_every_competing_subset_matches_independent_physical_oracle(seed):
    intake, ledger = synthetic_drawing(seed)
    original = deepcopy(intake)
    listing = tool_loader.run_tool_dynamic(TOOL, intake, {}, aps_live=False)
    assert listing["ok"]
    rows = {int(row["layer"].split()[1]): row for row in listing["result"]["sets"]}
    assert set(rows) == set(ledger)
    assert all(rows[group]["count"] == len(ledger[group]) for group in ledger)
    for length in range(5):
        for subset in itertools.combinations(range(4), length):
            selected_ids = [rows[group]["set_id"] for group in subset]
            envelope = tool_loader.run_tool_dynamic(TOOL, intake, {"set_ids": selected_ids}, aps_live=False)
            assert envelope["ok"], (seed, subset)
            expected = set().union(*(ledger[group] for group in subset))
            if not subset:
                assert envelope["result"]["count"] is None
                assert envelope["result"]["selection_required"]
                assert not envelope.get("overlay")
                continue
            assert envelope["result"]["count"] == len(expected), (seed, subset)
            handles = {format(identity[1] + 16, "X") for identity in expected if identity[0] == "known"}
            actual = envelope["overlay"]["highlight_handles"]
            assert set(actual) == handles, (seed, subset)
            assert len(actual) == len(handles), (seed, subset)
    assert intake == original
