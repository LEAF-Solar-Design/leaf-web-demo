/**
 * The stale-lease rail, in both of its states.
 *
 * Observed on production 2026-09-01: a lock taken ~4h earlier by a session that
 * closed its tab without releasing rendered as
 *
 *     Editing locked by sess-72d58f4d… until ~-4 h — read tools still run
 *
 * an interval running BACKWARDS. `fmtUntil` subtracted `now` from a past
 * `expires` and formatted the negative result, because every branch tested
 * `Math.abs(mins)` — so a lapsed lease got the same "until …" clause a live one
 * gets, with a minus sign in it. The server publishes the checkout record
 * verbatim whether or not it has elapsed (routers/drawings.py `_checkout_view`),
 * so this is reachable for the whole time between a forgotten lease expiring and
 * anyone taking the lock: hours, on any drawing.
 *
 * Both states are pinned, in the bundleFence spirit: asserting only that the
 * expired case says something is satisfied by a component that says it in every
 * case and has stopped counting down at all. So the live lease must still show
 * its horizon, and the elapsed one must show no horizon — the same component,
 * the same props, one clock apart.
 */
import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import CheckoutChip from './CheckoutChip.jsx'
import CheckoutControls from './CheckoutControls.jsx'
import { normalizeCheckout, lockState } from '../checkoutIdentity.js'
import createCheckoutController from '../controllers/checkout/createCheckoutController.js'

afterEach(cleanup)

const HOLDER = 'sess-72d58f4d-36af-421b-a164-a98394156cfa'
const at = (offsetMs) => new Date(Date.now() + offsetMs).toISOString()
const leaseEndingIn = (offsetMs) => ({
  holder: HOLDER,
  acquired: at(-4 * 3600e3),
  expires: at(offsetMs),
})

describe('CheckoutChip expiry horizon', () => {
  it('names the horizon while the lease is still live', () => {
    render(<CheckoutChip checkout={leaseEndingIn(4 * 3600e3)} />)
    const chip = screen.getByRole('status')
    expect(chip.textContent).toContain(`Editing locked by ${HOLDER}`)
    expect(chip.textContent).toContain('until ~4 h')
  })

  it('names no horizon once the lease has elapsed, and never a negative one', () => {
    // The production case: expired four hours ago.
    render(<CheckoutChip checkout={leaseEndingIn(-4 * 3600e3)} />)
    const chip = screen.getByRole('status')
    // The holder is still named — the lock is still what suppresses writes.
    expect(chip.textContent).toContain(`Editing locked by ${HOLDER}`)
    expect(chip.textContent).not.toContain('until')
    // The holder id itself carries hyphen-digit pairs, so the assertion is on
    // the rendered horizon token, not on "a minus sign anywhere in the line".
    expect(chip.textContent).not.toContain('~-')
    expect(chip.querySelector('.t-rel')).toBe(null)
  })

  it('names no horizon for an expires it cannot parse', () => {
    // Not hypothetical: `looksStale` treats an unparseable expiry as elapsed
    // precisely because such records exist, so echoing the raw string made the
    // rail say "until banana" and "this lease looks expired" at once.
    render(<CheckoutChip checkout={{ holder: HOLDER, expires: 'banana' }} />)
    const chip = screen.getByRole('status')
    expect(chip.textContent).toContain(`Editing locked by ${HOLDER}`)
    expect(chip.textContent).not.toContain('until')
    expect(chip.textContent).not.toContain('banana')
    expect(chip.querySelector('.t-rel')).toBe(null)
  })

  it('names no horizon when the record carries no expires at all', () => {
    render(<CheckoutChip checkout={{ holder: HOLDER }} />)
    expect(screen.getByRole('status').textContent).not.toContain('until')
  })

  it('rounds a lease with seconds left UP to a live horizon, not down to elapsed', () => {
    // Boundary between the two states above: 20s left is live, and rounding
    // minutes before testing the sign would call it expired.
    render(<CheckoutChip checkout={leaseEndingIn(20_000)} />)
    expect(screen.getByRole('status').textContent).toContain('until ~1 m')
  })
})

describe('CheckoutControls over an elapsed lease', () => {
  // The rail as a whole: the elapsed lease is worded once, and the action that
  // clears it is offered. Hiding the Take is what leaves a wedge no user action
  // can clear, so it is asserted here rather than assumed.
  const props = (checkout) => {
    const lock = lockState({ mock: false, checkout, unknown: false, ownHolder: 'sess-me' })
    return {
      lockedByOther: lock.otherHeld,
      staleByOther: lock.stale,
      legacyByOther: lock.legacy,
      canTake: lock.canTake,
      onTake: () => {},
      onRelease: () => {},
    }
  }

  it('says the lease looks expired once, with no negative horizon, and offers a Take', () => {
    const { container } = render(<CheckoutControls {...props(leaseEndingIn(-4 * 3600e3))} />)
    const rail = container.querySelector('.checkout-controls')
    expect(rail.textContent).toContain('this lease looks expired')
    expect(rail.textContent).not.toContain('until')
    expect(rail.textContent).not.toContain('~-')
    expect(screen.getByRole('button', { name: 'Take edit lock' })).toBeTruthy()
  })

  it('shows the horizon and no expiry note while the lease is live', () => {
    const { container } = render(<CheckoutControls {...props(leaseEndingIn(4 * 3600e3))} />)
    const rail = container.querySelector('.checkout-controls')
    expect(rail.textContent).toContain('until ~4 h')
    expect(rail.textContent).not.toContain('lease looks expired')
  })
})

describe('a holderless checkout is not a lock', () => {
  // GET /versions answers `checkout: {}` for a drawing nobody holds, and `{}` is
  // truthy: stored as-is it is an object that every reader must re-reject.
  // Normalized at the boundary, "no holder in the live answer" IS null, so a
  // refresh that comes back holderless replaces a cached holder instead of
  // layering an empty record over it.
  it('normalizes {} and a holderless record to null', () => {
    expect(normalizeCheckout({})).toBe(null)
    expect(normalizeCheckout({ holder: '   ' })).toBe(null)
    expect(normalizeCheckout(undefined)).toBe(null)
    expect(normalizeCheckout(null)).toBe(null)
  })

  it('keeps a record that does name a holder', () => {
    const live = leaseEndingIn(4 * 3600e3)
    expect(normalizeCheckout(live)).toBe(live)
    // Elapsed is still a lock — expiry is the SERVER's call, not the browser
    // clock's, and the client fails closed until a Take is adjudicated.
    const elapsed = leaseEndingIn(-4 * 3600e3)
    expect(normalizeCheckout(elapsed)).toBe(elapsed)
  })

  it('renders nothing for a holderless checkout', () => {
    const { container } = render(<CheckoutChip checkout={{}} />)
    expect(container.textContent).toBe('')
  })
})

describe('the live /versions answer is what the rail derives from', () => {
  // The controller is the one place the server's answer enters state, so this
  // is where "the server no longer reports a holder" has to become "no lock".
  const controllerOver = (answers) => {
    let call = 0
    return createCheckoutController({
      drawingId: 'demo',
      holder: 'sess-me',
      services: {
        loadVersions: async () => answers[Math.min(call++, answers.length - 1)],
        take: async () => null,
        release: async () => null,
      },
    })
  }

  it('drops a held lock when the next answer names no holder', async () => {
    const held = leaseEndingIn(4 * 3600e3)
    const controller = controllerOver([{ checkout: held }, { checkout: {} }])
    await controller.refresh()
    expect(controller.getSnapshot().lockedByOther).toEqual(held)

    // The lease lapsed and the server released it: `checkout: {}`.
    await controller.refresh()
    const after = controller.getSnapshot()
    expect(after.checkout).toBe(null)
    expect(after.lockedByOther).toBe(null)
    expect(after.writeLocked).toBe(false)
    expect(after.unknown).toBe(false)
  })

  it('keeps failing closed while a lock the server still reports has elapsed', async () => {
    // Expiry is the server's call, never the browser clock's: an elapsed record
    // the server still publishes stays a lock, with a Take offered for it.
    const elapsed = leaseEndingIn(-4 * 3600e3)
    const controller = controllerOver([{ checkout: elapsed }])
    await controller.refresh()
    const state = controller.getSnapshot()
    expect(state.lockedByOther).toEqual(elapsed)
    expect(state.writeLocked).toBe(true)
    expect(state.staleByOther).toBe(true)
    expect(state.canTake).toBe(true)
  })
})
