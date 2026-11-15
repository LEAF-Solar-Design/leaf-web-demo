import { afterEach, describe, expect, it, vi } from 'vitest'

vi.mock('../../telemetry.js', () => ({ track: vi.fn(), setTourStep: vi.fn() }))

import { createCatalogController } from './createCatalogController.js'
import { SecretRefusedError } from '../../lib/secretGuardTransport.js'

const inspection = 'Inspect all 94 original project photos and propose gallery assignments. Keep the originals unchanged.'

function setup({ agentDisabled = false, decision = { lane: 'build', confidence: 0.2 } } = {}) {
  const adapters = {
    draftStorage: null,
    openProjectSource: vi.fn(async () => true),
    openAuthor: vi.fn(),
    commitDecision: vi.fn(value => value),
    dismissDecision: vi.fn(),
    startAgentTurn: vi.fn(async () => undefined),
    agentBannerFor: vi.fn(() => ({ kind: 'unreachable' })),
  }
  const controller = createCatalogController({
    services: {
      getTools: vi.fn(async () => []),
      getCapabilities: vi.fn(async () => ({ families: [] })),
      routePrompt: vi.fn(async () => decision),
    },
    adapters,
    context: { mock: false, agentDisabled },
  })
  return { controller, adapters }
}

afterEach(() => vi.clearAllMocks())

describe('photo inspection uses existing agent tools', () => {
  it.each([
    inspection,
    'Please classify the uploaded images by visible content.',
    'Assign project photographs to the existing galleries.',
    'Describe the portfolio photos without inferring ingredients.',
    'Propose photo assignments for the existing galleries.',
  ])('does not open repository authoring for %s', async prompt => {
    const { controller, adapters } = setup()
    controller.actions.setPrompt(prompt)

    await controller.actions.dispatch()

    expect(adapters.openProjectSource).not.toHaveBeenCalled()
    expect(adapters.openAuthor).not.toHaveBeenCalled()
    expect(adapters.startAgentTurn).toHaveBeenCalledOnce()
    expect(adapters.startAgentTurn).toHaveBeenCalledWith(
      prompt, expect.objectContaining({ lane: 'build', confidence: 0.2 }), { allowSecretOnce: false },
    )
    expect(controller.getState().agentMode).toBe('primary')
    expect(controller.getState().prompt).toBe('')
  })

  it.each([
    'Build a responsive Next.js portfolio from project photos.',
    'Create a native iOS app using the project images.',
    'Propose a new portfolio application with repository source.',
  ])('preserves source authoring for %s', async prompt => {
    const { controller, adapters } = setup()
    controller.actions.setPrompt(prompt)
    await controller.actions.dispatch()
    expect(adapters.openProjectSource).toHaveBeenCalledOnce()
    expect(adapters.startAgentTurn).not.toHaveBeenCalled()
  })

  it('preserves existing behavior when the agent is disabled', async () => {
    const { controller, adapters } = setup({ agentDisabled: true })
    controller.actions.setPrompt(inspection)
    await controller.actions.dispatch()
    expect(adapters.openProjectSource).toHaveBeenCalledOnce()
    expect(adapters.startAgentTurn).not.toHaveBeenCalled()
  })

  it('retains an unavailable inspection request without falling into code authoring', async () => {
    const { controller, adapters } = setup()
    adapters.startAgentTurn.mockRejectedValueOnce(new Error('agent unavailable'))
    controller.actions.setPrompt(inspection)
    await controller.actions.dispatch()
    expect(adapters.startAgentTurn).toHaveBeenCalledOnce()
    expect(adapters.openProjectSource).not.toHaveBeenCalled()
    expect(adapters.openAuthor).not.toHaveBeenCalled()
    expect(adapters.commitDecision).not.toHaveBeenCalled()
    expect(controller.getState().agentBanner).toEqual({ kind: 'unreachable' })
    expect(controller.getState().prompt).toBe(inspection)
    expect(controller.getState().agentMode).toBeNull()
  })

  it('preserves the secret refusal without invoking source authoring', async () => {
    const { controller, adapters } = setup()
    const refusal = { id: 'synthetic-refusal', overridable: true }
    adapters.startAgentTurn.mockRejectedValueOnce(new SecretRefusedError(refusal))
    controller.actions.setPrompt(inspection)
    await controller.actions.dispatch()
    expect(controller.getState().secretRefusal).toMatchObject(refusal)
    expect(controller.getState().prompt).toBe(inspection)
    expect(adapters.agentBannerFor).not.toHaveBeenCalled()
    expect(adapters.openProjectSource).not.toHaveBeenCalled()
    expect(adapters.openAuthor).not.toHaveBeenCalled()
  })

  it('keeps an explicitly matched catalog run unchanged', async () => {
    const decision = { lane: 'run', tool: 'count-by-layer', confidence: 0.99 }
    const { controller, adapters } = setup({ decision })
    controller.actions.setPrompt(inspection)
    await controller.actions.dispatch()
    expect(adapters.commitDecision).toHaveBeenCalledWith(decision)
    expect(adapters.startAgentTurn).not.toHaveBeenCalled()
    expect(adapters.openProjectSource).not.toHaveBeenCalled()
  })
})
