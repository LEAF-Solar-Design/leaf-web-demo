// THE ONE GUARD SEAM every free-text transport passes through (slice 8a,
// round 3). Nothing here is a composer, a component, or a controller.
//
// WHY THE GUARD MOVED HERE, stated so nobody moves it back: rounds 1 and 2 put
// the decision in the composers. Round 1 guarded the command bar and the
// assistant reply box was open. Round 2 guarded the reply box and the /try bar
// plus the retry paths were open. Round 3's review found the Author-a-tool
// textarea open on BOTH shells, reaching the very endpoint the PR claimed was
// guarded, and found the one-shot override latching because both hosts
// short-circuit ABOVE the controller that held it. Three rounds, one shape of
// defect: a census of composers is a census someone will be short by one.
//
// There is no census of transports to get wrong. A credential reaches a model
// by leaving this browser through a function in api.js, converse.js or
// operatorClient.js, and every one of those functions calls `guardedText`
// before it touches the network — before its mock branch too, so the demo and
// the live app refuse identically. A new composer, a new retry chip, a new
// keyboard shortcut, a whole new shell: none of them can route around a guard
// that sits on the wire. `web/src/composer.test.mjs` pins that property by
// reading the sources, so adding a fifth sender without the seam fails a gate
// rather than shipping.
//
// THE OVERRIDE IS A PARAMETER, NEVER STATE. `allowSecretOnce: true` is passed
// INTO the one call the user just authorised with a "Send anyway" click, and it
// dies with that call's stack frame. Round 2's override was a latch (a module
// variable in the controller, a ref in two components), and a latch armed by a
// click whose follow-on call never reached the guard stayed armed for the next
// unrelated keystroke. Nothing in this file, and nothing that calls it, stores
// whether an override was granted. A "Send anyway" click that lands while the
// host is busy is a plain no-op, which is the fail-closed direction.
//
// AND IT CANNOT TALK PAST A NAMED SHAPE. `allowSecretOnce` is honoured only for
// a refusal whose hits are ALL overridable (the deliberately fuzzy generic
// `token: ...` assignment). An Anthropic key, an AWS pair, a private key header
// is refused with the flag set, so a caller cannot widen the policy by passing
// a boolean.
//
// THE VALUE NEVER LEAVES lib/secretPatterns.js. `evaluateSecretGuard` returns
// pattern identity, a frozen sentence and a mask (a four-character shape prefix
// behind a fixed bullet run). SecretRefusedError carries exactly that object and
// its MESSAGE IS THE FROZEN SENTENCE, so an error that escapes into a log, a
// toast, a banner or an operator console still cannot echo the credential.

import { evaluateSecretGuard } from './secretPatterns.js'
import { track } from '../telemetry.js'

// Does this shell mount a credential surface at all? It changes
// ONLY the refusal's closing sentence (point at the Claude accounts panel, or
// say plainly that nothing here can hold one) and never the decision to refuse.
//
// It is module state because the transports are plain functions that no React
// tree threads props through, and it FAILS HONEST: false until a shell answers,
// so a caller that never answers names no control rather than inventing one.
// App and ToolCast set it from the same expression that mounts the panel.
let mountAvailable = false

/** Answer "does this shell mount the Claude accounts panel" for the copy. */
export function setCredentialMountAvailable(value) {
  mountAvailable = value === true
}

/** The current answer. Exported for the shells' own copy decisions and tests. */
export function getCredentialMountAvailable() {
  return mountAvailable
}

/**
 * The typed refusal every guarded transport throws. A caller distinguishes it
 * from a network failure with `isSecretRefused`, renders `refusal` in its own
 * notice, and may show `message` raw: it is the frozen sentence, never the
 * value, never a URL, never a status code.
 */
export class SecretRefusedError extends Error {
  constructor(refusal) {
    super(String(refusal?.reason || 'Credentials never go to the model.'))
    this.name = 'SecretRefusedError'
    // The discriminator, checked by value not by `instanceof`: a bundle split
    // or a duplicated module copy breaks prototype identity, and a guard that
    // silently stops being recognised is the failure this flag prevents.
    this.secretRefused = true
    this.refusal = Object.freeze({
      id: String(refusal?.id || 'generic'),
      reason: String(refusal?.reason || 'Credentials never go to the model.'),
      masked: String(refusal?.masked || ''),
      overridable: refusal?.overridable === true,
    })
    // Terminal by construction: retrying the identical text refuses again, so
    // the author lane's resume machinery must not treat this as a blip.
    this.authorTerminal = true
  }
}

/** True for a refusal from this seam, however the module was loaded. */
export function isSecretRefused(error) {
  return !!error && error.secretRefused === true && !!error.refusal
}

/**
 * The decision every guarded transport runs before it touches the network.
 *
 * Returns `{ok: true, text}` when the text may be sent, `{ok: false, refusal}`
 * otherwise, where `refusal` is `{id, reason, masked, overridable}` — identity,
 * a frozen sentence and a mask, NEVER the value.
 *
 * `allowSecretOnce` is a per-call authorisation and nothing here remembers it.
 * It is honoured ONLY for a wholly overridable hit set; a named shape refuses
 * with it set.
 *
 * `credentialMountAvailable` overrides the module answer for a caller that
 * knows better (api.js derives it from its own `mock` argument). Omitted, the
 * module answer applies, which defaults to false.
 *
 * Linear time: one `evaluateSecretGuard` pass, no allocation beyond its hits.
 */
export function guardedText(text, { allowSecretOnce = false, credentialMountAvailable } = {}) {
  const refusal = evaluateSecretGuard(text, {
    credentialMountAvailable: credentialMountAvailable === undefined
      ? mountAvailable
      : credentialMountAvailable === true,
  })
  if (!refusal) return { ok: true, text }
  if (allowSecretOnce === true && refusal.overridable === true) return { ok: true, text }
  // Pattern identity ONLY. The value reaches no log, no telemetry payload and
  // no DOM node outside the masked span a composer renders.
  track('transport.secret_refused', { pattern_id: refusal.id })
  return { ok: false, refusal }
}
