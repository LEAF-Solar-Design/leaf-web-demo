"""X04 fixed-record dependency payload observations, without timing gates."""
import argparse
import importlib.util
import json
from pathlib import Path
from bench_x04_module_count import measure


def load_report():
    path = Path(__file__).resolve().parents[1] / "server" / "drawing_dependency_report.py"
    spec = importlib.util.spec_from_file_location("x04_payload_report", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.from_pairs


def benchmark(length, report, records=128):
    names = [f"{index:04d}-" + "x" * (length - 9) + ".shx" for index in range(records)]
    pairs = [(code, value) for name in names for code, value in [(0, "STYLE"), (3, name)]]
    result, metrics = measure(lambda: report(pairs))
    encoded = json.dumps(result, allow_nan=False)
    restored = json.loads(encoded)
    assert restored == result
    assert {row["name"] for row in restored["dependencies"]} == set(names)
    assert len(restored["dependencies"]) == records
    assert all(len(row["name"]) == length and row["disposition"] == "font remapped"
               for row in restored["dependencies"])
    return {"records": records, "name_characters": length, "strict_json_bytes": len(encoded.encode("utf-8")), **metrics}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    report = load_report()
    data = {"schema": "leaf.x04.dependency-payload.v1", "synthetic": True, "timing_gate": False,
            "memory_scope": "Report operation only; input construction and JSON encoding excluded.",
            "results": [benchmark(length, report) for length in (16, 256, 4096, 16384)]}
    encoded = json.dumps(data, indent=2) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(encoded, encoding="utf-8")
    print(encoded, end="")


if __name__ == "__main__":
    main()
