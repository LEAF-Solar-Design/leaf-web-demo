import React, { useCallback } from 'react'
import { act, cleanup, fireEvent, render, renderHook, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { readFileSync } from 'node:fs'
import { parse } from '@babel/parser'
import { getTools, runToolAsync } from './api.js'
import { readActiveScope } from './auth/activeScope.js'
import ReferenceMaterialIntake from './workspace/ReferenceMaterialIntake.jsx'

vi.mock('./telemetry.js', async (importOriginal) => ({
  ...(await importOriginal()), track: vi.fn(), trackErrorShown: vi.fn(),
}))

// Execute the production App callback without mounting unrelated editor and
// Solar controllers. Material intake, catalog reads, registration and the run
// transport below are production code, all the way to the fetch boundary.
const source = readFileSync(`${process.cwd()}/src/App.jsx`, 'utf8')
const tree = parse(source, { sourceType: 'module', plugins: ['jsx'] })
const callbacks = []
function walk(node) {
  if (!node || typeof node.type !== 'string') return
  if (node.type === 'VariableDeclarator' && node.id.name === 'onRunArloReference') callbacks.push(node.init)
  for (const value of Object.values(node)) {
    if (Array.isArray(value)) value.forEach(walk)
    else if (value && typeof value.type === 'string') walk(value)
  }
}
walk(tree)
if (callbacks.length !== 1) throw new Error('Expected one production ARLO reference callback')
const useAppArloReference = Function('useCallback', 'getTools', 'runToolAsync', 'readActiveScope', `
  return function useAppArloReference(input) {
    const { mock, signedIn, running, workspaceController, runJob, rehydrate } = input
    return (${source.slice(callbacks[0].start, callbacks[0].end)})
  }
`)(useCallback, getTools, runToolAsync, readActiveScope)

const O = '11111111-1111-4111-8111-111111111111'
const P = '22222222-2222-4222-8222-222222222222'
const M = '33333333-3333-4333-8333-333333333333'
const V = '44444444-4444-4444-8444-444444444444'
const J = '55555555-5555-4555-8555-555555555555'
const OTHER = '66666666-6666-4666-8666-666666666666'
const RUN_KEY = '77777777-7777-4777-8777-777777777777'
const digest = 'sha256:' + 'c'.repeat(64)
const tool = { name: 'arlo-design', catalog_digest: digest }
const material = { material_id: M, name: 'request.json', media_type: 'application/json',
  byte_size: 128, sha256: 'a'.repeat(64), created_at: '2026-01-01T00:00:00Z' }
const registered = { organization_id: O, project_id: P, material_id: M,
  original_sha256: material.sha256, input_version_id: V, input_sha256: 'b'.repeat(64),
  parent_id: null, original_id: V,
  request: { contract: 'arlo_design_request_v1', seed: 8,
    requirements: { diameter_m: 2, clearance_m: 3, max_support_span_m: 5, labor_rate: 4 },
    budget: { timeout_seconds: 45, beam_width: 3 } } }
const reply = (data, status = 200) => ({ ok: status >= 200 && status < 300, status,
  headers: { get: () => null }, json: async () => data, clone: () => reply(data, status) })
const deferred = () => {
  let resolve
  const promise = new Promise(yes => { resolve = yes })
  return { promise, resolve }
}
function account(subject = 'reference-owner') {
  const token = `fixture.${btoa(JSON.stringify({ iss: 'fixture-issuer', sub: subject }))}.fixture`
  localStorage.setItem('leaf.jwt', token)
  localStorage.setItem('leaf.org_id', O)
  return token
}

beforeEach(() => {
  localStorage.clear()
  account()
  vi.stubGlobal('EventSource', undefined)
  vi.spyOn(crypto, 'randomUUID').mockReturnValue(RUN_KEY)
})
afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  localStorage.clear()
})

function setup({ catalog = async () => reply({ tools: [
  { name: 'other-tool', catalog_digest: 'sha256:' + 'd'.repeat(64) }, tool,
] }), submission = () => reply({ job_id: J, status: 'submitted' }, 202) } = {}) {
  let workspace = { openProjectId: P, workspace: { project: { project_id: P, org_id: O },
    drawing_versions: [], jobs: [] } }
  const fetchImpl = vi.fn(async (url, init = {}) => {
    const path = new URL(String(url), 'https://studio.test').pathname
    if (path === `/api/projects/${P}/reference-files`) return reply({ materials: [material] })
    if (path === `/api/projects/${P}/reference-files/${M}/arlo-input`) return reply(registered)
    if (path === `/api/projects/${P}/arlo-inputs/${V}`) return reply(registered)
    if (path === '/api/tools') return catalog()
    if (path === '/api/run') return submission()
    if (path === `/api/jobs/${J}`) return reply({ job_id: J, tool: 'arlo-design', status: 'complete',
      org_id: O, project_id: P, input_version_id: V,
      result: { solver: 'arlo-design', solver_revision: 'fixture-revision' } })
    throw new Error(`Unexpected request: ${init.method || 'GET'} ${path}`)
  })
  vi.stubGlobal('fetch', fetchImpl)
  const jobSignal = new AbortController()
  const onSubmit = vi.fn()
  const onStatus = vi.fn()
  const input = { mock: false, signedIn: true, running: false,
    workspaceController: { getSnapshot: () => workspace },
    runJob: vi.fn(async ({ execute }) => execute({ signal: jobSignal.signal, onSubmit, onStatus })),
    rehydrate: vi.fn(async () => {}) }
  const calls = path => fetchImpl.mock.calls.filter(([url]) => new URL(String(url), 'https://studio.test').pathname === path)
  return { input, calls, onSubmit, onStatus, jobSignal,
    changeProject: () => { workspace = { ...workspace, openProjectId: OTHER } },
    changeOrg: () => { workspace = { ...workspace, workspace: { ...workspace.workspace,
      project: { project_id: P, org_id: OTHER } } } } }
}
async function mountMaterial(run) {
  function Material() {
    const onRunArlo = useAppArloReference(run.input)
    return <ReferenceMaterialIntake projectId={P} orgId={O} onRunArlo={onRunArlo} />
  }
  render(<Material />)
  await waitFor(() => expect(screen.getByRole('button', { name: 'Add reference files' })).toBeEnabled())
}
const clickRun = () => fireEvent.click(screen.getByRole('button', { name: 'Run ARLO with request.json' }))

describe('App Material ARLO catalog admission', () => {
  it('registers a fresh project reference and submits its exact input and server catalog digest on the explicit click', async () => {
    const run = setup()
    await mountMaterial(run)
    expect(run.calls('/api/run')).toHaveLength(0)
    expect(run.calls('/api/tools')).toHaveLength(0)
    clickRun()
    await screen.findByText('ARLO result saved to this project.')
    expect(run.calls(`/api/projects/${P}/reference-files/${M}/arlo-input`)).toHaveLength(1)
    expect(run.calls(`/api/projects/${P}/arlo-inputs/${V}`)).toHaveLength(1)
    const [[, catalogRequest]] = run.calls('/api/tools')
    expect(catalogRequest.headers.Authorization).toBe(`Bearer ${localStorage.getItem('leaf.jwt')}`)
    expect(run.calls('/api/tools')).toHaveLength(1)
    expect(run.calls('/api/run')).toHaveLength(1)
    const [[, sent]] = run.calls('/api/run')
    expect(sent.method).toBe('POST')
    expect(JSON.parse(sent.body)).toEqual({ tool: 'arlo-design', params: registered.request,
      dwg: registered.input_version_id, catalog_digest: digest })
    expect(sent.headers).toMatchObject({ 'X-Org-Id': O, 'X-Project-Id': P,
      'Idempotency-Key': RUN_KEY, Authorization: `Bearer ${localStorage.getItem('leaf.jwt')}` })
    expect(run.onSubmit).toHaveBeenCalledExactlyOnceWith(J)
    expect(run.onStatus).toHaveBeenCalledWith(expect.objectContaining({ status: 'submitted', job_id: J }))
    expect(run.input.rehydrate).toHaveBeenCalledOnce()
  })

  it.each([
    ['missing tool', []], ['missing digest', [{ name: 'arlo-design' }]],
    ['empty digest', [{ name: 'arlo-design', catalog_digest: '' }]],
    ['blank digest', [{ name: 'arlo-design', catalog_digest: '  ' }]],
    ['invalid digest type', [{ name: 'arlo-design', catalog_digest: 42 }]],
  ])('shows a clear refusal without submission for a %s', async (_label, tools) => {
    const run = setup({ catalog: async () => reply({ tools }) })
    await mountMaterial(run)
    clickRun()
    expect((await screen.findByRole('alert')).textContent).toMatch(/ARLO is unavailable or its catalog confirmation is missing/)
    expect(run.calls('/api/run')).toHaveLength(0)
    expect(run.input.runJob).not.toHaveBeenCalled()
    expect(run.input.rehydrate).not.toHaveBeenCalled()
  })

  it('shows a catalog fetch failure without starting a job', async () => {
    const run = setup({ catalog: async () => reply({}, 503) })
    await mountMaterial(run)
    clickRun()
    expect((await screen.findByRole('alert')).textContent).toMatch(/ARLO tool catalog could not be loaded/)
    expect(run.calls('/api/run')).toHaveLength(0)
    expect(run.input.runJob).not.toHaveBeenCalled()
  })

  it('rechecks the workspace after a deferred catalog fetch and displays the project-change refusal', async () => {
    const pending = deferred()
    const run = setup({ catalog: () => pending.promise })
    await mountMaterial(run)
    clickRun()
    await waitFor(() => expect(run.calls('/api/tools')).toHaveLength(1))
    run.changeProject()
    await act(async () => { pending.resolve(reply({ tools: [tool] })) })
    expect((await screen.findByRole('alert')).textContent).toMatch(/account or project changed/)
    expect(run.calls('/api/run')).toHaveLength(0)
    expect(run.input.runJob).not.toHaveBeenCalled()
    expect(run.input.rehydrate).not.toHaveBeenCalled()
  })

  it.each(['account', 'organization', 'abort'])('refuses a pending catalog result after %s changes', async change => {
    const pending = deferred()
    const run = setup({ catalog: () => pending.promise })
    const hook = renderHook(() => useAppArloReference(run.input))
    const controller = new AbortController()
    let result
    act(() => { result = hook.result.current(registered, {
      scope: readActiveScope(), signal: controller.signal, idempotencyKey: RUN_KEY,
    }) })
    await waitFor(() => expect(run.calls('/api/tools')).toHaveLength(1))
    if (change === 'account') account('another-owner')
    if (change === 'organization') run.changeOrg()
    if (change === 'abort') controller.abort()
    await act(async () => { pending.resolve(reply({ tools: [tool] })) })
    expect(await result).toMatchObject({ ok: false, error: {
      message: expect.stringMatching(change === 'abort' ? /cancelled/ : /account or project changed/),
    } })
    expect(run.calls('/api/run')).toHaveLength(0)
    expect(run.input.runJob).not.toHaveBeenCalled()
    expect(run.input.rehydrate).not.toHaveBeenCalled()
  })

  it('keeps a server catalog-change refusal visible and never automatically resubmits', async () => {
    const message = 'The catalog tool changed. Refresh tools and confirm again.'
    const run = setup({ submission: () => reply({ ok: false,
      error: { error_code: 'BAD_PARAMS', message, retryable: false } }, 409) })
    await mountMaterial(run)
    clickRun()
    expect((await screen.findByRole('alert')).textContent).toBe(message)
    expect(run.calls('/api/tools')).toHaveLength(1)
    expect(run.calls('/api/run')).toHaveLength(1)
    expect(run.calls(`/api/jobs/${J}`)).toHaveLength(0)
    expect(run.onSubmit).not.toHaveBeenCalled()
  })
})
