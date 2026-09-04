/**
 * The Plan panel's first control: the usage-telemetry consent switch
 * (slice 13c).
 *
 * Four things make this row trustworthy rather than decorative, and each has a
 * spec here: it is a real ARIA switch (so a screen reader announces on/off,
 * not "button"), every activation path toggles it exactly once (mouse, tap,
 * Space, Enter), it writes through to the SAME store the emitter reads, and
 * when the build-time kill switch is on it is disabled WITH the reason in
 * text — never a dead control the viewer has to guess about.
 *
 * The existing entitlement rows are asserted unchanged in the same file, on
 * purpose: this row is an addition, and a spec that only looks at the new
 * thing cannot notice that the old thing moved.
 */
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import EntitlementGate, { CONSENT_COPY, CONSENT_REASONS } from './EntitlementGate.jsx'
import { setUsageConsent, usageConsentGranted, USAGE_CONSENT_KEY } from '../lib/telemetryConsent.js'

const PAID = {
  tier: 'team',
  source: 'policy',
  entitlements: { run_read: true, run_write: true, build: false, converse: true },
}

const switchEl = () => screen.getByRole('switch', { name: /Usage telemetry/ })

beforeEach(() => {
  try { localStorage.clear() } catch { /* jsdom always has it */ }
  setUsageConsent(false)
})

afterEach(() => {
  cleanup()
  setUsageConsent(false)
  try { localStorage.clear() } catch { /* no-op */ }
})

describe('the consent row', () => {
  it('renders a switch that is OFF until the viewer turns it on', () => {
    render(<EntitlementGate tier="team" entitlements={PAID} />)

    const toggle = switchEl()
    expect(toggle).toBeInTheDocument()
    expect(toggle).toHaveAttribute('aria-checked', 'false')
    expect(toggle).toBeEnabled()
    expect(screen.getByText(CONSENT_COPY)).toBeInTheDocument()
  })

  it('carries the exact honest copy, both halves of it', () => {
    render(<EntitlementGate tier="team" entitlements={PAID} />)
    expect(screen.getByText(CONSENT_COPY).textContent).toBe(
      'Share how you use the studio (menu picks, searches). Product events are unaffected.',
    )
  })

  it('toggles on click, and writes through to the store the emitter reads', () => {
    render(<EntitlementGate tier="team" entitlements={PAID} />)

    fireEvent.click(switchEl())
    expect(switchEl()).toHaveAttribute('aria-checked', 'true')
    expect(usageConsentGranted()).toBe(true)
    expect(localStorage.getItem(USAGE_CONSENT_KEY)).toBe('granted')

    fireEvent.click(switchEl())
    expect(switchEl()).toHaveAttribute('aria-checked', 'false')
    expect(usageConsentGranted()).toBe(false)
    expect(localStorage.getItem(USAGE_CONSENT_KEY)).toBe(null)
  })

  it('toggles on Space', () => {
    render(<EntitlementGate tier="team" entitlements={PAID} />)

    fireEvent.keyDown(switchEl(), { key: ' ' })
    expect(switchEl()).toHaveAttribute('aria-checked', 'true')
    fireEvent.keyDown(switchEl(), { key: ' ' })
    expect(switchEl()).toHaveAttribute('aria-checked', 'false')
  })

  it('toggles on Enter', () => {
    render(<EntitlementGate tier="team" entitlements={PAID} />)

    fireEvent.keyDown(switchEl(), { key: 'Enter' })
    expect(switchEl()).toHaveAttribute('aria-checked', 'true')
    expect(usageConsentGranted()).toBe(true)
  })

  it('cancels the key event so a browser cannot also synthesize a click', () => {
    // Without preventDefault a native button would toggle twice per Enter.
    render(<EntitlementGate tier="team" entitlements={PAID} />)
    const handled = fireEvent.keyDown(switchEl(), { key: 'Enter' })
    expect(handled).toBe(false)   // fireEvent returns false when defaultPrevented
  })

  it('ignores keys that are not an activation', () => {
    render(<EntitlementGate tier="team" entitlements={PAID} />)
    fireEvent.keyDown(switchEl(), { key: 'a' })
    fireEvent.keyDown(switchEl(), { key: 'ArrowRight' })
    expect(switchEl()).toHaveAttribute('aria-checked', 'false')
  })

  it('reflects a grant that was already stored for this browser', () => {
    setUsageConsent(true)
    render(<EntitlementGate tier="team" entitlements={PAID} />)
    expect(switchEl()).toHaveAttribute('aria-checked', 'true')
  })
})

describe('when the build-time kill switch is on', () => {
  it('renders the row disabled, OFF, and names the reason in text', () => {
    setUsageConsent(true)   // even a stored yes cannot collect in this build
    render(<EntitlementGate tier="team" entitlements={PAID} telemetryDisabled />)

    const toggle = switchEl()
    expect(toggle).toBeDisabled()
    expect(toggle).toHaveAttribute('aria-checked', 'false')
    expect(screen.getByText('Telemetry is off for this build.')).toBeInTheDocument()
    expect(CONSENT_REASONS.buildDisabled).toBe('Telemetry is off for this build.')
  })

  it('does not show that reason when the build allows telemetry', () => {
    render(<EntitlementGate tier="team" entitlements={PAID} />)
    expect(screen.queryByText(CONSENT_REASONS.buildDisabled)).toBeNull()
  })
})

describe('the entitlement rows above it', () => {
  it('still render exactly as before, with their own state words', () => {
    render(<EntitlementGate tier="team" entitlements={PAID} />)

    const panel = screen.getByRole('region', { name: 'Entitlements' })
    expect(panel).toHaveTextContent('tier team')
    expect(panel).toHaveTextContent('enforced server-side · policy')
    for (const [label, state] of [
      ['Run read-only tools', 'included'],
      ['Run editing tools', 'included'],
      ['Author new tools', 'not in plan'],
      ['Chat with the assistant', 'included'],
    ]) {
      const row = within(panel).getByText(label, { exact: false }).closest('.ent-row')
      expect(row, label).not.toBeNull()
      expect(row).toHaveTextContent(state)
    }
  })

  it('keeps the four entitlement rows out of the consent block', () => {
    // The consent row lives in its own container, so .ent-rows still holds
    // exactly the four read-only entitlement rows and their hairline rhythm.
    const { container } = render(<EntitlementGate tier="team" entitlements={PAID} />)
    expect(container.querySelectorAll('.ent-rows .ent-row')).toHaveLength(4)
    expect(container.querySelectorAll('.ent-consent .ent-row')).toHaveLength(1)
  })

  it('still shows the honest demo note with no entitlements payload', () => {
    render(<EntitlementGate mock />)
    expect(screen.getByText(/demo tier · full access/)).toBeInTheDocument()
    expect(switchEl()).toBeInTheDocument()
  })
})

describe('the switch is styled to the repo\'s one switch rule', () => {
  // styles.css is not applied in jsdom, so these are static assertions on the
  // stylesheet source — the same seam overlayTheme.test.js uses. The first
  // version of this row failed both: its focus ring was a 12%-alpha halo
  // (~1.2:1 on paper, against the 3:1 WCAG 1.4.11 floor) on the Plan panel's
  // ONLY interactive control, and it appealed to a "Leaf standard" that
  // existed nowhere in the tree while contradicting the rule that does.
  const css = readFileSync(resolve(process.cwd(), 'src/styles.css'), 'utf8')
  const jsx = readFileSync(resolve(process.cwd(), 'src/components/EntitlementGate.jsx'), 'utf8')
  const RING = 'box-shadow: 0 0 0 2px var(--card), 0 0 0 4px var(--primary);'

  it('uses the same two-layer keyboard focus ring as every other control', () => {
    // Sliced, not matched: a regex literal carrying braces unbalances the
    // honesty-ladder scanner (check_honesty_ladder.mjs masks comments and
    // strings but not regex literals) and it reports the whole file untrusted.
    const at = css.indexOf('.ent-switch:focus-visible')
    expect(at).toBeGreaterThan(-1)
    const rule = css.slice(at, css.indexOf('}', at) + 1)
    expect(rule).toContain(RING)
    // The invisible halo, by name: a colour-mixed 3px shadow at 12% alpha.
    expect(rule).not.toContain('color-mix')
    // And the rule it now matches, verbatim, so the two cannot drift apart.
    expect(css).toContain(`.toggle:focus-visible, .switch input:focus-visible { ${RING} }`)
  })

  it('turns the knob --on-accent when on, exactly like .toggle:checked', () => {
    expect(css).toContain('.ent-switch.on .ent-switch-knob { background: var(--on-accent);')
    expect(css).toContain('background: var(--on-accent); }')
  })

  it('appeals to no switch standard that does not exist in the tree', () => {
    for (const phrase of ['Leaf standard switch', 'Leaf forms standard', "Leaf standard's"]) {
      expect(css, phrase).not.toContain(phrase)
      expect(jsx, phrase).not.toContain(phrase)
    }
  })
})
