#!/usr/bin/env python3
"""Replay the committed simulator gold set against the working tree.

The runner loads ``platform/replay.py`` by file path so it remains safe to run
from the repository root, where the ``platform`` directory shadows Python's
stdlib module of the same name.
"""
from __future__ import annotations

import argparse
import copy
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any


SIMGATE_DIR = Path(__file__).resolve().parent
PLATFORM_DIR = SIMGATE_DIR.parent
DEFAULT_GOLD_SET = SIMGATE_DIR / "gold-set.json"


def load_replay_module() -> Any:
    """Load the replay core without importing the shadowing platform package."""
    spec = importlib.util.spec_from_file_location(
        "leaf_platform_simgate_replay", PLATFORM_DIR / "replay.py"
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load platform/replay.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def report_counts(report: Any) -> dict[str, int | bool]:
    """Return the small, stable CI report shape used in logs and PR comments."""
    return {
        "total": report.total,
        "matched": report.matched,
        "diverged": len(report.diverged),
        "skipped": len(report.skipped),
        "ok": report.ok,
    }


def replay(gold_set: Path) -> int:
    core = load_replay_module()
    try:
        report = core.replay_gold_set(core.load_gold_set(gold_set))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
        return 2

    print(json.dumps(report_counts(report), sort_keys=True))
    # ReplayReport.ok is intentionally strict: a malformed record is a visible
    # skip and therefore a CI failure, just like a digest divergence.
    return 0 if report.ok else 1


def mint(payload_path: Path, gold_set: Path) -> int:
    core = load_replay_module()
    try:
        payloads = json.loads(payload_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"cannot read payload fixture: {exc}", file=sys.stderr)
        return 2
    if not isinstance(payloads, dict):
        print("payload fixture must be a JSON object keyed by stable record id", file=sys.stderr)
        return 2
    records = core.mint_gold_set(payloads)
    gold_set.write_text(json.dumps(records, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8", newline="\n")
    print(f"minted {gold_set}")
    return 0


def self_test() -> int:
    """Prove the runner observes matches, divergences, and visible skips."""
    core = load_replay_module()
    gold = core.mint_gold_set({"self-test": {"distance_m": 1.23456, "ok": True}})
    if not core.replay_gold_set(gold).ok:
        print("self-test failed: matching gold set was not accepted", file=sys.stderr)
        return 1

    divergent = copy.deepcopy(gold)
    divergent[0]["payload"]["distance_m"] = 1.2348
    if core.replay_gold_set(divergent).ok:
        print("self-test failed: digest divergence was accepted", file=sys.stderr)
        return 1

    skipped = copy.deepcopy(gold)
    skipped.append({"id": "missing-digest", "payload": {"x": 1}})
    if core.replay_gold_set(skipped).ok:
        print("self-test failed: skipped record was accepted", file=sys.stderr)
        return 1

    print("simulator-gate self-test: PASS")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gold-set", type=Path, default=DEFAULT_GOLD_SET,
                        help="gold-set JSON to replay (default: committed gold set)")
    parser.add_argument("--mint", type=Path, metavar="PAYLOADS",
                        help="mint a gold set from a deterministic payload JSON object")
    parser.add_argument("--write-gold", type=Path, metavar="PATH",
                        help="output path used with --mint")
    parser.add_argument("--self-test", action="store_true",
                        help="run dependency-free runner checks")
    args = parser.parse_args()

    if args.self_test:
        if args.mint or args.write_gold:
            parser.error("--self-test cannot be combined with mint options")
        return self_test()
    if args.mint:
        if not args.write_gold:
            parser.error("--mint requires --write-gold")
        return mint(args.mint, args.write_gold)
    if args.write_gold:
        parser.error("--write-gold requires --mint")
    return replay(args.gold_set)


if __name__ == "__main__":
    raise SystemExit(main())
