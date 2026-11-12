import { describe, expect, it, vi } from 'vitest'
import { createCatalogController } from './createCatalogController.js'

vi.mock('../../telemetry.js', () => ({ track: vi.fn(), trackErrorShown: vi.fn() }))

// Preserve PR2705's standalone case independently of App's profile context.
describe('standalone named-tool routing', () => {
  it.each([['absent', undefined], ['empty', []]])(
    'keeps confirmation when surface families are %s', async (_label, surfaceFamilyIds) => {
      const decision = { lane: 'run', tool: 'solar-settings', confidence: 0.95 }
      const tool = { name: decision.tool, capabilities: ['drawing.write'] }
      const capability = { ...tool,
        solar: { schema: 'leaf.solar-tool-view.v1', name: decision.tool,
          family: 'stringing', wave: 1, order: 10, maturity: 'production',
          engine: 'server-builtin', adapter: 'local-graph-commit',
          entitlement: 'run_write', interaction: { mode: 'form' }, ledger: [] },
        availability: { entitled: true, implemented: true, input_ready: true,
          engine_ready: true, refusal_reasons: [] },
      }
      const services = {
        getTools: vi.fn(async () => [tool]),
        getCapabilities: vi.fn(async () => ({ families: [
          { family_id: 'stringing', capabilities: [capability] },
        ] })),
        routePrompt: vi.fn(async () => decision),
      }
      const adapters = {
        commitDecision: vi.fn(value => value), dismissDecision: vi.fn(),
        startAgentTurn: vi.fn(),
      }
      const controller = createCatalogController({ services, adapters, context: {
        drawingId: 'drawing-1', entitlements: { entitlements: { run_write: true } },
        ...(surfaceFamilyIds === undefined ? {} : { surfaceFamilyIds }),
      } })
      try {
        await controller.actions.loadTools()
        await controller.actions.loadCatalog()
        const prompt = 'help me plan the next project milestone'
        expect(await controller.actions.dispatch(prompt)).toEqual(decision)
        expect(adapters.commitDecision).toHaveBeenCalledExactlyOnceWith(decision)
        expect(adapters.startAgentTurn).not.toHaveBeenCalled()
        expect(controller.getState()).toMatchObject({ route: decision, routeError: null })
      } finally {
        controller.destroy()
      }
    },
  )
})
