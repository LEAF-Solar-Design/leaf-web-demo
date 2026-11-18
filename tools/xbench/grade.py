"""Command-line entry point for shared X-bench grading."""

import argparse
import importlib.util
from pathlib import Path
import re
import sys
import unicodedata

sys.path.insert(0, str(Path(__file__).resolve().parent))
import xbench_core


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def main(argv=None, *, opener=None):
    parser = _Parser(description=__doc__)
    parser.add_argument("bench")
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--readiness-dir", default="D:/agents/production-lanes-2026-11-10/XBENCH/driver/readiness")
    parser.add_argument("--no-readiness", action="store_true")
    parser.add_argument("--benches-dir", default=str(Path(__file__).resolve().parent / "benches"))
    parser.add_argument("--opt", action="append", default=[], metavar="KEY=VALUE")
    try:
        args = parser.parse_args(argv)
        if len(args.opt) > 16:
            raise ValueError("at most 16 options are allowed")
        options = {}
        for option in args.opt:
            key, separator, value = option.partition("=")
            if not separator or re.fullmatch(r"[a-z][a-z0-9_]{0,31}", key) is None:
                raise ValueError("--opt requires KEY=VALUE with a valid key")
            if len(value) > 256 or any(unicodedata.category(char) == "Cc" for char in value):
                raise ValueError("--opt value must be at most 256 characters without control characters")
            if key in options:
                raise ValueError("duplicate --opt key: " + key)
            options[key] = value
        if args.bench not in xbench_core.BENCH_IDS:
            raise ValueError("invalid bench: " + args.bench)
        base = xbench_core.validate_base(args.base)
        manifest = xbench_core.load_manifest(args.manifest)
        if manifest["bench"] != args.bench:
            raise ValueError("manifest bench does not match argument")
        module_path = Path(args.benches_dir) / (args.bench.lower() + ".py")
        if not module_path.is_file():
            raise ValueError("bench " + args.bench + " grader not built")
    except ValueError as exc:
        print(str(exc).replace("\n", " "), file=sys.stderr)
        return xbench_core.EXIT_USAGE

    started_at = xbench_core.utc_now()
    fixtures = xbench_core.verify_fixtures(manifest)
    source_sha = None

    def receipt(verdict, steps, limits=""):
        return xbench_core.write_grade(
            args.out, bench=args.bench, verdict=verdict, source_sha=source_sha,
            manifest_sha256=manifest["manifest_sha256"], started_at=started_at,
            finished_at=xbench_core.utc_now(), steps=steps, fixtures=fixtures, limits=limits,
            options=options,
        )

    failed = [row["id"] for row in fixtures if not row["ok"]]
    if failed:
        receipt("UNREADABLE", [], "fixture verification failed: " + ", ".join(failed))
        return xbench_core.EXIT_UNREADABLE
    try:
        source_sha = xbench_core.read_health(base, opener=opener)
    except xbench_core.HealthError as exc:
        receipt("UNREADABLE", [], "staging health unreadable: " + str(exc))
        return xbench_core.EXIT_UNREADABLE

    limits = ""
    try:
        module_name = "xbench_bench_" + args.bench.lower()
        spec = importlib.util.spec_from_file_location(module_name, module_path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        limits = getattr(module, "LIMITS", "")
        if not isinstance(limits, str):
            raise TypeError("LIMITS must be a string")
        ctx = xbench_core.BenchContext(args.bench, manifest, base, Path(args.out), source_sha, fixtures,
                                       options=options)
        steps = module.run(ctx)
        if not isinstance(steps, list) or not all(isinstance(step, xbench_core.Step) for step in steps):
            raise TypeError("run(ctx) must return a list of Step")
        verdict = xbench_core.aggregate(steps)
    except Exception as exc:
        steps = [xbench_core.Step(1, "UNREADABLE", type(exc).__name__ + ": " + str(exc))]
        receipt("UNREADABLE", steps, limits if isinstance(limits, str) else "")
        return xbench_core.EXIT_UNREADABLE

    grade_path = receipt(verdict, steps, limits)
    if verdict == "PASS" and not args.no_readiness:
        xbench_core.write_readiness(args.readiness_dir, bench=args.bench, sha=source_sha, receipt=grade_path)
    print(f"{args.bench} {verdict} {source_sha} {grade_path}")
    return xbench_core.exit_code(verdict)


if __name__ == "__main__":
    sys.exit(main())
