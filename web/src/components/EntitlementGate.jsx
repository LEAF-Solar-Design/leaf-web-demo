// REAL entitlement state (replaces the old "PLACEHOLDER — NOT ENFORCED" demo).
// Driven by GET /api/entitlements -> {tier, entitlements:{run_read, run_write,
// build, converse}, source}. These gates are ENFORCED server-side now — POST
// /api/run (write tools), POST /api/author, and the converse lane's POST
// /api/sessions/{id}/messages return HTTP 403 {entitlement_required} on a
// disallowed tier — so this panel states the real plan and never makes a fake
// enforcement claim. In mock / off-auth (entitlements null) it shows the honest
// "demo tier · full access" line. Calm vocabulary: square mono tags, no pills.
//
// The tier itself is upstream truth: leaf_website FU-1 derives the Auth0 claim
// from the ORGANIZATION AGGREGATE (any active/trialing subscription in the org
// entitles every member), so this panel must only ever reflect the server
// payload — never infer entitlement from anything client-side.

const ROWS = [
  { key: 'run_read', label: 'Run read-only tools', hint: 'drawing.read', short: 'read tools' },
  { key: 'run_write', label: 'Run editing tools', hint: 'drawing.write', short: 'editing' },
  { key: 'build', label: 'Author new tools', hint: 'build lane', short: 'authoring' },
  { key: 'converse', label: 'Chat with the assistant', hint: 'converse lane', short: 'chat' },
]

// Unknown/absent capability -> permissive (the off-auth demo grants everything).
function entValue(ents, key) {
  if (!ents || typeof ents[key] === 'undefined' || ents[key] === null) return true
  return ents[key] !== false
}

export default function EntitlementGate({ tier, entitlements, loading, mock }) {
  const ents = entitlements?.entitlements || null
  // A real policy read exists only in live mode with the endpoint deployed.
  const known = !mock && !!entitlements
  const source = entitlements?.source || null
  const rows = ROWS.map((r) => ({ ...r, on: entValue(ents, r.key) }))
  const allOn = rows.every((r) => r.on)
  const tierLabel = tier || 'demo'

  return (
    <section className="ent-panel" aria-label="Entitlements">
      <div className="ent-head">
        <span className="ent-k">Entitlements</span>
        <span className="ent-tier">tier {tierLabel}</span>
        {known ? (
          <span className="ent-src">enforced server-side{source ? ` · ${source}` : ''}</span>
        ) : (!mock && loading) ? (
          <span className="ent-src dim ent-checking">
            <span className="dot live pulse" aria-hidden="true" />
            Checking plan
          </span>
        ) : (
          <span className="ent-src dim">demo · not signed in</span>
        )}
      </div>

      <div className="ent-rows">
        {rows.map((r) => (
          <div key={r.key} className={`ent-row ${r.on ? 'on' : 'off'}`}>
            <span className="ent-label">
              {r.label}{r.hint ? <span className="dim"> · {r.hint}</span> : null}
            </span>
            <span className={`ent-state ${r.on ? 'on' : ''}`}>{r.on ? 'included' : 'not in plan'}</span>
          </div>
        ))}
      </div>

      {!known ? (
        <p className="ent-note">
          demo tier · full access — entitlements apply once you sign in to a plan.
        </p>
      ) : allOn ? (
        <p className="ent-note">All capabilities are included on the {tierLabel} plan.</p>
      ) : (
        // Name the ACTUAL missing capabilities — the policy file is operator-
        // tunable per key, so no fixed sentence stays true across policies.
        <p className="ent-note amber">
          Some capabilities aren’t in the {tierLabel} plan ({rows.filter((r) => !r.on).map((r) => r.short).join(', ')}); a higher tier unlocks them.
        </p>
      )}
    </section>
  )
}

// Calm amber notice for a 403 entitlement_required rejection from POST /api/run
// (a write tool the plan doesn't include). Reuses the QuotaCard posture — this is
// an expected plan boundary, not a failure to alarm about. Nothing ran.
export function EntitlementNotice({ required, tier, message }) {
  const need = required === 'build' ? 'authoring tools (build)'
    : required === 'run_write' ? 'editing tools (write)'
    : required === 'run_read' ? 'read tools'
    : (required || 'this capability')
  return (
    <div className="banner quota" role="status">
      <b>Plan</b>
      <span className="banner-rest">
        {' — '}
        {message || `your ${tier || 'current'} plan doesn’t include ${need}`}
        {'; nothing ran — it was blocked before any billable work.'}
      </span>
      <span className="banner-tail">
        <span className="banner-since">clears when the plan includes it</span>
      </span>
    </div>
  )
}
