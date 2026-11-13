import { readFileSync } from 'node:fs'
import { parse } from '@babel/parser'
import { describe, expect, it, vi } from 'vitest'
import { PRODUCT_SURFACES } from './site/productSurfaces.js'
import { createCatalogController } from './controllers/catalog/createCatalogController.js'

vi.mock('./telemetry.js', () => ({ track: vi.fn() }))

// Evaluate the exported pure function itself, without importing App's unrelated
// browser integrations or mounting the console. The Solar scope tests use the
// same AST extraction approach for App's production declarations.
const source = readFileSync(`${process.cwd()}/src/App.jsx`, 'utf8').replace(/\r\n/g, '\n')
const tree = parse(source, { sourceType: 'module', plugins: ['jsx'] })
const declaration = tree.program.body.find(node => node.type === 'ExportNamedDeclaration'
  && node.declaration?.id?.name === 'catalogContextForSurface').declaration
const catalogContextForSurface = Function(`return (${source.slice(declaration.start, declaration.end)})`)()
const surface = id => PRODUCT_SURFACES.find(row => row.id === id)

describe('App catalog surface context', () => {
  it.each(['ops', 'lab', 'sheets'])('preserves %s empty families', id => {
    const manifest = surface(id)
    expect(manifest.familyIds).toEqual([])
    expect(catalogContextForSurface(manifest)).toEqual({ surfaceFamilyIds: [] })
    expect(catalogContextForSurface(manifest).surfaceFamilyIds).toBe(manifest.familyIds)
  })

  it.each(['solar', 'browser', 'cad', 'ios'])('passes through %s manifest families', id => {
    const manifest = surface(id)
    expect(catalogContextForSurface(manifest).surfaceFamilyIds).toBe(manifest.familyIds)
  })

  it('keeps null distinct from empty families, including a Solar manifest with null families', () => {
    const manifest = Object.freeze({ ...surface('solar'), familyIds: null })
    expect(catalogContextForSurface(manifest)).toEqual({ surfaceFamilyIds: null })
    expect(catalogContextForSurface(surface('ops'))).toEqual({ surfaceFamilyIds: [] })
  })

  it.each([undefined, null, {}])('clears previous families for an unknown surface %j', manifest => {
    const previous = Object.freeze({ surfaceFamilyIds: [], projectId: 'project-a', orgId: 'org-a' })
    expect(catalogContextForSurface(manifest, previous)).toEqual({
      ...previous, surfaceFamilyIds: null,
    })
    expect(previous.surfaceFamilyIds).toEqual([])
    expect(catalogContextForSurface(surface('unknown'))).toEqual({ surfaceFamilyIds: null })
  })

  it('follows Ops to Solar to Ops without changing project authority or mutating context', () => {
    const binding = Object.freeze({ project_id: 'project-a', organization_id: 'org-a' })
    const previous = Object.freeze({ solarBinding: binding, authorityKey: 'session-a', surfaceFamilyIds: null })
    const ops = Object.freeze(catalogContextForSurface(surface('ops'), previous))
    const solar = Object.freeze(catalogContextForSurface(surface('solar'), ops))
    const returned = catalogContextForSurface(surface('ops'), solar)
    expect(ops).toEqual({ ...previous, surfaceFamilyIds: [] })
    // Solar declares concrete families in the current manifest; keep those
    // rather than replacing them with the unrestricted null used by CAD/iOS.
    expect(solar.surfaceFamilyIds).toBe(surface('solar').familyIds)
    expect(returned).toEqual(ops)
    expect(returned.solarBinding).toBe(binding)
    expect(previous.surfaceFamilyIds).toBeNull()
  })
})

describe('project requests on surfaces without tool families', () => {
  it('sends a high-confidence named Solar route to conversation and retains tool admission elsewhere', async () => {
    const prompt = 'Summarize the project datasets'
    const decision = { lane: 'run', tool: 'solar-settings', params: {}, confidence: 0.99 }
    const services = {
      getTools: vi.fn(async () => [{ name: decision.tool }]),
      getCapabilities: vi.fn(async () => ({ families: [] })),
      routePrompt: vi.fn(async () => decision),
    }
    const adapters = {
      draftStorage: null,
      commitDecision: vi.fn(value => value),
      dismissDecision: vi.fn(),
      startAgentTurn: vi.fn(async () => undefined),
    }
    const controller = createCatalogController({ services, adapters,
      context: catalogContextForSurface(surface('ops'), {
        mock: false, agentDisabled: false, solarBinding: null, projectIntents: [],
        catalogDiscovery: { projectId: 'project-a', orgId: 'org-a' },
        entitlements: { entitlements: { converse: true } },
      }),
    })
    try {
      await controller.actions.loadTools()
      for (const id of ['ops', 'lab', 'sheets']) {
        controller.setContext(catalogContextForSurface(surface(id)))
        adapters.startAgentTurn.mockClear()
        controller.actions.setPrompt(prompt)
        expect(await controller.actions.dispatch()).toEqual(decision)
        expect(adapters.startAgentTurn).toHaveBeenCalledExactlyOnceWith(prompt, {
          lane: 'run', tool: decision.tool, confidence: 0.99, rationale: null,
        }, { allowSecretOnce: false })
        expect(adapters.commitDecision).not.toHaveBeenCalled()
        expect(controller.getState()).toMatchObject({ route: null, routeError: null,
          agentMode: 'primary', routing: false, prompt: '' })
      }

      // Neither Solar's declared families nor an unrestricted null list may
      // bypass project tool admission. Explicit slash commands also retain it.
      adapters.startAgentTurn.mockClear()
      for (const manifest of [surface('solar'), { familyIds: null }]) {
        controller.setContext(catalogContextForSurface(manifest))
        await controller.actions.dispatch(prompt)
        expect(controller.getState()).toMatchObject({ route: null, routeError: 'project_request_required' })
      }
      controller.setContext(catalogContextForSurface(surface('ops')))
      await controller.actions.dispatch(`/${decision.tool}`)
      expect(controller.getState()).toMatchObject({ route: null, routeError: 'project_request_required' })
      expect(adapters.startAgentTurn).not.toHaveBeenCalled()
      expect(adapters.commitDecision).not.toHaveBeenCalled()

      await controller.actions.dispatch(prompt)
      expect(adapters.startAgentTurn).toHaveBeenCalledOnce()
      expect(controller.getState()).toMatchObject({ route: null, routeError: null, agentMode: 'primary' })

      // Leaving the project preserves standalone named-tool confirmation.
      adapters.startAgentTurn.mockClear()
      controller.setContext({ solarBinding: undefined, catalogDiscovery: null })
      await controller.actions.dispatch(prompt)
      expect(adapters.startAgentTurn).not.toHaveBeenCalled()
      expect(adapters.commitDecision).toHaveBeenCalledExactlyOnceWith(decision)
      expect(controller.getState()).toMatchObject({ route: decision, routeError: null })
    } finally {
      controller.destroy()
    }
  })
})
