import { describe, expect, it, vi } from 'vitest'

vi.mock('../../telemetry.js', () => ({ track: vi.fn(), setTourStep: vi.fn() }))

import { createCatalogController } from './createCatalogController.js'

const run = { lane: 'run', tool: 'count-modules', confidence: 0.97 }
const build = { lane: 'build', confidence: 0.9 }

function setup(decision, context, families = []) {
  const adapters = {
    draftStorage: null,
    startAgentTurn: vi.fn(),
    commitDecision: vi.fn(value => value),
    openProjectSource: vi.fn(async () => true),
    openAuthor: vi.fn(),
    agentBannerFor: vi.fn(() => ({ kind: 'unreachable' })),
  }
  const routePrompt = vi.fn(async () => decision)
  const controller = createCatalogController({
    services: {
      getTools: vi.fn(async () => []),
      getCapabilities: vi.fn(async () => ({ families })),
      routePrompt,
    },
    adapters,
    context: { agentDisabled: false },
  })
  controller.setContext(context)
  return { controller, adapters, routePrompt }
}

describe('plan mode dispatch', () => {
  it.each([0.97, 0.65])('sends a run at confidence %s to the primary assistant', async confidence => {
    const decision = { ...run, confidence }
    const { controller, adapters } = setup(decision, { planMode: true })
    const prompt = 'count the panels'
    controller.actions.setPrompt(prompt)
    await controller.actions.dispatch(undefined, { allowSecretOnce: true })

    expect(adapters.startAgentTurn).toHaveBeenCalledOnce()
    expect(adapters.startAgentTurn).toHaveBeenCalledWith(
      prompt, { ...decision, rationale: null }, { allowSecretOnce: true },
    )
    expect(adapters.commitDecision).not.toHaveBeenCalled()
    expect(controller.getState()).toMatchObject({ agentMode: 'primary', prompt: '', route: null })
  })

  it('sends a build request to the assistant without opening project source', async () => {
    const { controller, adapters } = setup(build, { planMode: true })
    await controller.actions.dispatch('write a Branch tool')
    expect(adapters.startAgentTurn).toHaveBeenCalledOnce()
    expect(adapters.openProjectSource).not.toHaveBeenCalled()
    expect(adapters.commitDecision).not.toHaveBeenCalled()
    expect(controller.getState().agentMode).toBe('primary')
  })

  it('commits an explicit slash command', async () => {
    const { controller, adapters, routePrompt } = setup(run, { planMode: true })
    controller.actions.upsertTool({ name: run.tool })
    await controller.actions.dispatch('/count-modules')
    expect(adapters.commitDecision).toHaveBeenCalledWith(expect.objectContaining({ tool: run.tool, slash: true }))
    expect(routePrompt).not.toHaveBeenCalled()
    expect(adapters.startAgentTurn).not.toHaveBeenCalled()
  })

  it('shows the agent banner and retains the prompt after a rejected turn', async () => {
    const { controller, adapters } = setup(build, { planMode: true })
    const prompt = 'write a Branch tool'
    const error = new Error('agent unavailable')
    adapters.startAgentTurn.mockRejectedValueOnce(error)
    controller.actions.setPrompt(prompt)
    await controller.actions.dispatch()
    expect(adapters.agentBannerFor).toHaveBeenCalledWith(error)
    expect(controller.getState()).toMatchObject({ prompt, route: null, agentBanner: { kind: 'unreachable' } })
    expect(adapters.commitDecision).not.toHaveBeenCalled()
    expect(adapters.openAuthor).not.toHaveBeenCalled()
    expect(adapters.openProjectSource).not.toHaveBeenCalled()
  })

  it.each([{}, { planMode: false }, { planMode: true, agentDisabled: true }])(
    'retains chip dispatch with context %j', async context => {
      const { controller, adapters } = setup(run, context)
      await controller.actions.dispatch('count the panels')
      expect(adapters.commitDecision).toHaveBeenCalledWith(run)
      expect(adapters.startAgentTurn).not.toHaveBeenCalled()
      expect(controller.getState().route).toEqual(run)
    },
  )
})

describe('plan mode with a refused capability', () => {
  const tool = 'solar-size-strings'
  const prompt = 'size the strings'
  const reason = 'entitlement_required'
  const decision = { ...run, tool }
  const capability = {
    name: tool,
    capabilities: ['drawing.write'],
    solar: {
      schema: 'leaf.solar-tool-view.v1', name: tool, family: 'stringing', wave: 1,
      order: 20, maturity: 'production', engine: 'cloud-service', adapter: null,
      entitlement: 'run_write', interaction: { mode: 'form' }, ledger: ['string-sizer'],
    },
    availability: {
      entitled: false, implemented: true, engine_ready: false, input_ready: true,
      refusal_reasons: [reason],
    },
  }

  async function setupRefused(context) {
    const harness = setup(decision, context, [{ family_id: 'stringing', capabilities: [capability] }])
    await harness.controller.actions.loadCatalog()
    harness.controller.actions.setPrompt(prompt)
    return harness
  }

  it.each(['resolved', 'rejected'])('passes the refusal reason to a %s agent turn', async outcome => {
    const { controller, adapters } = await setupRefused({ planMode: true })
    const error = new Error('agent unavailable')
    if (outcome === 'rejected') adapters.startAgentTurn.mockRejectedValueOnce(error)
    await controller.actions.dispatch(undefined, { allowSecretOnce: true })

    expect(adapters.startAgentTurn).toHaveBeenCalledExactlyOnceWith(
      prompt, { ...decision, rationale: `${tool} is not ready here: ${reason}` }, { allowSecretOnce: true },
    )
    expect(adapters.commitDecision).not.toHaveBeenCalled()
    expect(adapters.openProjectSource).not.toHaveBeenCalled()
    expect(adapters.openAuthor).not.toHaveBeenCalled()
    expect(controller.getState()).toMatchObject({ route: null, routeError: null })
    if (outcome === 'rejected') {
      expect(adapters.agentBannerFor).toHaveBeenCalledWith(error)
      expect(controller.getState()).toMatchObject({ prompt, agentBanner: { kind: 'unreachable' } })
    } else {
      expect(controller.getState()).toMatchObject({ prompt: '', agentMode: 'primary' })
    }
  })

  it.each([{ planMode: false }, { planMode: true, agentDisabled: true }])(
    'shows the catalog refusal directly with context %j', async context => {
      const { controller, adapters } = await setupRefused(context)
      await controller.actions.dispatch()
      expect(controller.getState()).toMatchObject({ route: null, routeError: reason })
      expect(adapters.startAgentTurn).not.toHaveBeenCalled()
      expect(adapters.commitDecision).not.toHaveBeenCalled()
    },
  )
})
