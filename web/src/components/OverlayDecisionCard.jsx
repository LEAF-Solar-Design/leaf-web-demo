/**
 * T1 overlay decision card — the operator's ONE surface for a runtime theme or
 * copy change, and the only human gate before it reaches a tenant.
 *
 * WHY THIS HAS TWO BUTTONS. The spec described "one button", and an
 * adversarial review caught that denial is not optional: a proposal the
 * operator will not approve must be closable, or the requester keeps looking
 * at a preview nobody ever decided on. Approve and Deny are both mandatory,
 * authenticated state transitions.
 *
 * WHAT THIS COMPONENT MUST NOT DO, and why each is deliberate:
 *
 *   NEVER render token values as markup. Copy tokens arrive from a chat
 *   request, which means they are untrusted text. They render as text nodes
 *   only — no dangerouslySetInnerHTML, no attribute sinks, no title tooltips.
 *
 *   NEVER show a colour as a swatch WITHOUT its literal value. A swatch alone
 *   is exactly how a low-contrast pair sneaks past a human check; the operator
 *   needs to read `#fefefe` next to it.
 *
 *   NEVER let a stale card decide. `documentVersion` came with the proposal
 *   and rides back on the tap; the server compare-and-swaps it. The card does
 *   not attempt its own freshness check — a client-side guess would only mask
 *   the conflict the server exists to catch.
 *
 *   NEVER send two decisions. `deciding` latches on the first tap so a
 *   double-click cannot fire twice. The decisionKey makes the server-side
 *   retry idempotent regardless, which is the real guarantee — this is the
 *   cheap half of a belt-and-braces pair.
 */

import { useState } from 'react'

/** Suspicious-character exposure for copy the operator is about to approve.
 *  The server already refuses bidi and invisible characters, so anything here
 *  means something upstream changed — show it rather than render it. */
// Escaped, never literal. Writing these characters into the source put a
// NUL byte in the file, which made git treat this component as BINARY —
// so its diff was unreviewable. A file about exposing invisible characters
// must not smuggle them itself.
const SUSPECT = new RegExp(
  '[\u0000-\u0008\u000B\u000C\u000E-\u001F\u00AD\u034F\u061C'
  + '\u200B-\u200F\u202A-\u202E\u2060\u2066-\u2069'
  + '\u3164\uFEFF]',
)

function visibleCopy(value) {
  return String(value ?? '').replace(
    new RegExp(SUSPECT.source, 'g'),
    (ch) => `\\u${ch.codePointAt(0).toString(16).padStart(4, '0').toUpperCase()}`,
  )
}

const isColorToken = (id) => String(id).startsWith('color.')

/** A canonical colour, and nothing else, may reach a style sink.
 *
 *  A review found the swatch assigned any token value straight to
 *  `style.background`, so `url(https://attacker.example/track)` in a chat
 *  request made the OPERATOR's browser issue that request while they were
 *  deciding whether to approve it. The server should never have accepted the
 *  value, and now does not — but the card must not be the thing relying on
 *  that. Anything not shaped like #rrggbb renders as text only, with no
 *  swatch. */
const CANONICAL_COLOR = /^#[0-9a-f]{6}$/i
const isRenderableColor = (v) => CANONICAL_COLOR.test(String(v ?? ''))

export default function OverlayDecisionCard({
  proposal,            // {proposal_id, tokens, requested_by, request_text}
  documentVersion,     // CAS witness the server will check
  labels = {},         // token_id -> human label, from the server registry
  onDecide,            // (proposalId, {approve, decisionKey, documentVersion})
  busy = false,
}) {
  const [deciding, setDeciding] = useState(null) // 'approve' | 'deny' | null
  const [error, setError] = useState(null)

  if (!proposal) return null
  const tokens = proposal.tokens || {}
  const entries = Object.entries(tokens)

  const decide = async (approve) => {
    if (deciding || busy) return
    setDeciding(approve ? 'approve' : 'deny')
    setError(null)
    try {
      // Generated per TAP, not per render: a key that changed on every render
      // would make the server's idempotency check useless.
      const decisionKey = `od-${proposal.proposal_id}-${
        globalThis.crypto?.randomUUID?.() ?? Date.now().toString(36)
      }`
      await onDecide(proposal.proposal_id, { approve, decisionKey, documentVersion })
    } catch (e) {
      // Leave the card OPEN on failure. Clearing it would tell the operator
      // the decision landed when it did not.
      setError(e?.message || 'The decision did not go through — nothing changed.')
      setDeciding(null)
    }
  }

  return (
    <div className="overlay-card" role="group" aria-label="Theme change awaiting your decision">
      <div className="overlay-card-head">
        <span className="overlay-card-title">Theme change requested</span>
        {proposal.requested_by && (
          <span className="dim"> · by {visibleCopy(proposal.requested_by)}</span>
        )}
      </div>

      {proposal.request_text && (
        <p className="overlay-card-ask">“{visibleCopy(proposal.request_text)}”</p>
      )}

      <ul className="overlay-card-tokens">
        {entries.map(([id, value]) => (
          <li key={id} className="overlay-token">
            <span className="overlay-token-label">{labels[id] || id}</span>
            {isColorToken(id) && isRenderableColor(value) ? (
              <>
                {/* Swatch AND literal: a swatch alone hides a low-contrast pair. */}
                <span
                  className="overlay-swatch"
                  style={{ background: value }}
                  aria-hidden="true"
                />
                <code className="overlay-token-value">{visibleCopy(value)}</code>
              </>
            ) : (
              <code className="overlay-token-value">{visibleCopy(value)}</code>
            )}
          </li>
        ))}
      </ul>

      {entries.length === 0 && (
        <p className="dim">This proposal carries no tokens — deny it.</p>
      )}

      <div className="overlay-card-actions">
        <button
          type="button"
          className="chip-act"
          disabled={!!deciding || busy || entries.length === 0}
          onClick={() => decide(true)}
        >
          {deciding === 'approve' ? 'Applying…' : 'Approve — applies to this tenant'}
        </button>
        <button
          type="button"
          className="chip-act overlay-deny"
          disabled={!!deciding || busy}
          onClick={() => decide(false)}
        >
          {deciding === 'deny' ? 'Denying…' : 'Deny'}
        </button>
      </div>

      {error && <p className="overlay-card-error">{error}</p>}
    </div>
  )
}
