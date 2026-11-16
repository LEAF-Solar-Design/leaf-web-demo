"""X04 dependency extraction with fixed references and growing unrelated records."""
import argparse
import importlib.util
import json
from pathlib import Path
from bench_x04_module_count import measure


def load_report():
    path = Path(__file__).resolve().parents[1] / "server/drawing_dependency_report.py"
    spec = importlib.util.spec_from_file_location("x04_sparse_report", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.from_pairs


def drawing(unrelated):
    pairs = [(0,"SECTION"),(2,"TABLES"),(0,"STYLE"),(3,"synthetic.shx"),(0,"ENDSEC"),
             (0,"SECTION"),(2,"OBJECTS"),(0,"IMAGEDEF"),(1,"synthetic.png"),(0,"ENDSEC"),
             (0,"SECTION"),(2,"ENTITIES")]
    for index in range(unrelated):
        pairs.extend([(0,"LINE"),(5,format(index+16,"X")),(3,"not-a-font.shx"),(1,"not-an-image.png")])
    return pairs + [(0,"ENDSEC"),(0,"EOF")]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    report = load_report()
    expected = report(drawing(0))
    assert {(row["kind"],row["name"],row["disposition"]) for row in expected["dependencies"]} == {
        ("font","synthetic.shx","font remapped"),("image","synthetic.png","image omitted")}
    results = []
    for size in (1000, 10000, 100000):
        pairs = drawing(size)
        snapshot = list(pairs)
        actual, metrics = measure(lambda: report(pairs))
        assert actual == expected and pairs == snapshot
        results.append({"unrelated_records": size, "valid_dependencies": 2, **metrics})
    encoded = json.dumps({"schema":"leaf.x04.sparse-dependencies.v1","synthetic":True,"timing_gate":False,
        "memory_scope":"Report allocations only; input and immutable-entry snapshot constructed before tracing.",
        "results":results},indent=2) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(encoded, encoding="utf-8")
    print(encoded, end="")


if __name__ == "__main__":
    main()
