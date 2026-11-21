"""X04 duplicate-record pressure at a fixed set of long literal references."""
import argparse
import importlib.util
import json
from pathlib import Path
from bench_x04_module_count import measure


def load_report():
    path = Path(__file__).resolve().parents[1] / "server/drawing_dependency_report.py"
    spec = importlib.util.spec_from_file_location("x04_duplicate_report", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.from_pairs


def benchmark(records, report):
    names = [f"{index:03d}-" + "x" * 1016 + ".shx" for index in range(16)]
    pairs = [(code, value) for index in range(records)
             for code,value in [(0, "STYLE"), (3, names[index % len(names)])]]
    snapshot = list(pairs)  # Entries and their contents are immutable tuples/strings.
    result, metrics = measure(lambda: report(pairs))
    assert [row["name"] for row in result["dependencies"]] == names
    assert len(result["dependencies"]) == 16
    assert all(row["disposition"] == "font remapped" for row in result["dependencies"])
    assert pairs == snapshot
    return {"records": records, "unique_references": 16, "name_characters": 1024, **metrics}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    report = load_report()
    data = {"schema": "leaf.x04.dependency-duplicate-pressure.v1", "synthetic": True, "timing_gate": False,
            "memory_scope": "Report allocations only; synthetic input/snapshot constructed before tracing.",
            "results": [benchmark(size, report) for size in (128, 1024, 8192, 65536)]}
    encoded = json.dumps(data, indent=2) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(encoded, encoding="utf-8")
    print(encoded, end="")


if __name__ == "__main__":
    main()
