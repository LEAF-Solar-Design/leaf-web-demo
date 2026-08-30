"""Render a fleet unit-economics API result as one owner-facing issue body."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any, Mapping


def _money(value: Any) -> str:
    if value is None:
        return "not measured"
    return f"${float(value):,.2f}"


def render(report: Mapping[str, Any], run_url: str = "") -> str:
    if report.get("scope") not in {None, "fleet"}:
        raise ValueError("unit-economics issue renderer accepts fleet reports only")

    state = str(report.get("status") or "measured")
    lines = [
        "Leaf platform pricing depends on this recurring measurement lane.",
        "",
        f"Current state: **{state}**.",
        "",
    ]
    if state != "measured":
        lines.extend([
            str(report.get("message") or "The fleet report was not available."),
            "",
            "This issue stays open and will be updated by the next scheduled run.",
        ])
    else:
        metrics = report.get("decision_metrics") or {}
        marginal = metrics.get("marginal_cost_meters") or {}
        renewal = metrics.get("renewal_signals") or {}
        agent = marginal.get("agent") or {}
        aps = marginal.get("aps") or {}
        jobs = marginal.get("hosted_jobs_cross_check") or {}
        period = report.get("period") or {}
        lines.extend([
            f"Period: `{period.get('start', 'unknown')}` to `{period.get('end', 'unknown')}`.",
            "",
            "| Pricing input | Current measurement |",
            "| --- | ---: |",
            f"| Active hosted accounts | {int(report.get('hosted_accounts') or 0):,} |",
            f"| Shared fixed cost | {_money(metrics.get('shared_fixed_cost_usd'))} |",
            f"| Shared fixed cost per hosted account | {_money(metrics.get('shared_fixed_cost_per_hosted_account_usd'))} |",
            f"| Agent work | {int(agent.get('turns') or 0):,} turns, {_money(agent.get('usd_est'))} |",
            f"| APS work | {int(aps.get('runs') or 0):,} runs, {_money(aps.get('usd_est'))} |",
            f"| Hosted job cross-check | {int(jobs.get('costed_jobs') or 0):,} costed jobs, {_money(jobs.get('usd_est'))} |",
            f"| Paid invoices | {int(renewal.get('invoice_paid') or 0):,} |",
            f"| Payment failures | {int(renewal.get('payment_failed') or 0):,} |",
            f"| Cancellations | {int(renewal.get('canceled') or 0):,} |",
            "",
        ])
        gaps = [str(item) for item in report.get("coverage_gaps") or []]
        if gaps:
            lines.append("Coverage gaps:")
            lines.append("")
            lines.extend(f"- {gap}" for gap in gaps)
        else:
            lines.append("Coverage gaps: none reported.")
        lines.extend([
            "",
            "The three meters are evidence for a pricing decision. This workflow does not change prices.",
        ])
    if run_url:
        lines.extend(["", f"Latest workflow run: {run_url}"])
    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    report = json.loads(args.input.read_text(encoding="utf-8"))
    body = render(report, os.environ.get("UNIT_ECONOMICS_RUN_URL", ""))
    args.output.write_text(body, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
