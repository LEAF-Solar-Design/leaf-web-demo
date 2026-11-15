"""X04 synthetic metadata benchmark: exact dispositions gate; timings do not."""
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import time
import tracemalloc


def load_reader():
    source = Path(__file__).resolve().parents[1] / "server" / "drawing_dependency_report.py"
    spec = importlib.util.spec_from_file_location("x04_dependency_benchmark", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.from_pairs


def synthetic_records(size):
    pairs, expected = [], set()
    for i in range(size):
        name = f"reference-{i % 257}"
        variant = i % 6
        if variant == 0:
            record, kind, disposition = [(0, "STYLE"), (3, name)], "font", "font remapped"
        elif variant == 1:
            record, kind, disposition = [(0, "STYLE"), (70, "1"), (3, name)], "shape_font", "font remapped"
        elif variant == 2:
            record, kind, disposition = [(0, "BLOCK"), (70, "4"), (1, name)], "xref", "detached"
        elif variant == 3:
            record, kind, disposition = [(0, "IMAGEDEF"), (1, name)], "image", "image omitted"
        elif variant == 4:
            record, kind, disposition = [(0, "PDFDEFINITION"), (1, name)], "underlay", "underlay omitted"
        else:
            record, kind, disposition = [(0, "ACAD_PROXY_ENTITY"), (5, name)], "proxy", "proxy omitted"
        pairs.extend(record)
        expected.add((kind, name, disposition))
    return pairs, expected


def benchmark(size, reader):
    pairs, expected = synthetic_records(size)
    tracemalloc.start()
    started = time.perf_counter()
    try:
        report = reader(pairs)
        elapsed = time.perf_counter() - started
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    observed = {(row["kind"], row["name"], row["disposition"])
                for row in report["dependencies"]}
    assert observed == expected
    assert len(report["dependencies"]) == len(expected)
    return {"input_records": size, "unique_dependencies": len(expected),
            "observed_dependencies": len(report["dependencies"]),
            "seconds": round(elapsed, 6), "peak_bytes": peak}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", nargs="+", type=int, default=[1000, 10000, 100000])
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    if any(size < 1 for size in args.sizes):
        parser.error("sizes must be positive integers")
    reader = load_reader()
    report = {"schema": "leaf.x04.dependency-benchmark.v1", "synthetic": True,
              "memory_scope": "Operation allocations only; synthetic records built before tracing.",
              "timing_gate": False, "rows": [benchmark(size, reader) for size in args.sizes]}
    encoded = json.dumps(report, indent=2) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(encoded, encoding="utf-8")
    print(encoded, end="")


if __name__ == "__main__":
    main()
