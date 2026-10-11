// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup } from '@testing-library/react'
import { createHash } from 'node:crypto'
import { runInNewContext } from 'node:vm'
import { createSolarProjectClient } from './solarProjectClient.js'
import { PROJECT_READ_REASONS, projectReadReason } from './solarReadResultModel.js'
import { createSolarProjectArtifactClient, validateSolarProjectArtifactRef,
  SOLAR_PROJECT_ARTIFACT_MAX_BYTES, SOLAR_PROJECT_ARTIFACT_TIMEOUT_MS } from './solarProjectArtifactClient.js'
const projectBinding = Object.freeze({
  kind: 'project',
  organization_id: '11111111-1111-4111-8111-111111111111',
  project_id: '22222222-2222-4222-8222-222222222222',
  drawing_id: '33333333-3333-4333-8333-333333333333',
  input_version_id: '44444444-4444-4444-8444-444444444444',
})
const bodyHash = '44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a'
const intakeHash = 'a'.repeat(64)
const graphHash = 'b'.repeat(64)
const catalogDigest = 'sha256:' + 'c'.repeat(64)
function projectRef(overrides = {}) {
  const artifactId = 'd'.repeat(64)
  return { organization_id: projectBinding.organization_id, project_id: projectBinding.project_id,
    drawing_id: projectBinding.drawing_id, source_version_id: projectBinding.input_version_id,
    source_intake_sha256: intakeHash, graph_sha256: graphHash, artifact_id: artifactId,
    media_type: 'application/json', filename: 'ElectricalSchedules.json', byte_length: 2,
    content_sha256: bodyHash, schema: 'leaf.solar-project-artifact-ref.v1',
    download: `/api/projects/${projectBinding.project_id}/drawings/${projectBinding.drawing_id}/solar-artifacts/${artifactId}`,
    ...overrides }
}
function projectResult(overrides = {}) {
  return { schema: 'leaf.solar-project-read.v1', adapter: 'project-local-graph-read',
    organization_id: projectBinding.organization_id, project_id: projectBinding.project_id,
    drawing_id: projectBinding.drawing_id, input_version_id: projectBinding.input_version_id,
    input_intake_sha256: intakeHash, graph_sha256: graphHash, tool: 'solar-electrical-schedules',
    tool_manifest_sha256: catalogDigest, graph_project_id: 'private-graph-project',
    head_version_id: projectBinding.input_version_id, is_head: true, current_required: true,
    request_sha256: 'e'.repeat(64), output_sha256: 'f'.repeat(64), output_bytes: 200,
    drawing_changed: false, output: { result: 57.4, units: 'A', artifact: projectRef() },
    ...overrides }
}

afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); vi.restoreAllMocks() })
const digestBytes = async (bytes) => createHash('sha256').update(bytes).digest('hex')
const sourceHashes = { inputIntakeSha256: intakeHash, graphSha256: graphHash }
function response(overrides = {}) {
  return { ok: true, status: 200, data: { notModified: false, bytes: new Uint8Array([123, 125]),
    headers: { 'Content-Type': 'application/json; charset=utf-8', 'Content-Length': '2',
      ETag: '"' + bodyHash + '"', 'X-Leaf-Artifact-Id': 'd'.repeat(64) } }, ...overrides }
}
function harness({ transport = async () => response(), digest = digestBytes, timeoutMs } = {}) {
  const listeners = new Set()
  let valid = true
  let snapshot = { binding: projectBinding, authorityKey: 'epoch-1' }
  const projectClient = {
    downloadArtifact: vi.fn(transport),
    getSnapshot: () => snapshot,
    captureCatalogScope: () => () => valid,
    subscribe: (listener) => { listeners.add(listener); return () => listeners.delete(listener) },
  }
  const client = createSolarProjectArtifactClient({ projectClient, digest, timeoutMs })
  const options = { binding: projectBinding, authorityKey: 'epoch-1', ref: projectRef(), ...sourceHashes,
    isCurrent: () => true }
  return { client, projectClient, options,
    invalidate: (next = snapshot) => { snapshot = next; valid = false; for (const listener of listeners) listener() } }
}
const never = () => new Promise(() => {})
const flush = async () => { for (let index = 0; index < 12; index += 1) await Promise.resolve() }

describe('verified project artifacts', () => {
  it('R12-03 validates and privately captures the canonical reference', async () => {
    const ref = projectRef()
    expect(validateSolarProjectArtifactRef(ref, projectBinding, sourceHashes)).toBe(true)
    const h = harness()
    const pending = h.client.downloadArtifact({ ...h.options, ref })
    ref.filename = 'changed.json'
    const result = await pending
    expect(result.ok).toBe(true)
    expect(result.data.filename).toBe('ElectricalSchedules.json')
    expect(h.projectClient.downloadArtifact).toHaveBeenCalledTimes(1)
    const [copied, options] = h.projectClient.downloadArtifact.mock.calls[0]
    expect(copied).not.toBe(ref)
    expect(Object.isFrozen(copied)).toBe(true)
    expect(options).toEqual({ current: true, signal: expect.any(AbortSignal) })
    expect(Object.keys(h.client)).toEqual(['downloadArtifact'])
    expect(Object.isFrozen(h.client)).toBe(true)
    const foreignBytes = runInNewContext('new Uint8Array([123, 125])')
    const raw = response()
    raw.data.bytes = foreignBytes
    const foreign = harness({ transport: async () => raw, digest: async (bytes) => {
      foreignBytes[0] = 88
      const digest = await digestBytes(bytes)
      bytes[0] = 89
      return digest
    } })
    const verified = await foreign.client.downloadArtifact(foreign.options)
    expect(verified.ok).toBe(true)
    expect(verified.data.bytes).toEqual(new Uint8Array([123, 125]))
    expect(verified.data.bytes).not.toBe(foreignBytes)
  })

  it('R12-05 refuses same-length bytes with the wrong content hash', async () => {
    const raw = response()
    raw.data.bytes = new Uint8Array([88, 88])
    const h = harness({ transport: async () => raw })
    expect(await h.client.downloadArtifact(h.options)).toEqual({
      ok: false, status: 200, reason: 'SIP_R12_ARTIFACT_MISMATCH', envelope: null })
  })

  it('R12-06 refuses every binding mismatch before transport', async () => {
    for (const key of ['organization_id', 'project_id', 'drawing_id', 'source_version_id']) {
      const h = harness()
      const outcome = await h.client.downloadArtifact({ ...h.options,
        ref: projectRef({ [key]: '99999999-9999-4999-8999-999999999999' }) })
      expect(outcome.reason).toBe('SIP_R12_ARTIFACT_REF_INVALID')
      expect(h.projectClient.downloadArtifact).not.toHaveBeenCalled()
    }
  })

  it('R12-08 rejects hostile references and the closed filename media path grammar', async () => {
    const accessor = vi.fn(() => 'ElectricalSchedules.json')
    const hostile = projectRef()
    Object.defineProperty(hostile, 'filename', { get: accessor, enumerable: true })
    const ref = projectRef()
    const invalid = [hostile, { ...ref, extra: true }, { ...ref, [Symbol('x')]: true },
      { ...ref, filename: '../a.json' }, { ...ref, filename: 'a..json' }, { ...ref, filename: 'a.csv' },
      { ...ref, filename: 'a'.repeat(121) + '.json' }, { ...ref, media_type: 'text/plain' },
      { ...ref, byte_length: 0 }, { ...ref, byte_length: SOLAR_PROJECT_ARTIFACT_MAX_BYTES + 1 },
      ...['https://example.com' + ref.download, ref.download + '?current=true', ref.download + '#x',
        ref.download.replace('/drawings/', '/%64rawings/'), '/api/drawings/x/artifacts/' + ref.artifact_id]
        .map((download) => ({ ...ref, download }))]
    for (const value of invalid) {
      const h = harness()
      expect(validateSolarProjectArtifactRef(value, projectBinding, sourceHashes)).toBe(false)
      expect((await h.client.downloadArtifact({ ...h.options, ref: value })).reason).toBe('SIP_R12_ARTIFACT_REF_INVALID')
      expect(h.projectClient.downloadArtifact).not.toHaveBeenCalled()
    }
    expect(accessor).not.toHaveBeenCalled()
    for (const [media_type, extension] of [['text/csv', 'csv'], ['application/json', 'json'],
      ['application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', 'xlsx'],
      ['application/vnd.google-earth.kml+xml', 'kml'], ['application/xml', 'xml'], ['application/pdf', 'pdf']]) {
      expect(validateSolarProjectArtifactRef(projectRef({ media_type, filename: 'File.' + extension }), projectBinding, sourceHashes)).toBe(true)
    }
  })

  it('R12-09 rejects byte length and exposed header mismatches', async () => {
    for (const [key, value] of [['ETag', '"wrong"'], ['X-Leaf-Artifact-Id', 'a'.repeat(64)],
      ['Content-Type', 'text/csv'], ['Content-Type', 'application/json; charset=latin1'],
      ['Content-Length', '2.0'], ['Content-Length', '3'], ['Content-Disposition', 'inline'],
      ['X-Content-Type-Options', 'sniff']]) {
      const raw = response()
      raw.data.headers[key] = value
      const h = harness({ transport: async () => raw })
      expect((await h.client.downloadArtifact(h.options)).reason).toBe('SIP_R12_ARTIFACT_MISMATCH')
    }
    const raw = response()
    raw.data.bytes = new Uint8Array([1])
    const h = harness({ transport: async () => raw })
    expect((await h.client.downloadArtifact(h.options)).reason).toBe('SIP_R12_ARTIFACT_MISMATCH')
    const good = response()
    delete good.data.headers['Content-Length']
    good.data.headers['Content-Disposition'] = 'attachment; filename="ElectricalSchedules.json"'
    good.data.headers['X-Content-Type-Options'] = 'nosniff'
    const valid = harness({ transport: async () => good })
    expect((await valid.client.downloadArtifact(valid.options)).ok).toBe(true)
  })

  it('R12-10 rejects incomplete responses and unsolicited 304 without retrying', async () => {
    const cases = [response({ status: 304, data: { notModified: true, bytes: null } }),
      response({ data: { notModified: false, bytes: [] } }), response({ data: null }), response({ status: 201 })]
    for (const key of ['ETag', 'X-Leaf-Artifact-Id', 'Content-Type']) {
      const raw = response(); delete raw.data.headers[key]; cases.push(raw)
    }
    for (const raw of cases) {
      const h = harness({ transport: async () => raw })
      expect((await h.client.downloadArtifact(h.options)).reason).toBe('SIP_R12_RESPONSE_INVALID')
      expect(h.projectClient.downloadArtifact).toHaveBeenCalledTimes(1)
    }
  })

  it('R12-11 refuses unavailable rejected or malformed hashing and validates factory dependencies', async () => {
    for (const digest of [async () => { throw new Error('no digest') }, async () => null, async () => bodyHash.toUpperCase()]) {
      const h = harness({ digest })
      expect((await h.client.downloadArtifact(h.options)).reason).toBe('SIP_R12_DIGEST_UNAVAILABLE')
    }
    vi.stubGlobal('crypto', {})
    const h = harness()
    const client = createSolarProjectArtifactClient({ projectClient: h.projectClient })
    expect((await client.downloadArtifact(h.options)).reason).toBe('SIP_R12_DIGEST_UNAVAILABLE')
    for (const options of [{}, { projectClient: h.projectClient, digest: null },
      { projectClient: h.projectClient, timeoutMs: 0 }]) expect(() => createSolarProjectArtifactClient(options)).toThrow(TypeError)
    expect((await h.client.downloadArtifact(null)).ok).toBe(false)
  })

  it('R12-18 races abort and the total deadline during transport and hashing', async () => {
    expect(SOLAR_PROJECT_ARTIFACT_TIMEOUT_MS).toBe(60000)
    vi.useFakeTimers()
    for (const stage of ['transport', 'digest']) {
      const stalledDigest = vi.fn(never)
      const h = harness(stage === 'transport' ? { transport: never, timeoutMs: 10 } : { digest: stalledDigest, timeoutMs: 10 })
      const pending = h.client.downloadArtifact(h.options)
      await flush()
      if (stage === 'digest') expect(stalledDigest).toHaveBeenCalledTimes(1)
      await vi.advanceTimersByTimeAsync(11)
      expect((await pending).reason).toBe('SIP_R12_TIMEOUT')
      expect(h.projectClient.downloadArtifact.mock.calls[0][1].signal.aborted).toBe(true)
      const abort = new AbortController()
      const cancelledDigest = vi.fn(never)
      const other = harness(stage === 'transport' ? { transport: never } : { digest: cancelledDigest })
      const cancelled = other.client.downloadArtifact({ ...other.options, signal: abort.signal })
      await flush()
      if (stage === 'digest') expect(cancelledDigest).toHaveBeenCalledTimes(1)
      abort.abort()
      expect((await cancelled).reason).toBe('SIP_R12_ABORTED')
    }
  })

  it('R12-23 preserves stale server outcomes through the merged R7 transport', async () => {
    for (const reason of ['ARTIFACT_STALE', 'SOLAR_OUTPUT_NOT_CURRENT']) {
      const envelope = { error: { reason_code: reason, retryable: false } }
      const fetchImpl = vi.fn(async () => new Response(JSON.stringify(envelope), { status: 409 }))
      const projectClient = createSolarProjectClient({ fetchImpl })
      projectClient.setScope(projectBinding, 'epoch-1')
      const client = createSolarProjectArtifactClient({ projectClient, digest: digestBytes })
      const result = await client.downloadArtifact({ ...harness().options })
      expect(result).toEqual({ ok: false, status: 409, reason, envelope })
      expect(projectReadReason(result.reason, result.status)).toBe(PROJECT_READ_REASONS.stale)
      expect(fetchImpl.mock.calls[0][0]).toBe(projectRef().download + '?current=true')
      expect(fetchImpl.mock.calls[0][1].cache).toBe('no-store')
      projectClient.dispose()
    }
  })

  it('R12-24 preserves exact corrupt missing revoked disabled and storage outcomes', async () => {
    for (const [reason, status] of [['ARTIFACT_CORRUPT', 500], ['SIP_R6_ARTIFACT_SOURCE_MISMATCH', 500],
      ['ARTIFACT_NOT_FOUND', 404], ['FORBIDDEN', 403], ['project_execution_disabled', 409], ['ARTIFACT_STORE_UNAVAILABLE', 503]]) {
      const refusal = { ok: false, status, reason, envelope: { error: { reason_code: reason, retryable: false } } }
      const h = harness({ transport: async () => refusal })
      expect(await h.client.downloadArtifact(h.options)).toBe(refusal)
    }
  })

  it('R12-25 consumes R7 closed reasons without echoing free text or inventing a code', async () => {
    for (const reason of [undefined, 'secret /path', 'lco1.' + 'a'.repeat(64)]) {
      const projectClient = createSolarProjectClient({ fetchImpl: async () => new Response(
        JSON.stringify({ error: { reason_code: reason } }), { status: 500 }) })
      projectClient.setScope(projectBinding, 'epoch-1')
      const wrapper = createSolarProjectArtifactClient({ projectClient, digest: digestBytes })
      const result = await wrapper.downloadArtifact(harness().options)
      expect(result.reason).toBe('HTTP_ERROR')
      expect(Object.hasOwn(result, 'reason_code')).toBe(false)
      projectClient.dispose()
    }
  })
})
