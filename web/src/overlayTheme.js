/**
 * T1 preview render: apply a tenant/session overlay as CSS custom properties.
 *
 * This is the half that makes the feature visible. Until it existed, a user
 * could request a colour change, the server could validate and store it, an
 * operator could approve it, and nothing on any screen would move.
 *
 * WHY CUSTOM PROPERTIES AND NOT A STYLESHEET SWAP. The whole premise of T1 is
 * that an approved change appears without a build or a deploy. Setting
 * variables on :root is the only mechanism that is genuinely instant, is
 * trivially reversible (remove the property, the committed default returns),
 * and cannot introduce a rule that outlives the overlay. A generated
 * stylesheet would need cache-busting and a load round-trip, which is exactly
 * the deploy latency the tier exists to avoid.
 *
 * WHY VALUES ARE RE-CHECKED HERE. The server validates on the way in, and a
 * review still found `url(...)` reaching a style sink through a path that
 * skipped validation. This is a sink: whatever reaches it is applied by the
 * browser. It re-checks rather than trusting, because the cost is a regex and
 * the failure mode is an outbound request from a user's browser.
 */

/** Token id -> the CSS custom properties it drives (a token may drive more
 *  than one — panel.bg must override both the flat card colour and the card
 *  gradient, or gradient-backed panels keep the committed theme). A CLOSED
 *  map: an id with no entry here sets nothing, so a compromised or stale
 *  server cannot invent a property name and reach a declaration this app
 *  never intended to expose.
 *
 *  The values are the REAL tokens styles.css consumes (--background,
 *  --card...), not overlay-namespaced vars: an inline custom property on
 *  :root wins over the stylesheet's :root declaration, so setting the real
 *  token is what makes every consumer repaint with zero stylesheet
 *  indirection. The first shipped version set --canvas-bg/--surface-* — names
 *  no rule ever read — and an approved overlay repainted nothing.
 *
 *  This list mirrors server/overlay_registry.py's colour vocabulary exactly;
 *  the mirror test in overlayTheme.test.js fails when either side drifts. */
export const CSS_VAR_BY_TOKEN = {
  'color.canvas.bg': ['--background'],
  'color.canvas.fg': ['--foreground'],
  'color.panel.bg': ['--card', '--card-grad'],
  'color.panel.fg': ['--panel-fg'],
  'color.accent': ['--primary'],
  'color.accent.fg': ['--on-accent'],
  'color.border': ['--border'],
}

/** Canonical six-digit hex. Deliberately narrower than CSS accepts: named
 *  colours, rgb(), and colour functions are all legitimate CSS and none of
 *  them are worth the parsing surface for a five-token palette. */
const CANONICAL_COLOR = /^#[0-9a-f]{6}$/i

export function isRenderableColor(value) {
  return CANONICAL_COLOR.test(String(value ?? ''))
}

/**
 * The custom properties a token map should set.
 *
 * Silently DROPS anything unrenderable rather than throwing. A malformed token
 * must not take down the app's theming for every other token — the operator
 * card shows the literal value, so a dropped one is visible there rather than
 * as a blank screen here.
 */
export function cssVarsFor(tokens) {
  const out = {}
  for (const [id, value] of Object.entries(tokens || {})) {
    const props = CSS_VAR_BY_TOKEN[id]
    if (!props) continue                      // not a token we expose
    if (!isRenderableColor(value)) continue   // not a value we will apply
    for (const prop of props) out[prop] = String(value)
  }
  return out
}

/**
 * Apply an overlay to a root element and return a function that undoes it.
 *
 * The undo is the point. A session preview must come off cleanly when the
 * proposal is denied, reverted, or lapses, and the only way to be sure is to
 * remove exactly the properties this call set. Restoring a saved snapshot
 * would clobber whatever a later overlay had legitimately applied in between.
 */
export function applyOverlay(tokens, root = document.documentElement) {
  const vars = cssVarsFor(tokens)
  const applied = []
  for (const [prop, value] of Object.entries(vars)) {
    root.style.setProperty(prop, value)
    applied.push(prop)
  }
  return function removeOverlay() {
    for (const prop of applied) root.style.removeProperty(prop)
  }
}

/**
 * Copy tokens, exposed as a lookup rather than applied to the DOM.
 *
 * Copy CANNOT be applied the way colour is: there is no CSS mechanism for it,
 * and injecting text into the DOM from a token map is precisely the sink the
 * card was hardened against. Components read through this and render the
 * result as a text node, so untrusted copy stays text on every path.
 */
export function copyFor(tokens, defaults = {}) {
  const out = { ...defaults }
  for (const [id, value] of Object.entries(tokens || {})) {
    if (!id.startsWith('copy.')) continue
    out[id] = String(value ?? '')
  }
  return out
}
