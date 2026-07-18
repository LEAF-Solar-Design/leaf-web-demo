// Lower row (design 20ab544c9f6b): the entitlement gate + the sandboxed Build
// chip. BOTH are honest stubs — no enforcement, no pipeline. The gate shows the
// design's dignified hatched disabled-state on a fake "pro" capability, clearly
// labeled a placeholder; the Build chip is visually quarantined with a hazard
// hatch + trust-tier ladder. Neither makes a real entitlement or execution
// claim (that logic is explicitly out of this lane — see server/tenancy.py).
export default function EntitlementGate({ tier }) {
  return (
    <div className="lower">
      <div className="gate">
        <div className="lock">Not entitled · tier {tier}</div>
        <h4>route.homerun.optimal</h4>
        <p>
          MILP-optimal DC homerun routing with obstacle avoidance. Your tier includes greedy
          routing; the optimal solver would sit on a higher tier.
        </p>
        <button className="up" disabled title="Placeholder — no billing wired in this demo">
          Request tier upgrade
        </button>
        <div className="placeholder">Placeholder — entitlement is not enforced in this demo</div>
      </div>

      <div className="build">
        <span className="quarantine">Sandboxed · quarantined</span>
        <div className="id">build·chip 4a71 · sample</div>
        <h4>shade.annual.bifacial-albedo</h4>
        <p>
          A requested capability building against a sandboxed corpus. Results would be advisory
          only and could not enter Export until reviewed inline.
        </p>
        <div className="tiers">
          <span className="on">sandboxed</span>
          <span>reviewed-inline</span>
          <span>trusted</span>
        </div>
        <div className="placeholder">Placeholder — no Build pipeline runs in this demo</div>
      </div>
    </div>
  )
}
