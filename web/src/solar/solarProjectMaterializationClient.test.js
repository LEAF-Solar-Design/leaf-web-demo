import { describe, expect, it, vi } from 'vitest'
import { captureSolarPublication, createSolarProjectMaterializationClient,
  initialSolarParameters, solarPublicationMatches, solarPublicationReceiptMatches } from './solarProjectMaterializationClient.js'
import { seatSolarProjectPublication } from '../App.jsx'
import generationIntake from '../../../docs/parity/evidence/ground/generate/intake.json'

const id = n => `00000000-0000-0000-0000-${String(n).padStart(12, '0')}`
const scope = { organization_id: id(1), project_id: id(2), drawing_id: id(3), input_version_id: id(4) }
const physical = { physical_head: 'a'.repeat(64), physical_content_sha256: 'b'.repeat(64), physical_index: 2 }
const readback = { schema: 'leaf.project-physical-layout.v1', organization_id: id(1), project_id: id(2),
  drawing_id: id(3), parent_version_id: id(4), physical_source: physical, writer_available: true }
const captured = () => captureSolarPublication(scope, readback, id(5))
const originalParameters = { boundary: generationIntake.boundary, preset: generationIntake.active_preset, drawing_units: 'm' }
const initialScope = { ...scope, drawing_id: null, input_version_id: null }
const initialReadback = { ...readback, mode: 'initial', drawing_id: null, parent_version_id: null, physical_source: undefined }
const receipt = c => ({ organization_id: c.organization_id, project_id: c.project_id,
  drawing_id: c.drawing_id ?? id(3), parent_version_id: c.input_version_id, request_id: c.request_id,
  source_sha256: 'c'.repeat(64), intake_sha256: 'd'.repeat(64),
  ...(c.mode === 'initial' ? { mode: 'initial', generation_source: { parameters_sha256: 'e'.repeat(64), physical_content_sha256: 'f'.repeat(64) } }
    : { physical_source: { ...physical } }),
  drawing_version: { org_id: c.organization_id, project_id: c.project_id, drawing_id: c.drawing_id ?? id(3), version_id: id(6) } })
const response = (body, status = 200) => ({ ok: status < 400, status,
  headers: { get: () => null }, text: async () => JSON.stringify(body) })
const client = fetchImpl => createSolarProjectMaterializationClient({ fetchImpl,
  headers: () => ({ 'Authorization': 'test-only', 'X-Checkout-Capability': 'test-checkout' }) })

describe('canonical project Solar publication client', () => {
  it('reads actual canonical physical source and sends only its three fields', async () => {
    const c = captured(), fetchImpl = vi.fn().mockResolvedValueOnce(response(readback)).mockResolvedValueOnce(response(receipt(c)))
    const api = client(fetchImpl)
    expect(await api.read(scope)).toMatchObject({ ok: true, value: readback })
    expect(await api.publish(c)).toMatchObject({ ok: true })
    const [url, options] = fetchImpl.mock.calls[1]
    expect(url).toBe(`/api/projects/${id(2)}/drawings/${id(3)}/versions/${id(4)}/solar-layout`)
    expect(JSON.parse(options.body)).toEqual(physical)
    expect(options.headers).toMatchObject({ 'Idempotency-Key': id(5), 'X-Org-Id': id(1),
      'X-Project-Id': id(2), 'X-Checkout-Capability': 'test-checkout' })
    expect(options.redirect).toBe('error')
  })

  it.each(['organization_id', 'project_id', 'drawing_id', 'input_version_id'])(
    'rejects missing canonical %s without a write', async field => {
      const fetchImpl = vi.fn(), invalid = { ...captured(), [field]: null }
      expect(await client(fetchImpl).publish(invalid)).toMatchObject({ ok: false, outcomeUnknown: false })
      expect(fetchImpl).not.toHaveBeenCalled()
    })

  it('does not reinterpret a standalone numeric drawing version as a canonical parent', () => {
    expect(captureSolarPublication({ ...scope, input_version_id: 7 }, readback, id(5))).toBeNull()
  })

  it.each(['request_id', 'parent_version_id', 'organization_id', 'project_id', 'drawing_id'])(
    'refuses a foreign or late receipt %s', async field => {
      const c = captured(), answer = { ...receipt(c), [field]: id(99) }
      expect(await client(vi.fn(async () => response(answer))).publish(c))
        .toMatchObject({ ok: false, code: 'PROJECT_LAYOUT_RESPONSE_INVALID', outcomeUnknown: true })
    })

  it('rejects changed physical source in reads and publication receipts', async () => {
    const changed = { ...physical, physical_index: 3 }
    expect(captureSolarPublication(scope, { ...readback, project_id: id(99) }, id(5))).toBeNull()
    expect(solarPublicationReceiptMatches({ ...receipt(captured()), physical_source: changed }, captured())).toBe(false)
    expect(solarPublicationMatches(captured(), { ...captured(), ...changed })).toBe(false)
  })

  it('retains the exact idempotency key across a network-unknown retry', async () => {
    const c = captured(), fetchImpl = vi.fn().mockRejectedValueOnce(new Error('network')).mockResolvedValueOnce(response(receipt(c)))
    const api = client(fetchImpl)
    expect(await api.publish(c)).toMatchObject({ code: 'PROJECT_LAYOUT_OUTCOME_UNKNOWN', outcomeUnknown: true })
    expect(await api.publish(c)).toMatchObject({ ok: true })
    expect(fetchImpl.mock.calls.map(([, init]) => init.headers['Idempotency-Key'])).toEqual([id(5), id(5)])
  })

  it('reports the licensed writer negative boundary without a success or unknown write', async () => {
    const answer = await client(vi.fn(async () => response({ error: { reason_code: 'PROJECT_LAYOUT_LICENSED_WRITER_UNAVAILABLE' } }, 503))).publish(captured())
    expect(answer).toEqual({ ok: false, code: 'PROJECT_LAYOUT_LICENSED_WRITER_UNAVAILABLE', outcomeUnknown: false })
  })

  it('refuses publication without the current checkout capability', async () => {
    const fetchImpl = vi.fn()
    expect(await createSolarProjectMaterializationClient({ fetchImpl }).publish(captured()))
      .toMatchObject({ code: 'PROJECT_LAYOUT_CHECKOUT_REQUIRED', outcomeUnknown: false })
    expect(fetchImpl).not.toHaveBeenCalled()
  })

  it('bounds stalled POST headers and body read by the same wall deadline', async () => {
    const api = createSolarProjectMaterializationClient({ timeoutMs: 5,
      headers: () => ({ 'X-Checkout-Capability': 'test' }),
      fetchImpl: async () => ({ ok: true, headers: { get: () => null }, text: () => new Promise(() => {}) }) })
    expect(await api.publish(captured())).toMatchObject({ outcomeUnknown: true })
  })
  it('bounds a stalled canonical GET body without beginning a write', async () => {
    const fetchImpl = vi.fn(async () => ({ ok: true, headers: { get: () => null }, text: () => new Promise(() => {}) }))
    const api = createSolarProjectMaterializationClient({ timeoutMs: 5, fetchImpl })
    expect(await api.read(scope)).toMatchObject({ ok: false })
    expect(fetchImpl).toHaveBeenCalledOnce()
    expect(fetchImpl.mock.calls[0][1].method).toBeUndefined()
  })
  it('uses the actual initial project route and whole original generation request without a checkout', async () => {
    const c = captureSolarPublication(initialScope, initialReadback, id(5), originalParameters)
    const fetchImpl = vi.fn().mockResolvedValueOnce(response(initialReadback)).mockResolvedValueOnce(response(receipt(c)))
    const api = createSolarProjectMaterializationClient({ fetchImpl, headers: () => ({ 'Authorization': 'test-only' }) })
    expect(await api.read(initialScope)).toMatchObject({ ok: true })
    expect(await api.publish(c)).toMatchObject({ ok: true })
    expect(fetchImpl.mock.calls.map(([url]) => url)).toEqual([`/api/projects/${id(2)}/solar-layout`, `/api/projects/${id(2)}/solar-layout`])
    expect(JSON.parse(fetchImpl.mock.calls[1][1].body)).toEqual(originalParameters)
    expect(fetchImpl.mock.calls[1][1].headers['X-Checkout-Capability']).toBeUndefined()
    expect(c.drawing_id).toBeNull()
    expect(c.input_version_id).toBeNull()
  })
  it('rejects input drift and unsupported units without turning parameters into geometry', () => {
    const c = captureSolarPublication(initialScope, initialReadback, id(5), originalParameters)
    const changed = captureSolarPublication(initialScope, initialReadback, id(5), { ...originalParameters, drawing_units: 'ft' })
    expect(solarPublicationMatches(c, changed)).toBe(false)
    expect(initialSolarParameters(originalParameters.boundary, originalParameters.preset, 'mm')).toBeNull()
    expect(initialSolarParameters([[0, 0], [1, Infinity], [0, 2]], originalParameters.preset, 'm')).toBeNull()
  })
  it('refuses an initial response without generation provenance', () => {
    const c = captureSolarPublication(initialScope, initialReadback, id(5), originalParameters)
    expect(solarPublicationReceiptMatches({ ...receipt(c), generation_source: null }, c)).toBe(false)
  })
})

describe('actual App canonical Solar callback', () => {
  function controller(c, answer) {
    const state = { orgId: c.organization_id, openProjectId: c.project_id, canonicalVersionId: c.input_version_id,
      workspace: { drawing_versions: c.drawing_id ? [{ ...answer.drawing_version, version_id: c.input_version_id }] : [] } }
    return { state, getSnapshot: () => ({ ...state }),
      rehydrate: vi.fn(async () => { state.workspace = { drawing_versions: [...state.workspace.drawing_versions, answer.drawing_version] }; return state.workspace }),
      selectCanonicalVersion: vi.fn(version => { state.canonicalVersionId = version }) }
  }
  it('seats only the correlated returned canonical version after refresh', async () => {
    const c = captured(), answer = receipt(c), ctl = controller(c, answer)
    expect(await seatSolarProjectPublication(ctl, answer, c, () => true)).toBe(true)
    expect(ctl.selectCanonicalVersion).toHaveBeenCalledWith(id(6))
  })
  it('refuses a prior request when selection already changed before callback', async () => {
    const c = captured(), answer = receipt(c), ctl = controller(c, answer)
    ctl.state.canonicalVersionId = id(99)
    expect(await seatSolarProjectPublication(ctl, answer, c, () => true)).toBe(false)
    expect(ctl.rehydrate).not.toHaveBeenCalled()
  })
  it('refuses head/request drift during rehydrate without changing selection', async () => {
    const c = captured(), answer = receipt(c), ctl = controller(c, answer)
    let current = true
    ctl.rehydrate.mockImplementation(async () => { current = false; return { drawing_versions: [answer.drawing_version] } })
    expect(await seatSolarProjectPublication(ctl, answer, c, () => current)).toBe(false)
    expect(ctl.selectCanonicalVersion).not.toHaveBeenCalled()
  })
  it('preserves prior selection when rehydration fails', async () => {
    const c = captured(), answer = receipt(c), ctl = controller(c, answer)
    ctl.rehydrate.mockRejectedValue(new Error('unavailable'))
    await expect(seatSolarProjectPublication(ctl, answer, c, () => true)).rejects.toThrow('unavailable')
    expect(ctl.selectCanonicalVersion).not.toHaveBeenCalled()
  })
  it('accepts captured empty selection for first publication without a dummy drawing', async () => {
    const c = captureSolarPublication(initialScope, initialReadback, id(5), originalParameters)
    const answer = receipt(c), ctl = controller(c, answer)
    expect(c.mode).toBe('initial')
    expect(await seatSolarProjectPublication(ctl, answer, c, () => true)).toBe(true)
    expect(ctl.selectCanonicalVersion).toHaveBeenCalledWith(id(6))
  })
})
