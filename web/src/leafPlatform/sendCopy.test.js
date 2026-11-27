// @vitest-environment jsdom
// M3 S4 flow against a fake host and a fake server: the happy path, every section 5 stop,
// a sign-in or workspace change mid-flow, and an old plugin.
import { describe, expect, it, vi } from 'vitest'
import golden from './__fixtures__/leaf.send-copy.v1.golden.json'
import { fileStem, fingerprintLine, fingerprintsMatch, runSendCopy, sendCopyFileName, sendCopyMessage } from './sendCopy.js'

const ORG = '0a0a0a0a-0000-4000-8000-000000000001'
const PROJECT = '0b0b0b0b-0000-4000-8000-000000000002'
const DRAWING = '0c0c0c0c-0000-4000-8000-000000000003'
const goldenSent = JSON.parse(golden.cases.find((entry) => entry.name === 'sent').wire).payload
const sent = () => ({ status: 'sent', reason: null, fileName: goldenSent.fileName,
  bytes: Uint8Array.from(atob(goldenSent.dxfBase64), (c) => c.charCodeAt(0)),
  sha256: goldenSent.sha256, fingerprint: structuredClone(goldenSent.fingerprint) })
const httpError = (status, extra = {}) => Object.assign(new Error(extra.message || `HTTP ${status}`), { status, ...extra })

function setup({ identity, host = sent(), status } = {}) {
  let stored = null
  let key = 'token-a'
  const calls = []
  const api = {
    getSessionIdentity: vi.fn(async () => { calls.push('identity'); return identity ?? { org: ORG, workspace: { id: ORG, name: 'My workspace' } } }),
    createOrg: vi.fn(async () => { calls.push('createOrg'); return { org_id: ORG } }),
    setStoredOrgId: vi.fn((id) => { stored = id }),
    getStoredOrgId: vi.fn(() => stored),
    uploadDrawing: vi.fn(async () => { calls.push('upload'); return { drawing_id: DRAWING, tenant_kind: 'account', status: 'extracting' } }),
    getDrawingUploadStatus: vi.fn(async () => { calls.push('status'); return status ?? { status: 'ready', content_sha256: goldenSent.sha256, extracted_version: 1, fingerprint: structuredClone(goldenSent.fingerprint) } }),
    createProject: vi.fn(async () => { calls.push('project'); return { project_id: PROJECT } }),
    importUploadedDrawingVersion: vi.fn(async () => { calls.push('import'); return { drawingVersion: {}, replayed: false } }),
  }
  const bridge = { sendCopy: vi.fn(async () => { calls.push('sendCopy'); return typeof host === 'function' ? host() : host }) }
  const rememberProject = vi.fn()
  const options = { bridge, api, authority: () => key, rememberProject, sleep: async () => {} }
  return { api, bridge, rememberProject, calls, options, setKey: (value) => { key = value }, setStored: (value) => { stored = value } }
}

async function stopCode(promise) {
  try { await promise } catch (error) { return error }
  throw new Error('expected a stop')
}

describe('runSendCopy', () => {
  it('sends, uploads, checks, creates the project, imports and remembers it before opening', async () => {
    const { api, calls, options, rememberProject } = setup()
    const result = await runSendCopy(options)
    expect(calls).toEqual(['identity', 'sendCopy', 'upload', 'status', 'project', 'import'])
    expect(api.setStoredOrgId).toHaveBeenCalledWith(ORG)
    const file = api.uploadDrawing.mock.calls[0][0]
    expect(file.name).toBe('Parking.dxf')
    expect(file.size).toBe(70)
    expect(api.uploadDrawing.mock.calls[0]).toHaveLength(1)
    expect(api.createProject).toHaveBeenCalledWith('Parking', ORG, { requireCreated: false })
    expect(api.importUploadedDrawingVersion).toHaveBeenCalledWith(PROJECT,
      { drawingId: DRAWING, version: 1, name: 'Parking.dwg' }, { idempotencyKey: `b3:${PROJECT}:${DRAWING}:1` })
    expect(rememberProject).toHaveBeenCalledWith(ORG, PROJECT)
    expect(result.href).toBe(`/app?drawing=${DRAWING}`)
    expect(result.workspace).toBe('My workspace')
    expect(fingerprintLine(result.autocad, result.fileName)).toBe(fingerprintLine(result.studio, result.fileName))
  })

  it('creates the workspace for a brand-new account, then reads the identity again', async () => {
    const { api, calls, options } = setup()
    api.getSessionIdentity.mockRejectedValueOnce(httpError(403, { body: { detail: 'verified subject has no active platform identity binding' } }))
    await runSendCopy(options)
    expect(api.createOrg).toHaveBeenCalledWith('My workspace')
    expect(calls.slice(0, 3)).toEqual(['createOrg', 'identity', 'sendCopy'])
  })

  it('stops with no workspace when self-provisioning is invite only', async () => {
    const { api, bridge, options } = setup()
    api.getSessionIdentity.mockRejectedValueOnce(httpError(403, { body: { detail: 'verified subject has no active platform identity binding' } }))
    api.createOrg.mockRejectedValueOnce(httpError(403, { body: { detail: 'invite_only' } }))
    expect((await stopCode(runSendCopy(options))).code).toBe('no_workspace')
    expect(bridge.sendCopy).not.toHaveBeenCalled()
  })

  it.each([
    ['plugin_outdated', 'failed'], ['user_declined', 'declined'], ['no_model_objects', 'failed'], ['too_large', 'failed'],
    ['active_document_changed', 'failed'], ['timeout', 'failed'],
  ])('stops on a %s host result before any upload', async (reason, status) => {
    const { api, options } = setup({ host: { status, reason } })
    expect((await stopCode(runSendCopy(options))).code).toBe(reason)
    expect(api.uploadDrawing).not.toHaveBeenCalled()
    expect(api.createProject).not.toHaveBeenCalled()
  })

  it.each([[503, 'uploads_off'], [413, 'too_large'], [403, 'plan_lacks_upload'], [undefined, 'lost_reply']])(
    'maps upload HTTP %s to %s and creates no project', async (status, code) => {
      const { api, options } = setup()
      api.uploadDrawing.mockRejectedValueOnce(status ? httpError(status) : new TypeError('Failed to fetch'))
      const stopped = await stopCode(runSendCopy(options))
      expect(stopped.code).toBe(code)
      expect(api.uploadDrawing).toHaveBeenCalledTimes(1)
      expect(api.createProject).not.toHaveBeenCalled()
    })

  it('shows the server quota words, reset time included', async () => {
    const { api, options } = setup()
    api.uploadDrawing.mockRejectedValueOnce(httpError(429, { message: 'Your workspace has sent 20 drawings today. You can send more after the daily reset. Resets at Thu 6:00 PM CST.' }))
    const stopped = await stopCode(runSendCopy(options))
    expect(stopped.code).toBe('quota')
    expect(sendCopyMessage(stopped.code, stopped)).toMatch(/20 drawings today.*Resets at/)
  })

  it('reports a failed read with the server reason', async () => {
    const { options } = setup({ status: { status: 'failed', error: { message: 'not a DXF' } } })
    const stopped = await stopCode(runSendCopy(options))
    expect(stopped.code).toBe('read_failed')
    expect(sendCopyMessage(stopped.code, stopped)).toBe('Studio could not read this drawing: not a DXF')
  })

  it('polls while Studio reads, then stops at the 15 minute limit', async () => {
    let clock = 0
    const { api, options } = setup({ status: { status: 'extracting' } })
    const stopped = await stopCode(runSendCopy({ ...options, now: () => clock, sleep: async (ms) => { clock += ms } }))
    expect(stopped.code).toBe('read_timeout')
    expect(api.getDrawingUploadStatus.mock.calls.length).toBeGreaterThan(100)
    expect(api.createProject).not.toHaveBeenCalled()
  })

  it('stops, keeping the copy, when the counts differ', async () => {
    const studio = { ...structuredClone(goldenSent.fingerprint), model_objects: 3, layers_digest: 'f'.repeat(64) }
    const { api, options } = setup({ status: { status: 'ready', content_sha256: goldenSent.sha256, extracted_version: 1, fingerprint: studio } })
    const stopped = await stopCode(runSendCopy(options))
    expect(stopped.code).toBe('fingerprint_differs')
    expect(stopped.studio.model_objects).toBe(3)
    expect(api.importUploadedDrawingVersion).not.toHaveBeenCalled()
  })

  it('stops when Studio could not count the copy', async () => {
    const { options } = setup({ status: { status: 'ready', content_sha256: goldenSent.sha256, extracted_version: 1, fingerprint: null } })
    expect((await stopCode(runSendCopy(options))).code).toBe('fingerprint_unavailable')
  })

  it('stops when the stored bytes are not the sent bytes', async () => {
    const { options } = setup({ status: { status: 'ready', content_sha256: 'c'.repeat(64), extracted_version: 1, fingerprint: structuredClone(goldenSent.fingerprint) } })
    expect((await stopCode(runSendCopy(options))).code).toBe('fingerprint_differs')
  })

  it('retries a failed import with the same key and no new consent or upload', async () => {
    const { api, bridge, options } = setup()
    api.importUploadedDrawingVersion.mockRejectedValueOnce(httpError(500))
    const stopped = await stopCode(runSendCopy(options))
    expect(stopped.code).toBe('import_failed')
    const result = await runSendCopy({ ...options, sent: stopped.sent, upload: stopped.upload })
    expect(bridge.sendCopy).toHaveBeenCalledTimes(1)
    expect(api.uploadDrawing).toHaveBeenCalledTimes(1)
    expect(api.importUploadedDrawingVersion.mock.calls[1][2]).toEqual(api.importUploadedDrawingVersion.mock.calls[0][2])
    expect(result.projectId).toBe(PROJECT)
  })

  it('offers a separate project when the same-name project refuses the import', async () => {
    const { api, options } = setup()
    api.importUploadedDrawingVersion.mockRejectedValueOnce(httpError(403))
    const stopped = await stopCode(runSendCopy(options))
    expect(stopped.code).toBe('project_forbidden')
    await runSendCopy({ ...options, sent: stopped.sent, upload: stopped.upload, project: { name: 'Parking 2026-11-26 15:00', requireCreated: true } })
    expect(api.createProject).toHaveBeenLastCalledWith('Parking 2026-11-26 15:00', ORG, { requireCreated: true })
  })

  it('stops with nothing uploaded when the sign-in changes during consent', async () => {
    const { api, options, setKey } = setup({ host: () => { setKey('token-b'); return sent() } })
    expect((await stopCode(runSendCopy(options))).code).toBe('account_changed')
    expect(api.uploadDrawing).not.toHaveBeenCalled()
  })

  it('stops before the import when the stored workspace changes', async () => {
    const { api, options, setStored } = setup()
    api.createProject.mockImplementationOnce(async () => { setStored('0d0d0d0d-0000-4000-8000-000000000004'); return { project_id: PROJECT } })
    expect((await stopCode(runSendCopy(options))).code).toBe('account_changed')
    expect(api.importUploadedDrawingVersion).not.toHaveBeenCalled()
  })

  it('refuses to start signed out', async () => {
    const { options, setKey } = setup()
    setKey(null)
    expect((await stopCode(runSendCopy(options))).code).toBe('signed_out')
  })
})

describe('fingerprint helpers', () => {
  it('matches on the frozen fields only', () => {
    const a = structuredClone(goldenSent.fingerprint)
    expect(fingerprintsMatch(a, { ...a, layers: [] })).toBe(true)
    for (const key of ['sha256', 'bytes', 'model_objects', 'layer_count', 'layers_digest']) {
      expect(fingerprintsMatch(a, { ...a, [key]: typeof a[key] === 'number' ? a[key] + 1 : 'x' })).toBe(false)
    }
    expect(fingerprintsMatch(a, { ...a, status: 'unavailable' })).toBe(false)
  })

  it('prints more than eight layers the way the plugin does', () => {
    const layers = Array.from({ length: 10 }, (_, index) => ({ name: `L${index}`, count: 10 - index }))
    const line = fingerprintLine({ sha256: 'abcdef0123', layers, layer_count: 10, model_objects: 55 }, 'C:\\x\\Site.dwg')
    expect(line).toBe('Site.dwg | sent copy abcdef01 | L0 10, L1 9, L2 8, L3 7, L4 6, L5 5, L6 4, L7 3, and 2 more layers | 55 objects in model space')
    expect(fingerprintLine({ sha256: 'abcdef0123', layers: [{ name: 'A', count: 1 }], layer_count: 1, model_objects: 1 }, 'a.dwg')).toMatch(/\| 1 object in model space$/)
  })

  it('keeps file names as plain data', () => {
    expect(sendCopyFileName('..')).toBe('Drawing')
    expect(sendCopyFileName('a\u202Eb\u0007.dwg')).toBe('ab.dwg')
    expect(fileStem('Parking.final.dwg')).toBe('Parking.final')
    expect(fileStem('.dwg')).toBe('Drawing')
  })
})
