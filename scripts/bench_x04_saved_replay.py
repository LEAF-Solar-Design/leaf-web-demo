"""X04 strict JSON replay and trusted count observations with synthetic inputs."""
import argparse
import json
from pathlib import Path
import platform  # Cache the stdlib module before placing server on the import path.
import sys
from bench_x04_module_count import measure

X04_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(X04_ROOT / "server"))
import tool_loader
from drawing_dependency_report import from_pairs


def benchmark(size, tool):
    intake = {"inserts": [{"handle": format(i + 16, "X"), "layer": f"Set{i % 3}", "name": "Panel",
                           "rows": 2 if i % 10 == 0 else 1, "columns": 3 if i % 10 == 0 else 1}
                          for i in range(size)],
              "dependency_report": from_pairs([(0, "STYLE"), (3, "synthetic.shx"),
                                                (0, "IMAGEDEF"), (1, "synthetic.png")])}
    encoded, encoding = measure(lambda: json.dumps(intake, allow_nan=False))
    restored, decoding = measure(lambda: json.loads(encoded))
    listed, listing = measure(lambda: tool_loader.run_tool_dynamic(tool, restored, {}, aps_live=False))
    assert listed["ok"]
    params = {"set_ids": [row["set_id"] for row in listed["result"]["sets"]]}
    counted, selection = measure(lambda: tool_loader.run_tool_dynamic(tool, restored, params, aps_live=False))
    assert counted["ok"] and counted["result"]["count"] == size + 5 * ((size + 9) // 10)
    assert set(counted["overlay"]["highlight_handles"]) == {format(i + 16, "X") for i in range(size)}
    assert len(counted["overlay"]["highlight_handles"]) == size
    assert restored == intake and restored["dependency_report"] == intake["dependency_report"]
    return {"parent_records": size, "modules": counted["result"]["count"], "highlights": size,
            "json_bytes": len(encoded.encode("utf-8")), "encoding": encoding, "decoding": decoding,
            "listing": listing, "selection": selection}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    tool = next(t for t in json.loads((X04_ROOT / "engine/registry.json").read_text())["tools"] if t["name"] == "count-modules")
    assert tool_loader.is_trusted_builtin_tool(tool)
    assert tool_loader.run_tool_dynamic(tool, {}, {}, aps_live=False)["ok"]
    report = {"schema": "leaf.x04.saved-replay.v1", "synthetic": True, "timing_gate": False,
              "memory_scope": "Each operation measured separately; initial input construction and module warmup excluded.",
              "results": [benchmark(size, tool) for size in (100, 1000, 10000)]}
    encoded = json.dumps(report, indent=2) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(encoded, encoding="utf-8")
    print(encoded, end="")


if __name__ == "__main__":
    main()
