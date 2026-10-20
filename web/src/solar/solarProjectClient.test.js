import { afterEach, describe, expect, it, vi } from 'vitest'
import { createSolarProjectClient } from './solarProjectClient.js'
import { createRunSubmissionRequest } from '../runIntent.js'

const O = '11111111-1111-1111-1111-111111111111'
const P = '22222222-2222-2222-2222-222222222222'
const D = '33333333-3333-3333-3333-333333333333'
const V = '44444444-4444-4444-4444-444444444444'
const J = '55555555-5555-5555-5555-555555555555'
const binding = { kind: 'project', organization_id: O, project_id: P, drawing_id: D, input_version_id: V }
const digest = 'sha256:' + 'a'.repeat(64)
const token = () => 'lco1.' + 'b'.repeat(64)
const identity = { organization_id: O, project_id: P, drawing_id: D }
const lease = { holder: 'editor', acquired: '2026-01-01T00:00:00Z', expires: '2099-01-01T00:00:00Z', fence: '9007199254740993' }
const acquired = () => ({ ...identity, acquired: true, checkout: lease, checkout_capability: token() })
const contextData = { context: { ...identity, input_version_id: V, head_version_id: V, is_head: true }, intake: {} }
const job = (changes = {}) => ({ job_id: J, org_id: O, project_id: P, input_version_id: V,
  tool_name: 'solar-settings', status: 'complete', execution_context: { schema: 'leaf.solar-project-graph-job.v1', drawing_id: D }, ...changes })
const readData = { result: { ...identity, input_version_id: V, schema: 'leaf.solar-project-read.v1',
  adapter: 'project-local-graph-read', tool: 'solar-string-data', tool_manifest_sha256: digest,
  current_required: false, drawing_changed: false, output: { historical: true } } }
function response(data, status = 200) { return { ok: status >= 200 && status < 300, status, json: async () => data } }
function deferred() { let resolve; const promise = new Promise((done) => { resolve = done }); return { promise, resolve } }
const clients = []
function setup(data = contextData, status = 200, options = {}) {
  const fetchImpl = vi.fn(async () => response(data, status))
  const onUnauthorized = vi.fn()
  const client = createSolarProjectClient({ fetchImpl, apiBase: 'https://api.test', authHeaders: () => ({ Authorization: 'Bearer synthetic' }), onUnauthorized, ...options })
  client.setScope(binding, 'session-1')
  clients.push(client)
  const intent = (changes = {}) => client.prepareRun({ tool: 'solar-settings', params: { expected_rev: 0 }, catalogDigest: digest, idempotencyKey: 'request-1', ...changes })
  return { client, fetchImpl, onUnauthorized, intent }
}
afterEach(() => { for (const client of clients.splice(0)) client.dispose(); vi.useRealTimers() })
const init = (fetchImpl) => fetchImpl.mock.calls.at(-1)[1]
const url = (fetchImpl) => fetchImpl.mock.calls.at(-1)[0]
const refusal = (reason) => ({ error: { reason_code: reason, error_code: 'BAD_PARAMS', retryable: false }, detail: { preserved: true } })
function artifact(changes = {}) {
  const artifactId = 'c'.repeat(64)
  return { ...identity, source_version_id: V, source_intake_sha256: 'd'.repeat(64), graph_sha256: 'e'.repeat(64),
    artifact_id: artifactId, media_type: 'text/csv', filename: 'strings.csv', byte_length: 3,
    content_sha256: 'f'.repeat(64), schema: 'leaf.solar-project-artifact-ref.v1',
    download: `/api/projects/${P}/drawings/${D}/solar-artifacts/${artifactId}`, ...changes }
}

describe('project Solar transport and lifecycle', () => {
  it('W1C6 previews carry the same optional solve and commit fields as run bodies', async () => {
    const { client, fetchImpl, intent } = setup(acquired())
    await client.acquireCheckout()
    fetchImpl.mockResolvedValue(response({ families: [] }))
    for (const [tool, extra, wire] of [
      ['solar-settings', {}, {}],
      ['solar-solve-proposal', {}, {}],
      ['solar-commit-solve', {}, {}],
      ['solar-solve-proposal', { solveContext: { frame_ref: 'frame-1', expected_rev: 6 } },
        { solve_context: { frame_ref: 'frame-1', expected_rev: 6 } }],
      ['solar-solve-proposal', { solveContext: { frame_ref: 'frame-1', expected_rev: 6, phase: 'background' } },
        { solve_context: { frame_ref: 'frame-1', expected_rev: 6, phase: 'background' } }],
      ['solar-commit-solve', { proposalJobId: J }, { proposal_job_id: J }],
    ]) {
      const prepared = intent({ tool, ...extra })
      expect((await client.catalog({ intents: [prepared] })).ok).toBe(true)
      const entry = JSON.parse(new URL(url(fetchImpl)).searchParams.get('project_runs'))
      expect(entry).toEqual({ [tool]: { params: prepared.params, catalog_digest: digest,
        idempotency_key: 'request-1', ...wire } })
      expect(init(fetchImpl).headers['X-Checkout-Capability']).toBe(token())
      expect(url(fetchImpl)).not.toContain(token())
      expect(init(fetchImpl)).not.toHaveProperty('body')
    }
  })
  it('W1C5 carries only tool-matched proposal transport fields and keeps checkout proof private', async () => {
    const { client, fetchImpl, intent } = setup(acquired())
    await client.acquireCheckout()
    const solveContext = { frame_ref: 'frame-1', expected_rev: 6, phase: 'initial' }
    for (const [tool, extra, wire] of [
      ['solar-solve-proposal', { solveContext }, { solve_context: solveContext }],
      ['solar-commit-solve', { proposalJobId: J }, { proposal_job_id: J }],
    ]) {
      const prepared = intent({ tool, ...extra })
      expect(Object.keys(prepared).sort()).toEqual(['binding', 'authorityKey', 'tool', 'params',
        'catalogDigest', 'idempotencyKey', ...Object.keys(extra)].sort())
      fetchImpl.mockResolvedValueOnce(response({ job_id: J, status: 'submitted' }, 202))
      expect((await client.submitRun(prepared)).ok).toBe(true)
      expect(JSON.parse(init(fetchImpl).body)).toEqual({ tool, params: prepared.params, dwg: V, catalog_digest: digest, ...wire })
      expect(init(fetchImpl).headers['Idempotency-Key']).toBe('request-1')
      expect(init(fetchImpl).headers['X-Checkout-Capability']).toBe(token())
      expect(init(fetchImpl).body).not.toContain(token())
    }
    const prepared = intent({ tool: 'solar-solve-proposal', solveContext })
    solveContext.frame_ref = 'changed'
    expect(prepared.solveContext.frame_ref).toBe('frame-1')
    expect(Object.isFrozen(prepared.solveContext)).toBe(true)
  })
  it('W1C5 refuses extra fields on other tools, malformed values, accessors and open intents', async () => {
    const { client, fetchImpl, intent } = setup()
    const solveContext = { frame_ref: 'frame-1', expected_rev: 0 }
    for (const changes of [
      { solveContext }, { proposalJobId: J },
      { tool: 'solar-solve-proposal', proposalJobId: J },
      { tool: 'solar-commit-solve', solveContext },
      ...[null, {}, { ...solveContext, extra: 1 }, { ...solveContext, expected_rev: true },
        { ...solveContext, expected_rev: -1 }, { ...solveContext, phase: 'other' }]
        .map((value) => ({ tool: 'solar-solve-proposal', solveContext: value })),
      ...[null, 5, '', 'not-a-uuid'].map((proposalJobId) => ({ tool: 'solar-commit-solve', proposalJobId })),
    ]) expect(() => intent(changes)).toThrow(TypeError)
    let getters = 0
    const forged = { ...intent(), tool: 'solar-solve-proposal' }
    Object.defineProperty(forged, 'solveContext', { enumerable: true, get() { getters += 1; return solveContext } })
    for (const supplied of [forged, { ...intent(), solveContext }, { ...intent(), surprise: true },
      { ...intent(), tool: 'solar-commit-solve', proposalJobId: 'bad' }]) {
      expect((await client.submitRun(supplied)).reason).toBe('SIP_R7_REQUEST_INVALID')
      expect((await client.catalog({ intents: [supplied] })).reason).toBe('SIP_R7_REQUEST_INVALID')
    }
    expect(getters).toBe(0)
    expect(fetchImpl).not.toHaveBeenCalled()
  })
  it('R7-37 successes settling after sign-out are refused for every project read and write', async () => {
    for (const [data, invoke] of [
      [contextData, ({ client }) => client.readContext()],
      [{ families: [] }, ({ client, intent }) => client.catalog({ intents: [intent()] })],
      [{ job_id: J, status: 'submitted' }, ({ client, intent }) => client.submitRun(intent())],
      [job(), ({ client }) => client.readJob(J, { tool: 'solar-settings' })],
      [readData, ({ client }) => client.readSolar({ tool: 'solar-string-data', catalogDigest: digest })],
    ]) {
      const run = setup()
      const late = { ...data }
      Object.defineProperty(late, 'settlement', { enumerable: true, get() {
        run.client.setScope(null, null)
        return 'signed out'
      } })
      run.fetchImpl.mockResolvedValueOnce(response(late))
      expect(await invoke(run)).toEqual({ ok: false, status: null, reason: 'SIP_R7_SCOPE_STALE', envelope: null })
      expect(run.fetchImpl).toHaveBeenCalledOnce()
    }
  })
  it('R7-40 catalog carries checkout proof only with an exact preview', async () => {
    const { client, fetchImpl, intent } = setup(acquired())
    await client.acquireCheckout()
    fetchImpl.mockResolvedValue(response({ families: [] }))
    expect((await client.catalog()).ok).toBe(true)
    expect(url(fetchImpl)).not.toContain('project_runs')
    expect(init(fetchImpl).headers).not.toHaveProperty('X-Checkout-Capability')
    expect((await client.catalog({ intents: [intent()] })).ok).toBe(true)
    expect(init(fetchImpl).headers['X-Checkout-Capability']).toBe(token())
  })
  it('R7-41 prepare refuses embedded capability in a param value without fetch', () => {
    const { intent, fetchImpl } = setup()
    expect(() => intent({ params: { note: 'prefix ' + token() } })).toThrow(TypeError)
    expect(fetchImpl).not.toHaveBeenCalled()
  })
  it('R7-42 prepare refuses capability in a param key without fetch', () => {
    const { intent, fetchImpl } = setup()
    for (const key of [token(), 'prefix ' + token()]) {
      expect(() => intent({ params: { [key]: 'value' } })).toThrow(TypeError)
    }
    expect(fetchImpl).not.toHaveBeenCalled()
  })
  it('R7-43 prepare refuses embedded capability in an idempotency key without fetch', () => {
    const { intent, fetchImpl } = setup()
    expect(() => intent({ idempotencyKey: 'prefix ' + token() })).toThrow(TypeError)
    expect(fetchImpl).not.toHaveBeenCalled()
  })
  it('R7-44 take refuses embedded capability in holder without fetch', async () => {
    const { client, fetchImpl } = setup()
    expect((await client.acquireCheckout({ holder: 'holder ' + token() })).reason).toBe('SIP_R7_REQUEST_INVALID')
    expect(fetchImpl).not.toHaveBeenCalled()
  })
  it('R7-45 refusal envelopes remove embedded capability values and keys', async () => {
    const envelope = refusal('SIP_R1_CHECKOUT_DENIED')
    envelope.error.message = 'rejected ' + token()
    envelope.detail['prefix ' + token()] = 'hidden key'
    envelope.detail.items = ['prefix ' + token(), { nested: 'prefix ' + token() }]
    envelope.detail.leak = 'prefix ' + token()
    const { client } = setup(envelope, 403)
    const outcome = await client.readContext()
    expect(outcome).toMatchObject({ ok: false, status: 403, reason: 'SIP_R1_CHECKOUT_DENIED' })
    expect(JSON.stringify(outcome.envelope)).not.toContain(token())
    expect(outcome.envelope.detail.preserved).toBe(true)
    // A key whose string value carries a capability is dropped, never kept with a null value.
    expect(Object.hasOwn(outcome.envelope.detail, 'leak')).toBe(false)
    expect(Object.hasOwn(outcome.envelope.error, 'message')).toBe(false)
    expect(outcome.envelope.detail.items).toEqual([null, {}])
    expect(Object.keys(outcome.envelope.detail).some((key) => key.includes('lco1.'))).toBe(false)
  })
  it('R7-46 acquisition hides embedded capability in holder from outcome and snapshot', async () => {
    const { client } = setup({ ...acquired(), checkout: { ...lease, holder: 'holder ' + token() } })
    const outcome = await client.acquireCheckout()
    expect(outcome.ok).toBe(true)
    expect(client.getSnapshot().ownsCheckout).toBe(true)
    expect(JSON.stringify(outcome)).not.toContain(token())
    expect(JSON.stringify(client.getSnapshot())).not.toContain(token())
  })
  it('R7-47 exactly 262144 UTF-8 bytes are accepted for read and project preview', async () => {
    const { client, fetchImpl, intent } = setup(readData)
    const bytes = (value) => new TextEncoder().encode(JSON.stringify(value)).byteLength
    const readBase = { tool: 'solar-string-data', params: { drawing_id: D, padding: '' }, catalog_digest: digest, current: false }
    const padding = 'x'.repeat(262144 - bytes(readBase))
    expect(bytes({ ...readBase, params: { drawing_id: D, padding } })).toBe(262144)
    expect((await client.readSolar({ tool: 'solar-string-data', params: { padding }, catalogDigest: digest })).ok).toBe(true)
    expect(new TextEncoder().encode(init(fetchImpl).body).byteLength).toBe(262144)
    expect(fetchImpl).toHaveBeenCalledOnce()
    fetchImpl.mockClear()
    fetchImpl.mockResolvedValueOnce(response({ families: [] }))
    const baseIntent = intent({ params: { padding: '' } })
    const entries = (prepared) => ({ [prepared.tool]: { params: prepared.params, catalog_digest: prepared.catalogDigest, idempotency_key: prepared.idempotencyKey } })
    const prepared = intent({ params: { padding: 'x'.repeat(262144 - bytes(entries(baseIntent))) } })
    expect(bytes(entries(prepared))).toBe(262144)
    expect((await client.catalog({ intents: [prepared] })).ok).toBe(true)
    expect(new TextEncoder().encode(new URL(url(fetchImpl)).searchParams.get('project_runs')).byteLength).toBe(262144)
    expect(fetchImpl).toHaveBeenCalledOnce()
  })
  it('R7-48 project preview of 262145 UTF-8 bytes refuses before fetch', async () => {
    const { client, fetchImpl, intent } = setup({ families: [] })
    const entries = (prepared) => ({ [prepared.tool]: { params: prepared.params, catalog_digest: prepared.catalogDigest, idempotency_key: prepared.idempotencyKey } })
    const bytes = (value) => new TextEncoder().encode(JSON.stringify(value)).byteLength
    const prepared = intent({ params: { padding: 'x'.repeat(262145 - bytes(entries(intent({ params: { padding: '' } })))) } })
    expect(bytes(entries(prepared))).toBe(262145)
    expect((await client.catalog({ intents: [prepared] })).reason).toBe('SIP_R7_REQUEST_INVALID')
    expect(fetchImpl).not.toHaveBeenCalled()
  })
  it('R7-49 a server reason outside the code grammar reads HTTP_ERROR in outcomes and the snapshot', async () => {
    for (const reason of ['failure ' + token(), token(), 'a.b', 'not a code', 'X'.repeat(129), '9LEADING', 'trailing\n']) {
      const envelope = refusal(reason)
      const { client, fetchImpl } = setup(envelope, 403)
      const read = await client.readContext()
      expect(read).toMatchObject({ ok: false, status: 403, reason: 'HTTP_ERROR' })
      fetchImpl.mockResolvedValueOnce(response(envelope, 403))
      const take = await client.acquireCheckout()
      expect(take).toMatchObject({ ok: false, status: 403, reason: 'HTTP_ERROR' })
      expect(client.getSnapshot().error).toBe('HTTP_ERROR')
      for (const value of [read, take, client.getSnapshot()]) expect(JSON.stringify(value)).not.toContain(token())
    }
    const longest = 'A' + '_'.repeat(127)
    expect((await setup(refusal(longest), 403).client.readContext()).reason).toBe(longest)
    expect((await setup({ reason_code: 'outer ' + token() }, 409).client.readContext()).reason).toBe('HTTP_ERROR')
    const { client, fetchImpl } = setup()
    fetchImpl.mockResolvedValueOnce({ ok: false, status: 500, json: async () => { throw new SyntaxError('not json') } })
    expect(await client.readContext()).toEqual({ ok: false, status: 500, reason: 'HTTP_ERROR', envelope: null })
  })
  it('R7-50 an artifact header carrying a capability is dropped and the other headers are kept', async () => {
    const { client, fetchImpl } = setup()
    fetchImpl.mockResolvedValueOnce(new Response(new Uint8Array([1, 2, 3]), { headers: {
      'Content-Type': 'text/csv', 'Content-Disposition': `attachment; filename="${token()}.csv"`,
      'X-Leaf-Artifact-Id': 'c'.repeat(64), 'X-Content-Type-Options': 'nosniff' } }))
    const outcome = await client.downloadArtifact(artifact())
    expect(outcome.ok).toBe(true)
    expect(outcome.data.headers).toEqual({ 'Content-Type': 'text/csv', 'X-Leaf-Artifact-Id': 'c'.repeat(64), 'X-Content-Type-Options': 'nosniff' })
    expect(JSON.stringify(outcome.data.headers)).not.toContain(token())
    fetchImpl.mockResolvedValueOnce(new Response(new Uint8Array([1, 2, 3]), { headers: {
      'Content-Disposition': 'attachment; filename="strings.csv"' } }))
    expect((await client.downloadArtifact(artifact())).data.headers).toEqual({ 'Content-Disposition': 'attachment; filename="strings.csv"' })
  })
  it('R7-51 the acquisition body is read once and the capability comes from that one read', async () => {
    let reads = 0
    let run
    const body = acquired()
    delete body.checkout_capability
    Object.defineProperty(body, 'checkout_capability', { enumerable: true, get() {
      reads += 1
      if (reads === 2) run.client.setScope({ ...binding, project_id: J }, 'session-2')
      return token()
    } })
    run = setup(body)
    expect((await run.client.acquireCheckout()).ok).toBe(true)
    expect(reads).toBe(1)
    expect(run.client.getSnapshot().binding.project_id).toBe(P)
    expect(run.client.getSnapshot().ownsCheckout).toBe(true)
    run.fetchImpl.mockResolvedValueOnce(response({ families: [] }))
    expect((await run.client.catalog({ intents: [run.intent()] })).ok).toBe(true)
    expect(init(run.fetchImpl).headers['X-Checkout-Capability']).toBe(token())
    expect(reads).toBe(1)
  })
  it('R7-52 a scope change while the acquisition body is read installs no capability in either scope', async () => {
    let run
    const body = acquired()
    delete body.checkout_capability
    Object.defineProperty(body, 'checkout_capability', { enumerable: true, get() {
      run.client.setScope({ ...binding, project_id: J }, 'session-2')
      return token()
    } })
    run = setup(body)
    expect(await run.client.acquireCheckout()).toEqual({ ok: false, status: null, reason: 'SIP_R7_SCOPE_STALE', envelope: null })
    expect(run.client.getSnapshot()).toMatchObject({ ownsCheckout: false, pending: false, checkout: null })
    expect(run.client.getSnapshot().binding.project_id).toBe(J)
    run.fetchImpl.mockResolvedValueOnce(response({ families: [] }))
    expect((await run.client.catalog({ intents: [run.intent()] })).ok).toBe(true)
    expect(init(run.fetchImpl).headers).not.toHaveProperty('X-Checkout-Capability')
    expect(JSON.stringify(run.client.getSnapshot())).not.toContain(token())
  })
  it('R7-53 a response object with a custom prototype is refused before any of its getters run', async () => {
    let reads = 0
    const shaped = (run, fields, key) => {
      const value = Object.assign(Object.create({}), fields)
      Object.defineProperty(value, key, { enumerable: true, get() {
        reads += 1
        run.client.setScope(null, null)
        return 'complete'
      } })
      return value
    }
    const cases = [
      [(run) => shaped(run, job(), 'status'), (run) => run.client.readJob(J, { tool: 'solar-settings' })],
      [(run) => shaped(run, job(), 'status'), (run) => run.client.observeJob(J, { tool: 'solar-settings', onStatus: run.onStatus })],
      [(run) => ({ families: [shaped(run, { capabilities: [] }, 'family_id')] }), (run) => run.client.catalog()],
      [(run) => shaped(run, contextData, 'intake'), (run) => run.client.readContext()],
    ]
    for (const [data, invoke] of cases) {
      const run = setup()
      run.onStatus = vi.fn()
      run.fetchImpl.mockResolvedValueOnce(response(data(run)))
      expect(await invoke(run)).toEqual({ ok: false, status: null, reason: 'SIP_R7_RESPONSE_INVALID', envelope: null })
      expect(reads).toBe(0)
      expect(run.onStatus).not.toHaveBeenCalled()
      expect(run.client.getSnapshot().binding).toMatchObject(binding)
    }
  })
  it('reads fresh authentication and reports only a current unauthorized response', async () => {
    let auth = 'Bearer first'
    const { client, fetchImpl, onUnauthorized } = setup(contextData, 200, { authHeaders: () => ({ Authorization: auth }) })
    await client.readContext()
    expect(init(fetchImpl).headers.Authorization).toBe('Bearer first')
    auth = 'Bearer second'
    fetchImpl.mockResolvedValueOnce(response(refusal('UNAUTHENTICATED'), 401))
    const outcome = await client.readContext()
    expect(outcome.status).toBe(401)
    expect(init(fetchImpl).headers.Authorization).toBe('Bearer second')
    expect(onUnauthorized).toHaveBeenCalledWith(expect.objectContaining({ status: 401 }), 'Bearer second')
  })
  it('timeout settles a mutation without retrying a fetch that ignores abort', async () => {
    vi.useFakeTimers()
    const { client, fetchImpl } = setup(acquired(), 200, { timeoutMs: 100 })
    fetchImpl.mockReturnValueOnce(new Promise(() => {}))
    const pending = client.acquireCheckout()
    await vi.advanceTimersByTimeAsync(100)
    expect((await pending).reason).toBe('SIP_R7_TIMEOUT')
    expect(fetchImpl).toHaveBeenCalledOnce()
    expect(client.getSnapshot().pending).toBe(false)
    expect(client.getSnapshot().ownsCheckout).toBe(false)
  })
  it('caller abort is contained and a subsequent context read remains usable', async () => {
    const { client, fetchImpl } = setup()
    const controller = new AbortController()
    fetchImpl.mockReturnValueOnce(new Promise(() => {}))
    const pending = client.readContext({ signal: controller.signal })
    controller.abort()
    expect((await pending).reason).toBe('SIP_R7_ABORTED')
    expect((await client.readContext()).ok).toBe(true)
    expect(fetchImpl).toHaveBeenCalledTimes(2)
  })
  it('invalid JSON parameters and malformed options refuse without dispatch', async () => {
    const { client, fetchImpl } = setup()
    const accessor = {}; Object.defineProperty(accessor, 'value', { enumerable: true, get: () => 1 })
    const cycle = {}; cycle.child = cycle
    for (const params of [accessor, cycle, { value: undefined }, { value: Infinity }, { authorization: 'x' }, { drawing_id: J }]) {
      expect(() => client.prepareRun({ tool: 'solar-settings', params, catalogDigest: digest, idempotencyKey: 'one' })).toThrow(TypeError)
    }
    for (const promise of [client.readContext(null), client.catalog(null), client.acquireCheckout({ ttlS: true }),
      client.releaseCheckout(null), client.readSolar(null), client.downloadArtifact(artifact(), null),
      client.readJob(J, null), client.observeJob(J, null)]) {
      expect((await promise).reason).toBe('SIP_R7_REQUEST_INVALID')
    }
    expect(fetchImpl).not.toHaveBeenCalled()
  })
  it('acquire can complete across a version-only switch but does not accept integer fences', async () => {
    const { client, fetchImpl } = setup()
    const waiting = deferred()
    fetchImpl.mockReturnValueOnce(waiting.promise)
    const pending = client.acquireCheckout()
    const signal = init(fetchImpl).signal
    client.setScope({ ...binding, input_version_id: J }, 'session-1')
    expect(signal.aborted).toBe(false)
    waiting.resolve(response(acquired()))
    expect((await pending).ok).toBe(true)
    expect(client.getSnapshot().ownsCheckout).toBe(true)
    fetchImpl.mockResolvedValueOnce(response({ ...acquired(), checkout: { ...lease, fence: 1 } }))
    expect((await client.acquireCheckout()).reason).toBe('SIP_R7_RESPONSE_INVALID')
  })
  it('lease expiry clears proof and invalidates pending previews synchronously', async () => {
    vi.useFakeTimers()
    const { client, fetchImpl, intent } = setup({ ...acquired(), checkout: { ...lease, expires: new Date(Date.now() + 500).toISOString() } })
    await client.acquireCheckout()
    const before = client.getSnapshot().leaseRevision
    fetchImpl.mockReturnValueOnce(new Promise(() => {}))
    const pending = client.catalog({ intents: [intent()] })
    const signal = init(fetchImpl).signal
    await vi.advanceTimersByTimeAsync(500)
    expect(client.getSnapshot().ownsCheckout).toBe(false)
    expect(client.getSnapshot().leaseRevision).toBeGreaterThan(before)
    expect(signal.aborted).toBe(true)
    expect((await pending).reason).toBe('SIP_R7_SCOPE_STALE')
  })
  it('R7-03 unchanged run builder retains O P K C and distinct drawing identities', () => {
    const built = createRunSubmissionRequest('solar-settings', { drawing_id: D }, V, {
      orgId: O, projectId: P, idempotencyKey: 'request-1', catalogDigest: digest, checkoutCapability: token() })
    expect(built.headers).toEqual({ 'X-Org-Id': O, 'X-Project-Id': P, 'Idempotency-Key': 'request-1', 'X-Checkout-Capability': token() })
    expect(built.body).toEqual({ tool: 'solar-settings', params: { drawing_id: D }, dwg: V, catalog_digest: digest })
  })
  it('R7-07 context uses the exact R1 route query and authenticated scope headers', async () => {
    const { client, fetchImpl } = setup()
    expect(await client.readContext()).toEqual({ ok: true, status: 200, data: contextData })
    expect(url(fetchImpl)).toBe(`https://api.test/api/projects/${P}/drawing-versions/${V}/context?drawing_id=${D}`)
    expect(init(fetchImpl)).toMatchObject({ method: 'GET', cache: 'no-store', headers: {
      Authorization: 'Bearer synthetic', 'X-Org-Id': O, 'X-Project-Id': P, 'X-Tenant-Id': O } })
    expect(init(fetchImpl)).not.toHaveProperty('body')
  })
  it('R7-08 catalog sends full binding and only the three exact preview fields', async () => {
    const { client, fetchImpl, intent } = setup(acquired())
    await client.acquireCheckout()
    fetchImpl.mockResolvedValue(response({ families: [] }))
    const prepared = intent()
    const result = await client.catalog({ intents: [prepared] })
    expect(result.ok).toBe(true)
    const entry = { 'solar-settings': { params: prepared.params, catalog_digest: digest, idempotency_key: 'request-1' } }
    expect(url(fetchImpl)).toBe(`https://api.test/api/capabilities?drawing_id=${D}&project_id=${P}&input_version_id=${V}&project_runs=${encodeURIComponent(JSON.stringify(entry))}`)
    expect(init(fetchImpl).headers['X-Checkout-Capability']).toBe(token())
    expect(init(fetchImpl).headers).not.toHaveProperty('Idempotency-Key')
    expect(url(fetchImpl)).not.toContain(token())
    expect(init(fetchImpl)).not.toHaveProperty('body')
  })
  it('R7-12 an old scope 401 is stale and cannot invoke the unauthorized callback', async () => {
    const { client, fetchImpl, onUnauthorized } = setup()
    const old = deferred()
    fetchImpl.mockReturnValueOnce(old.promise)
    const pending = client.readContext()
    client.setScope({ ...binding, organization_id: J }, 'session-2')
    old.resolve(response(refusal('UNAUTHENTICATED'), 401))
    expect((await pending).reason).toBe('SIP_R7_SCOPE_STALE')
    expect(onUnauthorized).not.toHaveBeenCalled()
  })
  it('R7-13 take defaults privately installs capability and omits it from public data', async () => {
    const { client, fetchImpl } = setup(acquired())
    const result = await client.acquireCheckout()
    expect(result.ok).toBe(true)
    expect(result.data).not.toHaveProperty('checkout_capability')
    expect(JSON.stringify(client.getSnapshot())).not.toContain(token())
    expect(client.getSnapshot().ownsCheckout).toBe(true)
    expect(client.getSnapshot().checkout.fence).toBe(lease.fence)
    expect(url(fetchImpl)).toBe(`https://api.test/api/projects/${P}/drawings/${D}/checkout`)
    expect(init(fetchImpl).body).toBe('{"ttl_s":3600}')
    expect(init(fetchImpl).method).toBe('POST')
    expect(init(fetchImpl).headers).not.toHaveProperty('X-Checkout-Capability')
  })
  it('R7-14 renewal sends current proof replaces it and invalidates pending preview', async () => {
    const { client, fetchImpl, intent } = setup(acquired())
    await client.acquireCheckout()
    const before = client.getSnapshot().leaseRevision
    const old = deferred()
    fetchImpl.mockReturnValueOnce(old.promise)
    const pending = client.catalog({ intents: [intent()] })
    const previewSignal = init(fetchImpl).signal
    const nextToken = 'lco1.' + 'c'.repeat(64)
    fetchImpl.mockResolvedValueOnce(response({ ...acquired(), checkout_capability: nextToken }))
    expect((await client.acquireCheckout({ holder: 'editor', ttlS: 100 })).ok).toBe(true)
    expect(init(fetchImpl).headers['X-Checkout-Capability']).toBe(token())
    expect(client.getSnapshot().leaseRevision).toBeGreaterThan(before)
    expect(previewSignal.aborted).toBe(true)
    old.resolve(response({ families: [] }))
    expect((await pending).reason).toBe('SIP_R7_SCOPE_STALE')
    fetchImpl.mockResolvedValueOnce(response({ families: [] }))
    await client.catalog()
    expect(init(fetchImpl).headers).not.toHaveProperty('X-Checkout-Capability')
    fetchImpl.mockResolvedValueOnce(response({ families: [] }))
    await client.catalog({ intents: [intent()] })
    expect(init(fetchImpl).headers['X-Checkout-Capability']).toBe(nextToken)
  })
  it('R7-15 acquire and release overlap returns busy with only one mutation dispatched', async () => {
    const { client, fetchImpl } = setup(acquired())
    const waiting = deferred()
    fetchImpl.mockReturnValueOnce(waiting.promise)
    const taking = client.acquireCheckout()
    expect((await client.releaseCheckout()).reason).toBe('SIP_R7_CHECKOUT_BUSY')
    expect(fetchImpl).toHaveBeenCalledOnce()
    waiting.resolve(response(acquired()))
    expect((await taking).ok).toBe(true)
  })
  it('R7-16 version switch discards reads and readiness while preserving checkout', async () => {
    const { client, fetchImpl, intent } = setup(acquired())
    await client.acquireCheckout()
    const prepared = intent()
    const old = deferred()
    fetchImpl.mockReturnValueOnce(old.promise)
    const read = client.readContext()
    const readSignal = init(fetchImpl).signal
    client.setScope({ ...binding, input_version_id: J }, 'session-1')
    expect(readSignal.aborted).toBe(true)
    expect(client.getSnapshot().ownsCheckout).toBe(true)
    old.resolve(response(contextData))
    expect((await read).reason).toBe('SIP_R7_SCOPE_STALE')
    expect((await client.catalog({ intents: [prepared] })).reason).toBe('SIP_R7_REQUEST_INVALID')
    fetchImpl.mockResolvedValueOnce(response({ ...identity, released: true, checkout: null }))
    await client.releaseCheckout()
    expect(init(fetchImpl).headers['X-Checkout-Capability']).toBe(token())
  })
  it('R7-17 project artifact or principal switch discards a late take without automatic release', async () => {
    for (const [next, authority] of [[{ ...binding, project_id: J }, 'session-1'],
      [{ ...binding, drawing_id: J }, 'session-1'], [binding, 'session-2']]) {
      const { client, fetchImpl } = setup()
      const old = deferred()
      fetchImpl.mockReturnValueOnce(old.promise)
      const pending = client.acquireCheckout()
      client.setScope(next, authority)
      expect(client.getSnapshot().ownsCheckout).toBe(false)
      old.resolve(response(acquired()))
      expect((await pending).reason).toBe('SIP_R7_SCOPE_STALE')
      expect(client.getSnapshot().ownsCheckout).toBe(false)
      expect(fetchImpl).toHaveBeenCalledOnce()
      expect(client.getSnapshot().pending).toBe(false)
    }
  })
  it('R7-18 explicit release sends C with no body key and clears valid ownership', async () => {
    const { client, fetchImpl } = setup(acquired())
    await client.acquireCheckout()
    fetchImpl.mockResolvedValueOnce(response({ ...identity, released: true, checkout: null }))
    expect((await client.releaseCheckout()).ok).toBe(true)
    expect(init(fetchImpl).method).toBe('DELETE')
    expect(init(fetchImpl).headers['X-Checkout-Capability']).toBe(token())
    expect(Object.hasOwn(init(fetchImpl), 'body')).toBe(false)
    expect(client.getSnapshot().ownsCheckout).toBe(false)
  })
  it('R7-19 a new write without a lease retains the server checkout-required refusal', async () => {
    const envelope = refusal('SIP_R1_CHECKOUT_REQUIRED')
    const { client, fetchImpl, intent } = setup(envelope, 409)
    expect(await client.submitRun(intent())).toEqual({ ok: false, status: 409, reason: 'SIP_R1_CHECKOUT_REQUIRED', envelope })
    expect(init(fetchImpl).headers).not.toHaveProperty('X-Checkout-Capability')
  })
  it('R7-20 a live server lease without proof retains checkout-denied rather than inventing ownership', async () => {
    const { client, fetchImpl, intent } = setup({ ...identity, checkout: lease })
    await client.readCheckout()
    expect(client.getSnapshot().ownsCheckout).toBe(false)
    const envelope = refusal('SIP_R1_CHECKOUT_DENIED')
    fetchImpl.mockResolvedValueOnce(response(envelope, 403))
    expect(await client.submitRun(intent())).toEqual({ ok: false, status: 403, reason: 'SIP_R1_CHECKOUT_DENIED', envelope })
    expect(init(fetchImpl).headers).not.toHaveProperty('X-Checkout-Capability')
  })
  it('R7-21 exact idempotent replay after lease loss retains K and request without invented C', async () => {
    const { client, fetchImpl, intent } = setup(acquired())
    await client.acquireCheckout()
    const prepared = intent()
    fetchImpl.mockResolvedValueOnce(response({ job_id: J, status: 'submitted' }, 202))
    expect((await client.submitRun(prepared)).ok).toBe(true)
    const first = init(fetchImpl)
    fetchImpl.mockResolvedValueOnce(response({ ...identity, checkout: null }))
    await client.readCheckout()
    fetchImpl.mockResolvedValueOnce(response({ job_id: J, status: 'submitted' }, 202))
    expect((await client.submitRun(prepared)).ok).toBe(true)
    expect(init(fetchImpl).body).toBe(first.body)
    expect(init(fetchImpl).headers['Idempotency-Key']).toBe(first.headers['Idempotency-Key'])
    expect(init(fetchImpl).headers).not.toHaveProperty('X-Checkout-Capability')
  })
  it('R7-22 same key changed intent preserves the server idempotency conflict', async () => {
    const envelope = refusal('SIP_R4_IDEMPOTENCY_CONFLICT')
    const { client, intent, fetchImpl } = setup(envelope, 409)
    expect(await client.submitRun(intent({ params: { expected_rev: 1 } }))).toEqual({ ok: false, status: 409, reason: 'SIP_R4_IDEMPOTENCY_CONFLICT', envelope })
    expect(init(fetchImpl).headers['Idempotency-Key']).toBe('request-1')
  })
  it('R7-23 valid project write pins V in dwg D in params and never dwg_version', async () => {
    const { client, fetchImpl, intent } = setup({ job_id: J, status: 'submitted' }, 202)
    expect((await client.submitRun(intent())).ok).toBe(true)
    expect(url(fetchImpl)).toBe('https://api.test/api/run')
    expect(JSON.parse(init(fetchImpl).body)).toEqual({ tool: 'solar-settings', params: { drawing_id: D, expected_rev: 0 }, dwg: V, catalog_digest: digest })
    expect(init(fetchImpl).headers).toMatchObject({ 'X-Org-Id': O, 'X-Project-Id': P, 'Idempotency-Key': 'request-1' })
  })
  it('R7-24 serial authenticated observation stops at a scope switch without stale callbacks', async () => {
    vi.useFakeTimers()
    const { client, fetchImpl } = setup(job({ status: 'running' }))
    const old = deferred()
    fetchImpl.mockResolvedValueOnce(response(job({ status: 'running' }))).mockReturnValueOnce(old.promise)
    const onStatus = vi.fn()
    const pending = client.observeJob(J, { tool: 'solar-settings', onStatus, intervalMs: 250 })
    await vi.advanceTimersByTimeAsync(0)
    expect(onStatus).toHaveBeenCalledOnce()
    expect(fetchImpl).toHaveBeenCalledOnce()
    await vi.advanceTimersByTimeAsync(250)
    expect(fetchImpl).toHaveBeenCalledTimes(2)
    expect(url(fetchImpl)).toBe(`https://api.test/api/jobs/${J}`)
    expect(init(fetchImpl).headers.Authorization).toBe('Bearer synthetic')
    client.setScope({ ...binding, input_version_id: J }, 'session-1')
    old.resolve(response(job()))
    expect((await pending).reason).toBe('SIP_R7_SCOPE_STALE')
    expect(onStatus).toHaveBeenCalledOnce()
    await vi.advanceTimersByTimeAsync(1000)
    expect(fetchImpl).toHaveBeenCalledTimes(2)
  })
  it('R7-25 wrong stored D V tool or graph schema refuses job publication', async () => {
    const { client, fetchImpl } = setup()
    for (const data of [job({ input_version_id: J }), job({ tool_name: 'solar-other' }),
      job({ execution_context: { schema: 'leaf.solar-project-graph-job.v1', drawing_id: J } }),
      job({ execution_context: { schema: 'wrong', drawing_id: D } })]) {
      fetchImpl.mockResolvedValueOnce(response(data))
      const onStatus = vi.fn()
      expect((await client.observeJob(J, { tool: 'solar-settings', onStatus })).reason).toBe('SIP_R7_RESPONSE_INVALID')
      expect(onStatus).not.toHaveBeenCalled()
    }
  })
  it('R7-26 historical R6 read uses version POST without C or K and accepts matching result', async () => {
    const { client, fetchImpl } = setup(acquired())
    await client.acquireCheckout()
    fetchImpl.mockResolvedValueOnce(response(readData))
    const outcome = await client.readSolar({ tool: 'solar-string-data', params: { zone: 'one' }, catalogDigest: digest, current: false })
    expect(outcome).toEqual({ ok: true, status: 200, data: readData })
    expect(url(fetchImpl)).toBe(`https://api.test/api/projects/${P}/drawings/${D}/versions/${V}/solar-reads`)
    expect(JSON.parse(init(fetchImpl).body)).toEqual({ tool: 'solar-string-data', params: { drawing_id: D, zone: 'one' }, catalog_digest: digest, current: false })
    expect(init(fetchImpl).headers).not.toHaveProperty('X-Checkout-Capability')
    expect(init(fetchImpl).headers).not.toHaveProperty('Idempotency-Key')
  })
  it('R7-27 current R6 read retains stale-current server refusal', async () => {
    const envelope = refusal('SIP_R6_STALE_CURRENT')
    const { client } = setup(envelope, 409)
    expect(await client.readSolar({ tool: 'solar-string-data', catalogDigest: digest, current: true })).toEqual({ ok: false, status: 409, reason: 'SIP_R6_STALE_CURRENT', envelope })
  })
  it('R7-28 disabled R6 read retains project_execution_disabled without fallback', async () => {
    const envelope = refusal('project_execution_disabled')
    const { client, fetchImpl } = setup(envelope, 409)
    expect(await client.readSolar({ tool: 'solar-string-data', catalogDigest: digest })).toEqual({ ok: false, status: 409, reason: 'project_execution_disabled', envelope })
    expect(fetchImpl).toHaveBeenCalledOnce()
    expect(url(fetchImpl)).toContain('/solar-reads')
  })
  it('R7-29 a read body of 262145 UTF-8 bytes refuses locally without fetch', async () => {
    const { client, fetchImpl } = setup()
    const base = JSON.stringify({ tool: 'solar-string-data', params: { drawing_id: D, padding: '' }, catalog_digest: digest, current: false })
    const padding = 'x'.repeat(262145 - new TextEncoder().encode(base).byteLength)
    const request = { tool: 'solar-string-data', params: { padding }, catalogDigest: digest }
    expect((await client.readSolar(request)).reason).toBe('SIP_R7_REQUEST_INVALID')
    expect(fetchImpl).not.toHaveBeenCalled()
  })
  it('R7-30 foreign artifact version download missing and extra reference fields refuse locally', async () => {
    const { client, fetchImpl } = setup()
    const missing = artifact(); delete missing.graph_sha256
    const accessor = artifact(); Object.defineProperty(accessor, 'filename', { get: () => 'strings.csv' })
    for (const ref of [artifact({ source_version_id: J }), artifact({ download: 'https://foreign.test/data' }),
      artifact({ extra: true }), missing, accessor, artifact({ graph_sha256: 'G'.repeat(64) }), artifact({ byte_length: true })]) {
      expect((await client.downloadArtifact(ref)).reason).toBe('SIP_R7_REQUEST_INVALID')
    }
    expect(fetchImpl).not.toHaveBeenCalled()
  })
  it('R7-31 artifact 200 returns bounded transport bytes and selected headers', async () => {
    const { client, fetchImpl } = setup()
    fetchImpl.mockResolvedValueOnce(new Response(new Uint8Array([1, 2, 3]), { headers: {
      'Content-Type': 'text/csv', ETag: '"' + 'f'.repeat(64) + '"', 'X-Leaf-Artifact-Id': 'c'.repeat(64), 'X-Secret': 'excluded' } }))
    const outcome = await client.downloadArtifact(artifact())
    expect(outcome.ok).toBe(true)
    expect(Array.from(outcome.data.bytes)).toEqual([1, 2, 3])
    expect(outcome.data.headers).toEqual({ 'Content-Type': 'text/csv', ETag: '"' + 'f'.repeat(64) + '"', 'X-Leaf-Artifact-Id': 'c'.repeat(64) })
    expect(url(fetchImpl)).toBe(`https://api.test${artifact().download}?current=false`)
    expect(init(fetchImpl)).not.toHaveProperty('body')
    expect(init(fetchImpl).headers).not.toHaveProperty('X-Checkout-Capability')
  })
  it('R7-32 matching ETag 304 returns notModified and null bytes without reading body', async () => {
    const { client, fetchImpl } = setup()
    const json = vi.fn(); const arrayBuffer = vi.fn()
    fetchImpl.mockResolvedValueOnce({ ok: false, status: 304, json, arrayBuffer })
    expect(await client.downloadArtifact(artifact(), { ifNoneMatch: '"' + 'f'.repeat(64) + '"' })).toEqual({ ok: true, status: 304, data: { notModified: true, bytes: null } })
    expect(init(fetchImpl).headers['If-None-Match']).toBe('"' + 'f'.repeat(64) + '"')
    expect(json).not.toHaveBeenCalled(); expect(arrayBuffer).not.toHaveBeenCalled()
  })
  it('R7-33 oversize or late artifact streams refuse without publishing bytes', async () => {
    const { client, fetchImpl } = setup()
    let count = 0
    const cancel = vi.fn()
    const stream = new ReadableStream({ pull(controller) {
      count += 1
      controller.enqueue(new Uint8Array(1024 * 1024))
      if (count === 17) controller.close()
    }, cancel })
    fetchImpl.mockResolvedValueOnce(new Response(stream))
    expect((await client.downloadArtifact(artifact())).reason).toBe('SIP_R7_RESPONSE_INVALID')
    const waiting = deferred()
    const read = vi.fn(() => waiting.promise)
    const reader = { read, cancel: vi.fn(async () => {}), releaseLock: vi.fn() }
    fetchImpl.mockResolvedValueOnce({ ok: true, status: 200, headers: new Headers(), body: { getReader: () => reader } })
    const pending = client.downloadArtifact(artifact())
    await Promise.resolve(); await Promise.resolve(); await Promise.resolve()
    client.setScope({ ...binding, input_version_id: J }, 'session-1')
    waiting.resolve({ done: true })
    const outcome = await pending
    expect(outcome.reason).toBe('SIP_R7_SCOPE_STALE')
    expect(outcome).not.toHaveProperty('data')
  })
})
