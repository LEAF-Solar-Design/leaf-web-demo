"""X04 repeated invalid/valid selection recovery over one fixed saved intake."""
import argparse
from copy import deepcopy
import json
from pathlib import Path
from bench_x04_module_count import measure
from bench_x04_saved_replay import X04_ROOT, tool_loader


def benchmark(cycles, tool):
    intake = {"inserts": [{"handle": format(index+16, "X"), "layer": f"Set{index%3}",
                           "name": "Panel", "rows": 2, "columns": 3} for index in range(64)]}
    intake = json.loads(json.dumps(intake, allow_nan=False))
    snapshot = deepcopy(intake)
    listing = tool_loader.run_tool_dynamic(tool, intake, {}, aps_live=False)
    assert listing["ok"]
    ids = [row["set_id"] for row in listing["result"]["sets"]]
    baseline = tool_loader.run_tool_dynamic(tool, intake, {"set_ids": ids}, aps_live=False)
    assert baseline["ok"] and baseline["result"]["count"] == 384
    handles = {format(index+16, "X") for index in range(64)}
    def recover():
        for _ in range(cycles):
            failed = tool_loader.run_tool_dynamic(tool, intake, {"set_ids": [ids[0], "absent"]}, aps_live=False)
            assert not failed["ok"] and not failed.get("result") and not failed.get("overlay")
            valid = tool_loader.run_tool_dynamic(tool, intake, {"set_ids": ids}, aps_live=False)
            assert valid["ok"] and valid["result"] == baseline["result"]
            assert valid["overlay"] == baseline["overlay"]
            assert set(valid["overlay"]["highlight_handles"]) == handles
            assert len(valid["overlay"]["highlight_handles"]) == 64
            assert intake == snapshot
        return cycles
    completed, metrics = measure(recover)
    return {"cycles": completed, "parent_records": 64, "modules": 384, "highlights": 64, **metrics}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    tool = next(t for t in json.loads((X04_ROOT / "engine/registry.json").read_text())["tools"] if t["name"] == "count-modules")
    assert tool_loader.is_trusted_builtin_tool(tool)
    data = {"schema": "leaf.x04.rejection-recovery.v1", "synthetic": True, "timing_gate": False, "heap_gate": False,
            "memory_scope": "Recovery loop only; input/snapshot and trusted module warmup excluded.",
            "results": [benchmark(cycles, tool) for cycles in (10, 100, 1000)]}
    encoded = json.dumps(data, indent=2) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(encoded, encoding="utf-8")
    print(encoded, end="")


if __name__ == "__main__":
    main()
