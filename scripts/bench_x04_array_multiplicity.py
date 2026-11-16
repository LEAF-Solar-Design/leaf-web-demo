"""X04 array size observations with a fixed synthetic parent population."""
import argparse
import json
from pathlib import Path

from bench_x04_module_count import load_tool, measure


def benchmark(rows, columns, tool, parents=128):
    handles = [format(index + 16, "X") for index in range(parents)]
    intake = {"inserts": [{"handle": handle, "layer": "Modules", "name": "Panel",
                           "rows": rows, "columns": columns} for handle in handles]}
    (listing, _), listing_metrics = measure(lambda: tool.run(intake, {}))
    (result, overlay), count_metrics = measure(lambda: tool.run(intake,
        {"set_ids": [row["set_id"] for row in listing["sets"]]}))
    expected = parents * rows * columns
    assert result["count"] == expected == listing["sets"][0]["count"]
    assert overlay["highlight_handles"] == handles
    assert len(overlay["highlight_handles"]) == parents
    return {"parent_inserts": parents, "rows": rows, "columns": columns,
            "expected_modules": expected, "count": result["count"],
            "highlighted_parents": len(overlay["highlight_handles"]),
            "listing": listing_metrics, "counting": count_metrics}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    tool = load_tool()
    report = {"schema": "leaf.x04.array-multiplicity.v1", "synthetic": True, "timing_gate": False,
              "memory_scope": "Operation allocations only; input built before tracing.",
              "results": [benchmark(r, c, tool) for r, c in ((1, 1), (2, 3), (256, 256), (65535, 65535))]}
    encoded = json.dumps(report, indent=2) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(encoded, encoding="utf-8")
    print(encoded, end="")


if __name__ == "__main__":
    main()
