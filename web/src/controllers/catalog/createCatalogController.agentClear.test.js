import { afterEach, describe, expect, it, vi } from 'vitest'

vi.mock('../../telemetry.js', () => ({ track: vi.fn(), setTourStep: vi.fn() }))

import { createCatalogController } from './createCatalogController.js'
import { SecretRefusedError } from '../../lib/secretGuardTransport.js'

const prompt = 'count entities by layer'
const primary = { lane: 'build', confidence: 0.2 }
const race = { lane: 'run', tool: 'count-by-layer', confidence: 0.65 }

function setup(decision) {
  const services = {
    getTools: vi.fn(async () => []),
    getCapabilities: vi.fn(async () => ({ families: [] })),
    routePrompt: vi.fn(async () => decision),
  }
  const adapters = {
    draftStorage: null,
    commitDecision: vi.fn((d) => d),
    dismissDecision: vi.fn(),
    startAgentTurn: vi.fn(async () => undefined),
    agentBannerFor: vi.fn(() => ({ kind: 'unreachable' })),
  }
  const controller = createCatalogController({
    services, adapters, context: { mock: false, agentDisabled: false },
  })
  controller.actions.setPrompt(prompt)
  return { controller, adapters }
}

afterEach(() => vi.clearAllMocks())

describe('command bar prompt after dispatch', () => {
  it.each([
    ['primary', primary],
    ['race', race],
  ])('clears the prompt when a %s agent turn is accepted', async (mode, decision) => {
    const { controller, adapters } = setup(decision)

    await controller.actions.dispatch()

    expect(adapters.startAgentTurn).toHaveBeenCalledWith(
      prompt,
      expect.objectContaining({ lane: decision.lane, confidence: decision.confidence }),
      { allowSecretOnce: false },
    )
    expect(controller.getState().prompt).toBe('')
    expect(controller.getState().agentMode).toBe(mode)
  })

  it.each([
    ['primary', primary],
    ['race', race],
  ])('keeps the draft when a %s agent turn fails to start', async (_mode, decision) => {
    const { controller, adapters } = setup(decision)
    adapters.startAgentTurn.mockRejectedValueOnce(new Error('agent unavailable'))

    await controller.actions.dispatch()

    expect(adapters.startAgentTurn).toHaveBeenCalledOnce()
    expect(controller.getState().prompt).toBe(prompt)
    expect(controller.getState().agentMode).toBeNull()
    expect(controller.getState().agentBanner).toEqual({ kind: 'unreachable' })
  })

  it.each([
    ['primary', primary],
    ['race', race],
  ])('keeps the draft when a %s agent turn is refused', async (_mode, decision) => {
    const { controller, adapters } = setup(decision)
    const refusal = { id: 'generic', overridable: true }
    adapters.startAgentTurn.mockRejectedValueOnce(new SecretRefusedError(refusal))

    await controller.actions.dispatch()

    expect(controller.getState().prompt).toBe(prompt)
    expect(controller.getState().agentMode).toBeNull()
    expect(controller.getState().secretRefusal).toMatchObject(refusal)
    expect(adapters.agentBannerFor).not.toHaveBeenCalled()
  })

  it('keeps the existing prompt behavior for a tool run without an agent turn', async () => {
    const decision = { ...race, confidence: 0.95 }
    const { controller, adapters } = setup(decision)

    await controller.actions.dispatch()

    expect(adapters.commitDecision).toHaveBeenCalledWith(decision)
    expect(adapters.startAgentTurn).not.toHaveBeenCalled()
    expect(controller.getState().route).toEqual(decision)
    expect(controller.getState().prompt).toBe(prompt)
    expect(controller.getState().agentMode).toBeNull()
  })
})
