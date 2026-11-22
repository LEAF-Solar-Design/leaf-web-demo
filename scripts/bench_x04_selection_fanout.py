"""X04 fanout observations with fixed records and physical-entity population."""
import argparse
import json
from pathlib import Path

from bench_x04_module_count import load_tool, measure


def benchmark(set_count, tool, record_count=2048, physical_count=256):
    intake, ledger = {"inserts": []}, {group: set() for group in range(set_count)}
    for index in range(record_count):
        physical = index % physical_count
        group = (index + index // physical_count) % set_count
        ledger[group].add(physical)
        intake["inserts"].append({"layer": f"Set {group}", "name": "Panel", "handle": format(physical + 16, "X")})
    (listing, _), listed_metrics = measure(lambda: tool.run(intake, {}))
    selected_groups = set(range(set_count // 2))
    ids = [row["set_id"] for row in listing["sets"] if int(row["layer"].split()[1]) in selected_groups]
    (counted, overlay), count_metrics = measure(lambda: tool.run(intake, {"set_ids": ids}))
    expected = set().union(*(ledger[group] for group in selected_groups))
    assert len(listing["sets"]) == set_count and len(ids) == len(selected_groups)
    assert counted["count"] == len(expected)
    assert set(overlay["highlight_handles"]) == {format(value + 16, "X") for value in expected}
    assert len(overlay["highlight_handles"]) == len(expected)
    return {"input_records": record_count, "physical_entities": physical_count,
            "candidate_sets": set_count, "selected_ids": len(ids), "oracle_union_count": len(expected),
            "selected_count": counted["count"], "listing": listed_metrics, "counting": count_metrics}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    tool = load_tool()
    report = {"schema": "leaf.x04.selection-fanout.v1", "synthetic": True, "timing_gate": False,
              "memory_scope": "Operation allocations only; synthetic input built before tracing.",
              "rows": [benchmark(size, tool) for size in (4, 16, 64, 256)]}
    encoded = json.dumps(report, indent=2) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(encoded, encoding="utf-8")
    print(encoded, end="")


if __name__ == "__main__":
    main()
