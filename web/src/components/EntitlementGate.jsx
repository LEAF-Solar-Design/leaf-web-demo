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
//
// Slice 13c adds the panel's FIRST control: the usage-telemetry consent
// switch. It sits below the entitlement rows in its own block, so the rows
// above keep their exact markup and hairline rhythm.
import { useId, useSyncExternalStore } from 'react'

import {
  setUsageConsent,
  subscribeUsageConsent,
  usageConsentGranted,
} from '../lib/telemetryConsent.js'
import { TELEMETRY_BUILD_DISABLED } from '../telemetry.js'

// The exact copy. Honest on both halves: it names what would be collected AND
// says what the switch does not touch, because a toggle that silently also
// governed crash reporting would be the same lie in the other direction.
//
// PRESENT TENSE, deliberately: no usage emitter exists in the tree yet (grep
// trackUsage across web/src and only telemetry.js and its own specs answer),
// so copy reading "Share how you use the studio" would promise a viewer who
// turns this on that something starts flowing today. It does not. The switch
// is the permission, and the permission is real now; the signals arrive with
// the emitters slices 10-13 add.
export const CONSENT_LABEL = 'Usage telemetry'
export const CONSENT_COPY = 'Allow sharing how you use the studio (menu picks, searches) once those signals exist. Product events are unaffected.'

// REASONS style: one sentence naming why the control cannot be used, never a
// disabled control with no explanation. Exact-string tested — a reworded
// reason is a product change and should fail the spec, not slip through.
export const CONSENT_REASONS = Object.freeze({
  buildDisabled: 'Telemetry is off for this build.',
  // The fence is a BUILD flag, and the grant outlives it. Saying only "off for
  // this build" would let a viewer read the OFF-looking row as "my yes is
  // gone", and the next build without the fence would then resume collecting
  // with no re-ask. So the stored yes is stated, and taking it back is offered
  // in the same sentence as the control that does it.
  storedGrantKept: 'Your saved yes is kept and would resume in a build without this fence. Turn the switch off to take it back now.',
})

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

/** The consent switch. A real ARIA switch, not a checkbox pretending. Its
 * 32x18 pill takes its values from `.toggle, .switch input` in styles.css,
 * the repo's one switch rule: accent track on, --on-accent knob on, and the
 * shared two-layer keyboard ring every other control uses.
 *
 * KEYBOARD, and why the handler exists at all: a native <button> activates on
 * Enter (keydown) and Space (keyup), so a click handler alone would already
 * work in a browser — but not deterministically under test, and a switch whose
 * keyboard path is untested is a switch that breaks silently. The handler
 * calls preventDefault() on both keys, which CANCELS the browser's synthesized
 * click, so every activation path (mouse, touch tap, Space, Enter) toggles
 * exactly once. Removing the preventDefault would double-toggle on Enter.
 *
 * Touch: a tap on a <button> is a click; no extra pointer plumbing, and the
 * 32x18 pill sits inside a row-height hit target.
 *
 * Under the build fence the switch is ONE-WAY, not dead. A viewer must always
 * be able to take consent back, so a stored grant keeps the control operable
 * and clicking it revokes; only the granting direction is refused, and once
 * nothing is stored the button is `disabled` (a native disabled button fires
 * neither click nor keydown, so that refusal needs no handler-side guard).
 * Both reasons render as text below, never only as a title attribute a
 * keyboard or screen reader user would never reach, and both are named by
 * aria-describedby so the focused control announces them. */
function UsageConsentRow({ buildDisabled }) {
  const labelId = useId()
  const copyId = `${labelId}-copy`
  const reasonId = `${labelId}-reason`
  const keptId = `${labelId}-kept`
  // The store is external (shared with the emitter and with any other mounted
  // panel), so this is exactly what useSyncExternalStore exists for: no
  // effect-based mirror to drift, and a revoke in one panel is instantly true
  // in the other and in telemetry.js's next buildEvent call.
  const granted = useSyncExternalStore(
    subscribeUsageConsent,
    usageConsentGranted,
    // Server snapshot: nothing is consented before a browser exists.
    () => false,
  )
  // The switch shows the STORED state, under the fence too: the grant is what
  // the control governs, and it survives the build flag. What the fence
  // changes is what the grant DOES, and that is what the reason text below
  // says. Hiding the stored yes here would tell a viewer their consent was
  // gone while a build without the fence would silently resume on it.
  const on = granted
  // One-way under the fence: revoke yes, grant never.
  const canToggle = !buildDisabled || granted

  const toggle = () => {
    if (buildDisabled && !granted) return   // belt and braces beside `disabled`
    setUsageConsent(!granted)
  }
  const onKeyDown = (ev) => {
    if (ev.key !== ' ' && ev.key !== 'Spacebar' && ev.key !== 'Enter') return
    ev.preventDefault()
    toggle()
  }

  return (
    <div className="ent-consent">
      <div className={`ent-row ent-consent-row ${on ? 'on' : 'off'}`}>
        <span className="ent-label" id={labelId}>
          {CONSENT_LABEL}<span className="dim"> · this browser</span>
        </span>
        <button
          type="button"
          role="switch"
          aria-checked={on}
          aria-labelledby={labelId}
          aria-describedby={[
            copyId,
            buildDisabled ? reasonId : null,
            buildDisabled && granted ? keptId : null,
          ].filter(Boolean).join(' ')}
          className={`ent-switch${on ? ' on' : ''}`}
          disabled={!canToggle}
          onClick={toggle}
          onKeyDown={onKeyDown}
        >
          <span className="ent-switch-knob" aria-hidden="true" />
        </button>
      </div>
      <p className="ent-note ent-consent-copy" id={copyId}>{CONSENT_COPY}</p>
      {buildDisabled ? (
        <p className="ent-note ent-consent-reason" id={reasonId}>{CONSENT_REASONS.buildDisabled}</p>
      ) : null}
      {buildDisabled && granted ? (
        <p className="ent-note ent-consent-reason" id={keptId}>{CONSENT_REASONS.storedGrantKept}</p>
      ) : null}
    </div>
  )
}

export default function EntitlementGate({
  tier,
  entitlements,
  loading,
  mock,
  // Defaulted from the build fence so every existing mount site is unchanged;
  // a prop only so the disabled arm has a spec that does not need a rebuild.
  telemetryDisabled = TELEMETRY_BUILD_DISABLED,
}) {
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

      <UsageConsentRow buildDisabled={telemetryDisabled} />
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
