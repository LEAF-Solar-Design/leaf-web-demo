from render_unit_economics_issue import render


def test_measured_report_renders_the_three_pricing_inputs():
    body = render({
        "scope": "fleet",
        "period": {"start": "2026-08-01T00:00:00Z", "end": "2026-09-01T00:00:00Z"},
        "hosted_accounts": 10,
        "decision_metrics": {
            "shared_fixed_cost_usd": 100,
            "shared_fixed_cost_per_hosted_account_usd": 10,
            "marginal_cost_meters": {
                "agent": {"turns": 5, "usd_est": 1.25},
                "aps": {"runs": 2, "usd_est": 3.5},
                "hosted_jobs_cross_check": {"costed_jobs": 2, "usd_est": 3.5},
            },
            "renewal_signals": {"invoice_paid": 4, "payment_failed": 1, "canceled": 0},
        },
        "coverage_gaps": [],
    })
    assert "Shared fixed cost per hosted account | $10.00" in body
    assert "Agent work | 5 turns, $1.25" in body
    assert "Paid invoices | 4" in body
    assert "Coverage gaps: none reported" in body


def test_unconfigured_state_is_visible_and_stays_owned():
    body = render({
        "status": "unconfigured",
        "message": "Set the report URL and secret.",
    })
    assert "Current state: **unconfigured**" in body
    assert "Set the report URL and secret" in body
    assert "next scheduled run" in body


def test_tenant_scoped_report_is_rejected():
    try:
        render({"scope": "tenant"})
    except ValueError as exc:
        assert "fleet reports only" in str(exc)
    else:
        raise AssertionError("tenant-scoped reports must not reach GitHub issues")
