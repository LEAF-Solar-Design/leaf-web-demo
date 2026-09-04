/**
 * The credential refusal AT THE FUNNEL (standardization slice 8a, round 3).
 *
 * WHAT CHANGED IN ROUND 3. The controller no longer evaluates the guard. It
 * calls `services.routePrompt` (api.nlPrompt) and `adapters.startAgentTurn`
 * (converse.postMessage), and BOTH of those refuse on the wire and throw a
 * typed SecretRefusedError. The controller's remaining jobs are the two this
 * file pins:
 *
 *   1. CATCH that refusal and publish it as `secretRefusal`, so every bar that
 *      reads this controller renders the decision that was actually enforced —
 *      including App's failed-strip Retry chip and its R-key twin, which
 *      dispatch around every composer with whatever sits in the bar.
 *   2. NOT confuse it with an outage. A refusal is not a `routeError` and not
 *      an `agentBanner`; both would tell the user the assistant is unavailable
 *      about a decision that never left the browser.
 *
 * And one absence, which is the round-3 fix itself: there is NO
 * `allowSecretOnce()` action and no armed state here. The override is a
 * parameter on `dispatch`, so a host that short-circuits above this function
 * (App on `running`, ToolCast on its precondition set) strands nothing.
 *
 * `services.routePrompt` is the REAL api.nlPrompt behaviour, reproduced by
 * running the real guard seam, so these rows exercise the actual contract.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'

vi.mock('../../telemetry.js', () => ({ track: vi.fn(), setTourStep: vi.fn() }))

import { createCatalogController } from './createCatalogController.js'
import { SecretRefusedError, guardedText, isSecretRefused } from '../../lib/secretGuardTransport.js'
import {
  SECRET_REASONS,
  SECRET_REASONS_NO_MOUNT,
} from '../../lib/secretPatterns.js'

// Structurally valid, entirely fake — no real credential appears in this repo.
const FAKE_ANTHROPIC = `sk-ant-api03-${'A9_-'.repeat(12)}`
const FAKE_GENERIC = `api_key: ${'x'.repeat(24)}`

const decision = { lane: 'run', tool: 'count-by-layer', confidence: 0.95, rationale: 'ok' }

/**
 * A routePrompt that behaves exactly like the guarded api.nlPrompt: it refuses
 * before it would have hit the network, and honours a per-call authorisation
 * for the overridable shape only.
 */
const guardedRoutePrompt = (mountAvailable = true) => vi.fn(
  async (mock, text, tools, { allowSecretOnce = false } = {}) => {
    const verdict = guardedText(text, { allowSecretOnce, credentialMountAvailable: mountAvailable })
    if (!verdict.ok) throw new SecretRefusedError(verdict.refusal)
    return decision
  },
)

function setup({ context = {}, routePrompt, startAgentTurn } = {}) {
  const services = {
    getTools: vi.fn(async () => []),
    getCapabilities: vi.fn(async () => ({ families: [] })),
    routePrompt: routePrompt || guardedRoutePrompt(),
  }
  const adapters = {
    commitDecision: vi.fn((d) => d),
    startAgentTurn: startAgentTurn || vi.fn(async () => {}),
    dismissDecision: vi.fn(),
    agentBannerFor: vi.fn(() => ({ kind: 'unreachable' })),
  }
  const controller = createCatalogController({
    services,
    adapters,
    context: { mock: false, agentDisabled: true, ...context },
  })
  return { controller, services, adapters }
}

afterEach(() => vi.clearAllMocks())

describe('the funnel renders the verdict the wire produced', () => {
  it('publishes the refusal a named shape raised, and returns nothing', async () => {
    const { controller } = setup()
    controller.actions.setPrompt(FAKE_ANTHROPIC)

    const result = await controller.actions.dispatch()

    expect(result).toBeUndefined()
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
    expect(JSON.stringify(secretRefusal)).not.toContain(FAKE_ANTHROPIC.slice(4))
  })

  // A refusal is not an outage. Publishing it as a routeError would put "we
  // could not reach the router" under a notice that says nothing was sent.
  it('is not a route error and leaves no stale decision strip', async () => {
    const { controller } = setup()
    controller.actions.setPrompt(FAKE_ANTHROPIC)
    await controller.actions.dispatch()

    const state = controller.getState()
    expect(state.routeError).toBeNull()
    expect(state.route).toBeNull()
    expect(state.agentBanner).toBeNull()
    expect(state.routing, 'the bar must not be left spinning').toBe(false)
  })

  it('a real routing failure still reads as a routing failure', async () => {
    const routePrompt = vi.fn(async () => { throw new Error('router unreachable') })
    const { controller } = setup({ routePrompt })
    controller.actions.setPrompt('count entities by layer')
    await controller.actions.dispatch()

    const state = controller.getState()
    expect(state.routeError).toBeTruthy()
    expect(state.secretRefusal).toBeNull()
  })

  // A refusal raised while starting the agent turn must not become an agent
  // banner: the assistant is fine, the client refused.
  it('a refusal from the agent turn surfaces as a refusal, not an agent banner', async () => {
    const startAgentTurn = vi.fn(async (text, hint, { allowSecretOnce = false } = {}) => {
      const verdict = guardedText(text, { allowSecretOnce, credentialMountAvailable: true })
      if (!verdict.ok) throw new SecretRefusedError(verdict.refusal)
    })
    // The router lets it through (a stubbed lane), so the turn is what refuses.
    const routePrompt = vi.fn(async () => ({ lane: 'build', confidence: 0.2 }))
    const { controller, adapters } = setup({
      routePrompt, startAgentTurn, context: { agentDisabled: false },
    })
    controller.actions.setPrompt(FAKE_ANTHROPIC)
    await controller.actions.dispatch()

    expect(controller.getState().secretRefusal.id).toBe('anthropic')
    expect(controller.getState().agentBanner).toBeNull()
    expect(adapters.agentBannerFor).not.toHaveBeenCalled()
  })
})

describe('every re-dispatch path arrives at the same place', () => {
  // App's Retry chip and its R key call dispatch with a MouseEvent / no
  // argument, so the text comes from `state.prompt` — whatever is in the bar at
  // click time. That is the path round 2's review proved was open.
  it.each([
    ['the Retry chip (a MouseEvent)', { type: 'click' }],
    ['the R key (no argument)', undefined],
    ['a string override (the tour, /try)', FAKE_ANTHROPIC],
  ])('%s is refused', async (_label, override) => {
    const { controller } = setup()
    controller.actions.setPrompt(FAKE_ANTHROPIC)

    await controller.actions.dispatch(override)

    expect(controller.getState().secretRefusal.id).toBe('anthropic')
  })
})

describe('THE LATCH SCENARIO, rewritten: there is no arm step (round 3)', () => {
  // ROUND 2'S DEFECT. `actions.allowSecretOnce()` armed a module-level latch;
  // both hosts short-circuit ABOVE dispatch, so a "Send anyway" click whose
  // follow-on dispatch never arrived left it armed, and the NEXT dispatch of
  // ANY text skipped the guard. A probe proved it: arm, then dispatch an AWS
  // key, and the key reached routePrompt.
  //
  // The scenario can no longer be written, and THAT is the fix. These rows say
  // so in both directions: the arming action is gone, and an authorisation
  // that is granted is spent on exactly the call it was granted for.
  it('exposes no arming action at all', () => {
    const { controller } = setup()
    expect(controller.actions.allowSecretOnce).toBeUndefined()
    expect(Object.keys(controller.actions)).not.toContain('allowSecretOnce')
  })

  it('an override authorises the call it is passed to, and only that call', async () => {
    const { controller, services } = setup()
    controller.actions.setPrompt(FAKE_GENERIC)

    await controller.actions.dispatch()
    expect(controller.getState().secretRefusal.overridable).toBe(true)

    await controller.actions.dispatch(undefined, { allowSecretOnce: true })
    expect(services.routePrompt).toHaveBeenCalledTimes(2)
    expect(controller.getState().secretRefusal).toBeNull()

    // The very next dispatch, of a HARD shape, must be refused.
    controller.actions.setPrompt(FAKE_ANTHROPIC)
    await controller.actions.dispatch()
    expect(controller.getState().secretRefusal.id).toBe('anthropic')
  })

  it('a swallowed authorisation cannot reach a later dispatch', async () => {
    // The host short-circuit, reproduced: `running` true, so dispatch returns
    // before it does anything. Under round 2 this armed the latch.
    const { controller, services } = setup({ context: { running: true } })
    controller.actions.setPrompt(FAKE_GENERIC)

    await controller.actions.dispatch(undefined, { allowSecretOnce: true })
    expect(services.routePrompt).not.toHaveBeenCalled()

    // The host recovers; the next dispatch is an ordinary one.
    const live = setup()
    live.controller.actions.setPrompt(FAKE_ANTHROPIC)
    await live.controller.actions.dispatch()
    expect(live.controller.getState().secretRefusal.id).toBe('anthropic')

    // And on the same controller, once it is no longer running.
    const { controller: resumed, services: resumedServices } = setup()
    resumed.actions.setPrompt(FAKE_GENERIC)
    await resumed.actions.dispatch()
    expect(resumedServices.routePrompt).toHaveBeenCalledTimes(1)
    expect(resumed.getState().secretRefusal.overridable).toBe(true)
  })

  it('an override never talks past a named shape, even when passed', async () => {
    const { controller } = setup()
    controller.actions.setPrompt(`${FAKE_GENERIC} and ${FAKE_ANTHROPIC}`)

    await controller.actions.dispatch(undefined, { allowSecretOnce: true })

    const { secretRefusal } = controller.getState()
    expect(secretRefusal.id).toBe('anthropic')
    expect(secretRefusal.overridable).toBe(false)
  })
})

describe('the refusal is retired the moment it stops being true', () => {
  it('an edit clears it', async () => {
    const { controller } = setup()
    controller.actions.setPrompt(FAKE_ANTHROPIC)
    await controller.actions.dispatch()
    expect(controller.getState().secretRefusal).not.toBeNull()

    controller.actions.setPrompt('count entities by layer')
    expect(controller.getState().secretRefusal).toBeNull()
  })

  it('a successful dispatch clears it', async () => {
    const { controller } = setup()
    controller.actions.setPrompt(FAKE_ANTHROPIC)
    await controller.actions.dispatch()

    await controller.actions.dispatch('count entities by layer')
    expect(controller.getState().secretRefusal).toBeNull()
  })

  it('resetTransient clears it', async () => {
    const { controller } = setup()
    controller.actions.setPrompt(FAKE_ANTHROPIC)
    await controller.actions.dispatch()

    controller.actions.resetTransient()
    expect(controller.getState().secretRefusal).toBeNull()
  })

  it('clearSecretRefusal clears it', async () => {
    const { controller } = setup()
    controller.actions.setPrompt(FAKE_ANTHROPIC)
    await controller.actions.dispatch()

    controller.actions.clearSecretRefusal()
    expect(controller.getState().secretRefusal).toBeNull()
  })
})

describe('ordinary prompts are untouched', () => {
  it('routes normally and publishes no refusal', async () => {
    const { controller, services } = setup()
    controller.actions.setPrompt('count the panels within 24in of the roof edge')

    const result = await controller.actions.dispatch()

    expect(result).toEqual(decision)
    expect(services.routePrompt).toHaveBeenCalledTimes(1)
    expect(controller.getState().secretRefusal).toBeNull()
  })
})

describe('the copy the funnel publishes is whatever the wire decided', () => {
  // The controller no longer holds a mount answer of its own — a second copy of
  // that question is a second thing to drift. It publishes the transport's.
  it('names the panel where the transport says it is mounted', async () => {
    const { controller } = setup({ routePrompt: guardedRoutePrompt(true) })
    controller.actions.setPrompt(FAKE_ANTHROPIC)
    await controller.actions.dispatch()

    expect(controller.getState().secretRefusal.reason).toBe(SECRET_REASONS.anthropic)
  })

  it('names no control where the transport says none is mounted', async () => {
    const { controller } = setup({ routePrompt: guardedRoutePrompt(false) })
    controller.actions.setPrompt(FAKE_ANTHROPIC)
    await controller.actions.dispatch()

    const { reason } = controller.getState().secretRefusal
    expect(reason).toBe(SECRET_REASONS_NO_MOUNT.anthropic)
    expect(reason).not.toContain('Claude accounts')
  })

  it('recognises the refusal by its flag, not by prototype identity', () => {
    // A bundle split or a duplicated module copy breaks `instanceof`, and a
    // guard that silently stops being recognised is the failure this avoids.
    const plain = { secretRefused: true, refusal: { id: 'generic' } }
    expect(isSecretRefused(plain)).toBe(true)
    expect(isSecretRefused(new Error('router unreachable'))).toBe(false)
  })
})
