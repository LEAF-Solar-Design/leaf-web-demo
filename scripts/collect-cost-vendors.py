#!/usr/bin/env python3
"""Print one month of Leaf's declared vendor and subscription costs as JSON Lines (TCM-07).

Reads cost_meter/data/cost-vendors.yaml through server/cost_meter/vendors.py and writes one
cost and one usage observation per covering entry to stdout. It never writes the
ledger; a publisher joins the observations by resource_id. Transparency, never billing.

    python scripts/collect-cost-vendors.py --period 2026-09 [--config PATH]

Exit 0 on success, 2 on a malformed schedule or period (nothing is printed).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SERVER_DIR = REPO_ROOT / "server"
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from cost_meter.vendors import (  # noqa: E402
    VendorConfigError, load_vendor_config, vendor_observations,
)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--period", required=True, help="UTC calendar month, YYYY-MM")
    parser.add_argument("--config", type=Path,
                        help="vendor schedule YAML (default: LEAF_COST_VENDORS_CONFIG or packaged data)")
    args = parser.parse_args(argv)
    try:
        observations = vendor_observations(args.period, load_vendor_config(args.config))
    except (VendorConfigError, OSError) as exc:
        print(f"collect-cost-vendors: {exc}", file=sys.stderr)
        return 2
    # Built whole before printing so a failure never leaves a partial stream.
    sys.stdout.write("".join(json.dumps(o, sort_keys=True) + "\n" for o in observations))
    return 0


if __name__ == "__main__":
    sys.exit(main())
