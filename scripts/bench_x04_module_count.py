"""Offline synthetic module-count timings. Exact counts gate; timings do not."""
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import time
import tracemalloc


def load_tool():
    source = Path(__file__).resolve().parents[1] / "server" / "builtins" / "count_modules.py"
    spec = importlib.util.spec_from_file_location("x04_count_benchmark", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def measure(action):
    tracemalloc.start()
    started = time.perf_counter()
    try:
        value = action()
        seconds = time.perf_counter() - started
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return value, {"seconds": round(seconds, 6), "peak_bytes": peak}


def benchmark(size, tool):
    intake = {"inserts": [{"layer": f"Layer {i % 4}", "name": "Panel",
                            "handle": f"H{i:X}"} for i in range(size)]}
    (listing, _), listing_metrics = measure(lambda: tool.run(intake, {}))
    ids = [row["set_id"] for row in listing["sets"]]
    (selected, overlay), selected_metrics = measure(lambda: tool.run(intake, {"set_ids": ids}))
    assert sum(row["count"] for row in listing["sets"]) == size
    assert selected["count"] == size
    assert len(set(overlay["highlight_handles"])) == size
    return {"entities": size, "sets": len(ids), "selected_count": selected["count"],
            "listing": listing_metrics, "selected_counting": selected_metrics}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", nargs="+", type=int, default=[1000, 10000, 100000])
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    if any(size < 1 for size in args.sizes):
        parser.error("sizes must be positive integers")
    tool = load_tool()
    report = {"schema": "leaf.x04.module-benchmark.v1", "synthetic": True,
              "memory_scope": "Operation allocations only; synthetic input built before tracing.",
              "timing_gate": False, "rows": [benchmark(size, tool) for size in args.sizes]}
    encoded = json.dumps(report, indent=2) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(encoded, encoding="utf-8")
    print(encoded, end="")


if __name__ == "__main__":
    main()
