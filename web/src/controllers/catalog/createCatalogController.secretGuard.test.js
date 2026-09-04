/**
 * The credential guard AT THE FUNNEL (standardization slice 8a, fix round 2).
 *
 * WHY THIS FILE EXISTS. Two review rounds killed the same defect twice: the
 * guard was installed per composer while the promise ("credentials never go to
 * the model") was per app, and the composer census was short by one both
 * times. Round 1 missed the assistant reply box. Round 2 missed /try's ToolCast
 * bar AND the two re-dispatch paths — App's failed-strip Retry chip and its
 * R-key twin — which call `catalogActions.dispatch` directly, around every
 * composer, with whatever text sits in the bar at click time.
 *
 * The one thing all of those share is `createCatalogController.dispatch`. So
 * the load-bearing spec here is the NEGATIVE one at that choke point:
 * `services.routePrompt` must not be called, and `adapters.startAgentTurn`
 * must not be called, no matter which path asked.
 *
 * `../../telemetry.js` is mocked so a refusal can be shown to carry a pattern
 * id and nothing else.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('../../telemetry.js', () => ({ track: vi.fn(), setTourStep: vi.fn() }))

import { track } from '../../telemetry.js'
import { createCatalogController } from './createCatalogController.js'
import {
  MOUNTABLE_NEXT_STEP,
  SECRET_REASONS,
  SECRET_REASONS_NO_MOUNT,
  UNMOUNTABLE_NEXT_STEP,
} from '../../lib/secretPatterns.js'

// Structurally valid, entirely fake — no real credential appears in this repo.
const FAKE_ANTHROPIC = `sk-ant-api03-${'A9_-'.repeat(12)}`
const FAKE_GENERIC = `api_key: ${'x'.repeat(24)}`

const decision = { lane: 'run', tool: 'count-by-layer', confidence: 0.95, rationale: 'ok' }

function setup({ context = {}, routePrompt } = {}) {
  const services = {
    getTools: vi.fn(async () => []),
    getCapabilities: vi.fn(async () => ({ families: [] })),
    routePrompt: routePrompt || vi.fn(async () => decision),
  }
  const adapters = {
    commitDecision: vi.fn((d) => d),
    startAgentTurn: vi.fn(async () => {}),
    dismissDecision: vi.fn(),
  }
  const controller = createCatalogController({
    services,
    adapters,
    context: { mock: false, agentDisabled: true, credentialMountAvailable: true, ...context },
  })
  return { controller, services, adapters }
}

afterEach(() => vi.clearAllMocks())

describe('the funnel refuses before anything leaves the client', () => {
  it('a named shape typed into the bar never reaches routePrompt', async () => {
    const { controller, services, adapters } = setup()
    controller.actions.setPrompt(FAKE_ANTHROPIC)

    const result = await controller.actions.dispatch()

    expect(result).toBeUndefined()
    expect(services.routePrompt, 'a credential must never reach the router').not.toHaveBeenCalled()
    expect(adapters.startAgentTurn).not.toHaveBeenCalled()
    const { secretRefusal } = controller.getState()
    expect(secretRefusal.id).toBe('anthropic')
    expect(secretRefusal.overridable).toBe(false)
    expect(secretRefusal.reason).toBe(SECRET_REASONS.anthropic)
  })

  it('carries a mask and a pattern id, never the value', async () => {
    const { controller } = setup()
    controller.actions.setPrompt(FAKE_ANTHROPIC)
    await controller.actions.dispatch()

    const { secretRefusal } = controller.getState()
    expect(secretRefusal.masked).toBe('sk-a••••••••')
    // No eight-character window of the credential survives into the state.
    const serialized = JSON.stringify(secretRefusal)
    for (let i = 0; i + 8 <= FAKE_ANTHROPIC.length; i += 1) {
      expect(serialized).not.toContain(FAKE_ANTHROPIC.slice(i, i + 8))
    }
    expect(track).toHaveBeenCalledWith('prompt.secret_refused', { pattern_id: 'anthropic' })
  })

  // The /try bar (ToolCast.dispatchRequest) hands its text in as a STRING
  // override; the app bar's slash fast-path does the same. Neither goes
  // through PromptBox's local pre-check.
  it('a string override — the /try bar and every slash path — is refused too', async () => {
    const { controller, services } = setup()

    const result = await controller.actions.dispatch(`please run ${FAKE_ANTHROPIC}`)

    expect(result).toBeUndefined()
    expect(services.routePrompt).not.toHaveBeenCalled()
    expect(controller.getState().secretRefusal.id).toBe('anthropic')
  })

  // The exact reachable chain the second review filed: dispatch benign text,
  // paste a credential into the bar while the route is in flight, let the route
  // fail, then hit Retry (or R). Both pass a NON-string, so the text comes from
  // `state.prompt` — the credential.
  it('the Retry chip and the R key re-dispatch the live bar text, guarded', async () => {
    const routePrompt = vi.fn(async () => { throw new Error('route died') })
    const { controller, services } = setup({ routePrompt })

    controller.actions.setPrompt('count the panels')
    await controller.actions.dispatch()
    expect(services.routePrompt).toHaveBeenCalledTimes(1)
    expect(controller.getState().routeError).toBeTruthy()

    // The user pastes a key into the still-enabled bar, then clicks Retry.
    controller.actions.setPrompt(FAKE_ANTHROPIC)
    await controller.actions.dispatch({ type: 'click' }) // the DOM event Retry passes

    expect(services.routePrompt, 'Retry must not re-post a credential').toHaveBeenCalledTimes(1)
    expect(controller.getState().secretRefusal.id).toBe('anthropic')
  })

  it('lets ordinary text straight through, with no refusal left behind', async () => {
    const { controller, services } = setup()
    controller.actions.setPrompt('count the panels on the roofline layer')

    await controller.actions.dispatch()

    expect(services.routePrompt).toHaveBeenCalledTimes(1)
    expect(controller.getState().secretRefusal).toBeNull()
  })
})

describe('the override is a controller action, spent exactly once', () => {
  it('only the fuzzy generic shape is overridable, and it sends on the next dispatch', async () => {
    const { controller, services } = setup()
    controller.actions.setPrompt(FAKE_GENERIC)
    await controller.actions.dispatch()

    expect(controller.getState().secretRefusal.overridable).toBe(true)
    expect(services.routePrompt).not.toHaveBeenCalled()

    controller.actions.allowSecretOnce()
    expect(controller.getState().secretRefusal).toBeNull()
    await controller.actions.dispatch()
    expect(services.routePrompt).toHaveBeenCalledTimes(1)
  })

  it('a second dispatch after an override refuses again', async () => {
    const { controller, services } = setup()
    controller.actions.setPrompt(FAKE_GENERIC)
    await controller.actions.dispatch()
    controller.actions.allowSecretOnce()
    await controller.actions.dispatch()
    expect(services.routePrompt).toHaveBeenCalledTimes(1)

    controller.actions.setPrompt(FAKE_ANTHROPIC)
    await controller.actions.dispatch()

    expect(services.routePrompt).toHaveBeenCalledTimes(1)
    expect(controller.getState().secretRefusal.id).toBe('anthropic')
  })

  // The round-1 fail-open, ported to the funnel: an override armed while the
  // bar cannot dispatch must be SPENT on that no-op, never left latched for
  // the next unrelated Enter.
  it('an override armed while the bar is busy is spent on the no-op, not latched', async () => {
    const { controller, services } = setup({ context: { running: true } })
    controller.actions.setPrompt(FAKE_GENERIC)

    controller.actions.allowSecretOnce()
    await controller.actions.dispatch() // early-returns: running
    expect(services.routePrompt).not.toHaveBeenCalled()

    controller.setContext({ running: false })
    controller.actions.setPrompt(FAKE_ANTHROPIC)
    await controller.actions.dispatch()

    expect(services.routePrompt, 'a spent override must not carry over').not.toHaveBeenCalled()
    expect(controller.getState().secretRefusal.id).toBe('anthropic')
  })

  it('an override armed while a route is already in flight is spent too', async () => {
    let release
    const routePrompt = vi.fn(() => new Promise((resolve) => { release = () => resolve(decision) }))
    const { controller, services } = setup({ routePrompt })

    controller.actions.setPrompt('count the panels')
    const inFlight = controller.actions.dispatch()
    expect(controller.getState().routing).toBe(true)

    controller.actions.allowSecretOnce()
    await controller.actions.dispatch() // early-returns: routing
    release()
    await inFlight

    controller.actions.setPrompt(FAKE_ANTHROPIC)
    await controller.actions.dispatch()
    expect(services.routePrompt).toHaveBeenCalledTimes(1)
    expect(controller.getState().secretRefusal.id).toBe('anthropic')
  })
})

describe('the refusal retires with the text it was about', () => {
  it('an edit clears it', async () => {
    const { controller } = setup()
    controller.actions.setPrompt(FAKE_ANTHROPIC)
    await controller.actions.dispatch()
    expect(controller.getState().secretRefusal).not.toBeNull()

    controller.actions.setPrompt('never mind')
    expect(controller.getState().secretRefusal).toBeNull()
  })

  it('clearSecretRefusal and resetTransient clear it', async () => {
    const { controller } = setup()
    controller.actions.setPrompt(FAKE_ANTHROPIC)
    await controller.actions.dispatch()
    controller.actions.clearSecretRefusal()
    expect(controller.getState().secretRefusal).toBeNull()

    await controller.actions.dispatch()
    expect(controller.getState().secretRefusal).not.toBeNull()
    controller.actions.resetTransient()
    expect(controller.getState().secretRefusal).toBeNull()
  })
})

describe('the copy is honest about the mode it is shown in', () => {
  it('points at Claude accounts only where that panel is mounted', async () => {
    const { controller } = setup({ context: { credentialMountAvailable: true } })
    controller.actions.setPrompt(FAKE_ANTHROPIC)
    await controller.actions.dispatch()
    expect(controller.getState().secretRefusal.reason).toBe(SECRET_REASONS.anthropic)
    expect(controller.getState().secretRefusal.reason).toContain(MOUNTABLE_NEXT_STEP)
  })

  // Mock mode renders no ClaudeAccountPanel at all (App.jsx `{!mock && ...}`;
  // the component itself returns null under mock), so telling a mock-mode user
  // to mount it there names a control that is not on screen.
  it('names no surface in mock mode, where the panel is not rendered', async () => {
    const { controller } = setup({ context: { mock: true, credentialMountAvailable: false } })
    controller.actions.setPrompt(FAKE_ANTHROPIC)
    await controller.actions.dispatch()

    const { reason } = controller.getState().secretRefusal
    expect(reason).toBe(SECRET_REASONS_NO_MOUNT.anthropic)
    expect(reason).toContain(UNMOUNTABLE_NEXT_STEP)
    expect(reason).not.toContain('Claude accounts')
  })

  it('defaults to naming no surface when the caller never answered', async () => {
    const services = {
      getTools: vi.fn(async () => []),
      getCapabilities: vi.fn(async () => ({ families: [] })),
      routePrompt: vi.fn(async () => decision),
    }
    const controller = createCatalogController({ services, adapters: {}, context: {} })
    controller.actions.setPrompt(FAKE_ANTHROPIC)
    await controller.actions.dispatch()

    expect(controller.getState().secretRefusal.reason).toBe(SECRET_REASONS_NO_MOUNT.anthropic)
  })
})
