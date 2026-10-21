import { useEffect } from 'react'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { getCapabilities } from './api.js'
import { useCatalogController } from './controllers/catalog/useCatalogController.js'
import { createSolarProjectClient } from './solar/solarProjectClient.js'
import { useProjectSolarScope } from './solar/useProjectSolarScope.js'
import SolarFlowRail from './solar/SolarFlowRail.jsx'

vi.mock('./telemetry.js', () => ({ track: vi.fn() }))

const U = n => `${n.toString().repeat(8)}-${n.toString().repeat(4)}-${n.toString().repeat(4)}-${n.toString().repeat(4)}-${n.toString().repeat(12)}`
const identity = { organization_id: U(1), project_id: U(2), drawing_id: U(3) }
const lease = { holder: 'editor', fence: '1', acquired: '2026-01-01T00:00:00Z', expires: '2099-01-01T00:00:00Z' }
const catalog = { families: [{ family_id: 'stringing', capabilities: [{
  name: 'solar-solve-proposal', label: 'Solve', catalog_digest: 'sha256:' + 'a'.repeat(64),
  solar: { schema: 'leaf.solar-tool-view.v1', name: 'solar-solve-proposal', family: 'stringing',
    wave: 1, order: 40, entitlement: 'solve', interaction: { mode: 'form' } },
  availability: { entitled: true, implemented: true, engine_ready: true, input_ready: true,
    runnable: true, admission_checked: true, admission: { status_code: 200, error: null }, refusal_reasons: [] },
}] }] }
const json = data => ({ ok: true, status: 200, json: async () => data })
const pendingCopy = 'Checking Solar steps for the selected project drawing version.'

afterEach(cleanup)

it.each(['take', 'read', 'release'])('W1C10 reopens Solve after a %s checkout rotation with one replacement catalog', async kind => {
  const catalogRequests = []
  let checkout = lease
  const fetchImpl = vi.fn((url, options) => {
    if (url.startsWith('/api/capabilities?')) {
      return new Promise(resolve => catalogRequests.push({ signal: options.signal, resolve }))
    }
    if (url.endsWith('/checkout')) return Promise.resolve(json({ ...identity,
      checkout: options.method === 'DELETE' ? null : checkout,
      ...(options.method === 'POST' ? { acquired: true, checkout_capability: 'lco1.' + 'a'.repeat(64) } : {}),
      ...(options.method === 'DELETE' ? { released: true } : {}),
    }))
    throw new Error(`Unexpected request: ${url}`)
  })
  let client
  let controller
  const createClient = () => { client = createSolarProjectClient({ fetchImpl }); return client }
  const services = { getTools: async () => [], getCapabilities, routePrompt: async () => null,
    get projectSolarClient() { return client } }
  const adapters = { draftStorage: null }
  const intents = []
  const onOpenStep = vi.fn()
  function Host() {
    const solar = useProjectSolarScope({ projectMode: true,
      runContext: { orgId: U(1), projectId: U(2), drawingArtifactId: U(3), drawingId: U(4), drawingVersion: null },
      session: { authenticated: true, actorId: 'hosted-pro-user', generation: 1 } }, { createClient })
    const result = useCatalogController({ services, adapters, context: {
      solarBinding: solar.ready ? solar.scope.binding : null,
      authorityKey: solar.ready ? solar.scope.authorityKey : null, projectIntents: intents,
    } })
    controller = result.controller
    useEffect(() => {
      controller.setContext({ solarBinding: solar.ready ? solar.scope.binding : null,
        authorityKey: solar.ready ? solar.scope.authorityKey : null, projectIntents: intents })
    }, [solar.ready, solar.scope.binding, solar.scope.authorityKey])
    return <SolarFlowRail solarBinding={solar.ready ? solar.scope.binding : null}
      authorityKey={solar.scope.authorityKey} projectClient={solar.projectClient}
      projectCatalog={result.state.projectCatalog} onOpenStep={onOpenStep} />
  }
  const view = render(<Host />)
  try {
    await waitFor(() => expect(catalogRequests).toHaveLength(1))
    await act(async () => { catalogRequests[0].resolve(json(catalog)) })
    await waitFor(() => expect(screen.getByRole('button', { name: /Solve Ready/ })).toBeEnabled())
    // Read/release need a locally owned lease before their rotation. That first
    // acquisition must itself recover, rather than leaving a stale receipt.
    if (kind !== 'take') {
      await act(async () => { expect((await client.acquireCheckout()).ok).toBe(true) })
      await waitFor(() => expect(catalogRequests).toHaveLength(2))
      await act(async () => { catalogRequests[1].resolve(json(catalog)) })
      await waitFor(() => expect(screen.getByRole('button', { name: /Solve Ready/ })).toBeEnabled())
    }
    const receipt = controller.getState().projectCatalog
    const before = catalogRequests.length
    const revision = client.getSnapshot().leaseRevision
    await act(async () => {
      if (kind === 'read') checkout = null
      const outcome = await (kind === 'take' ? client.acquireCheckout()
        : kind === 'read' ? client.readCheckout() : client.releaseCheckout())
      expect(outcome.ok).toBe(true)
    })
    expect(client.getSnapshot().leaseRevision).toBe(revision + 1)
    expect(receipt.isCurrent()).toBe(false)
    expect(screen.getByTestId('project-solar-flow-reason')).toHaveTextContent(pendingCopy)
    expect(screen.getByRole('button', { name: /solar-solve-proposal Blocked/ })).toBeDisabled()
    await waitFor(() => expect(catalogRequests).toHaveLength(before + 1))
    await act(async () => { catalogRequests[before].resolve(json(catalog)) })
    await waitFor(() => expect(screen.queryByText(pendingCopy)).not.toBeInTheDocument())
    const solve = screen.getByRole('button', { name: /Solve Ready/ })
    expect(solve).toBeEnabled()
    fireEvent.click(solve)
    expect(onOpenStep).toHaveBeenCalledWith(expect.objectContaining({ name: 'solar-solve-proposal' }))
    await act(async () => { await client.readCheckout(); await client.readCheckout() })
    expect(catalogRequests).toHaveLength(before + 1)
    expect(controller.getState().projectCatalog.isCurrent()).toBe(true)
    expect(receipt.isCurrent()).toBe(false)
  } finally { view.unmount() }
})
