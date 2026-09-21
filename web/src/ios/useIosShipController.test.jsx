import { act, cleanup, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { useIosShipController, shipSetupState } from './useIosShipController.js'
import { validateIosShipReadiness, fetchIosShipSources as readSources, requestIosShipApproval as postApproval } from '../site/iosShipReadiness.js'
import { readFileSync } from 'node:fs'

// Transport spies: the hook still calls every real fetch helper and validator.
const fetchIosShipReadiness = vi.fn()
const getIosShipExecution = vi.fn()
const getIosShipReceipt = vi.fn()
const requestIosShipLaunch = vi.fn()
const fetchIosShipSources = vi.fn()
const requestIosShipApproval = vi.fn()

const sourceOne = { source_revision: 'a'.repeat(40), source_sha256: 'b'.repeat(64),
  bundle_identifier: 'com.example.app', marketing_version: '1.0', build_number: '12', catalog_key: 'catalog-one' }
const sourceTwo = { ...sourceOne, source_revision: 'c'.repeat(40), build_number: '11' }
const sourceApproval = (source = sourceOne, overrides = {}) => ({ approval_id: 'approval-one', revision: 'r1',
  source_revision: source.source_revision, consumed_at: null, ...overrides })
const sourceCatalog = (overrides = {}) => ({ ok: true, sources: [sourceOne, sourceTwo], approvals: [sourceApproval(sourceTwo)],
  sync: { status: 'ok', registered: 2, conflicts: [], unpinned: [], refused: [] }, can_approve: true, ...overrides })

const props = { projectId: 'p1', revision: 'r1', tenantKey: 'tenant1', sessionActive: true }
const key = 'leaf.ios-ship.pointer:tenant1:p1'
const approvedReadiness = ({ projectId = 'p1', revision = 'r1', ...overrides } = {}) => validateIosShipReadiness({
  record_kind: 'leaf.ios-ship-readiness.v1', project_id: projectId,
  launchable: true, healthy: true, grant_status: 'healthy', dispatch_available: true,
  approved_launch: { approval_id: 'a1', revision, source_revision: 'source1', source_sha256: 'a'.repeat(64),
    bundle_identifier: 'com.leaf.test', marketing_version: '1.0', build_number: '12' },
  ...overrides,
}, { projectId, revision })
const emptyIosShipReadiness = (reason) => approvedReadiness({ launchable: false, healthy: false, reason })
const transport = async (url, init) => {
  const path = new URL(url, 'http://localhost')
  let data
  if (path.pathname.endsWith('/ios/sources')) {
    data = await fetchIosShipSources({ projectId: decodeURIComponent(path.pathname.split('/')[3]) })
  } else if (path.pathname.endsWith('/ios/approvals')) {
    data = await requestIosShipApproval(JSON.parse(init.body))
    if (data?.error) return { ok: false, status: data.status || 409, json: async () => data }
  } else if (path.pathname.endsWith('/readiness')) {
    const readiness = await fetchIosShipReadiness({ projectId: path.searchParams.get('project_id'), revision: path.searchParams.get('revision') })
    data = { readiness: { record_kind: readiness.kind, project_id: readiness.projectId,
      launchable: readiness.launchable, healthy: readiness.healthy, grant_status: readiness.grantStatus,
      dispatch_available: readiness.dispatchAvailable, approved_launch: readiness.approvedLaunch,
      reason: readiness.reason, setup_action: readiness.setupAction } }
  } else if (path.pathname.endsWith('/launch')) {
    const { project_id: projectId, ...approvedLaunch } = JSON.parse(init.body)
    data = { ok: true, ...await requestIosShipLaunch({ projectId, approvedLaunch, idempotencyKey: init.headers['Idempotency-Key'] }) }
  } else if (path.pathname.includes('/executions/')) {
    data = { ok: true, ...await getIosShipExecution({ projectId: path.searchParams.get('project_id'), executionId: decodeURIComponent(path.pathname.split('/').pop()) }) }
  } else if (path.pathname.includes('/receipts/')) {
    data = { ok: true, ...await getIosShipReceipt({ projectId: path.searchParams.get('project_id'), receiptId: decodeURIComponent(path.pathname.split('/').pop()) }) }
  } else throw new Error(`Unexpected request: ${path.pathname}`)
  return { ok: true, status: path.pathname.endsWith('/launch') ? 202 : 200, json: async () => data }
}
const queued = { execution_id: 'e1', status: 'queued' }
const receipt = { kind: 'leaf.ios-testflight-receipt.v1', receipt_id: 'receipt1', build_number: '12' }
const flush = async () => { await act(async () => { await Promise.resolve() }) }
const tick = async (ms = 2000) => { await act(async () => { await vi.advanceTimersByTimeAsync(ms) }) }
const mount = () => renderHook((input) => useIosShipController(input), { initialProps: props })
const launch = async (result) => { await act(async () => { await result.current.launch() }) }

it('J2 row6 switching the served profile preserves execution ownership and resumes following', async () => {
  const app = readFileSync(`${process.cwd()}/src/App.jsx`, 'utf8')
  expect(app).toContain("const shipControllerLive = ENV_IOS_SURFACE && surfaceSlots.toolbar.profile === 'ship' && !mock && signedIn")
  expect(app).toContain('enabled: shipControllerLive')
  expect(app).toContain('ship={ship}')
  const { result, rerender } = renderHook((input) => useIosShipController(input), { initialProps: { ...props, enabled: true } })
  await flush()
  await launch(result)
  const execution = result.current.execution
  expect(execution.execution_id).toBe('e1')
  rerender({ ...props, enabled: false })
  await flush()
  expect(result.current.phase).toBe('idle')
  expect(result.current.execution).toBe(execution)
  const reads = getIosShipExecution.mock.calls.length
  await tick(4000)
  expect(getIosShipExecution).toHaveBeenCalledTimes(reads)
  rerender({ ...props, enabled: true })
  await flush()
  expect(result.current.phase).toBe('running')
  expect(result.current.execution.execution_id).toBe('e1')
  getIosShipExecution.mockResolvedValue({ execution: { ...queued, status: 'succeeded' } })
  await tick()
  expect(result.current.phase).toBe('succeeded')
})

beforeEach(() => {
  vi.useFakeTimers()
  vi.resetAllMocks()
  sessionStorage.clear()
  vi.stubGlobal('fetch', vi.fn(transport))
  fetchIosShipReadiness.mockResolvedValue(approvedReadiness())
  requestIosShipLaunch.mockResolvedValue({ ok: true, execution: queued })
  getIosShipExecution.mockResolvedValue({ execution: queued })
  getIosShipReceipt.mockResolvedValue({ receipt })
  fetchIosShipSources.mockResolvedValue(sourceCatalog())
  requestIosShipApproval.mockResolvedValue({ ok: true, approval: sourceApproval() })
})
afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); sessionStorage.clear() })

it('B4D row1 reads the project catalog once across drawing version changes', async () => {
  const { result, rerender } = mount()
  await flush()
  expect(fetchIosShipSources).toHaveBeenCalledTimes(1)
  expect(result.current.sources).toEqual([sourceOne, sourceTwo])
  expect(result.current.approvals).toEqual([sourceApproval(sourceTwo)])
  expect(result.current.sync.status).toBe('ok')
  expect(result.current.canApprove).toBe(true)
  rerender({ ...props, revision: 'r2' })
  await flush()
  expect(fetchIosShipSources).toHaveBeenCalledTimes(1)
  expect(result.current.sources).toEqual([sourceOne, sourceTwo])
})

it('B4D row2 inactive or missing project scopes never read the catalog', async () => {
  for (const patch of [{ enabled: false }, { sessionActive: false }, { projectId: null }]) {
    const hook = renderHook(() => useIosShipController({ ...props, ...patch }))
    await flush()
    expect(hook.result.current).toMatchObject({ sources: [], approvals: [], sync: null, canApprove: false, sourcesError: null })
    hook.unmount()
  }
  expect(fetchIosShipSources).not.toHaveBeenCalled()
})

it('B4D row3 submits only the catalog tuple and refreshes sources and readiness', async () => {
  let resolveApproval
  requestIosShipApproval.mockReturnValueOnce(new Promise((resolve) => { resolveApproval = resolve }))
  const { result } = mount()
  await flush()
  let pending
  act(() => { pending = result.current.approve(sourceOne.source_revision) })
  expect(result.current.approving).toBe(true)
  expect(result.current.busy).toBe(false)
  expect(requestIosShipApproval).toHaveBeenCalledTimes(1)
  expect(requestIosShipApproval).toHaveBeenCalledWith({ revision: 'r1', source_revision: sourceOne.source_revision,
    source_sha256: sourceOne.source_sha256, bundle_identifier: sourceOne.bundle_identifier,
    marketing_version: sourceOne.marketing_version, build_number: sourceOne.build_number })
  await act(async () => {
    resolveApproval({ ok: true, approval: sourceApproval() })
    expect(await pending).toEqual({ approval: sourceApproval(), readBack: false })
  })
  expect(result.current.approving).toBe(false)
  expect(fetchIosShipSources).toHaveBeenCalledTimes(2)
  expect(fetchIosShipReadiness).toHaveBeenCalledTimes(2)
})

it('B4D row4 locks a second synchronous approval before the request settles', async () => {
  let resolveApproval
  requestIosShipApproval.mockReturnValueOnce(new Promise((resolve) => { resolveApproval = resolve }))
  const { result } = mount()
  await flush()
  let first
  let second
  act(() => { first = result.current.approve(sourceOne.source_revision); second = result.current.approve(sourceOne.source_revision) })
  expect(await second).toBeNull()
  expect(requestIosShipApproval).toHaveBeenCalledTimes(1)
  await act(async () => { resolveApproval({ ok: true, approval: sourceApproval() }); await first })
})

it('B4D row5 owner version and catalog membership are required for approval', async () => {
  for (const [canApprove, revision, selected] of [[false, 'r1', sourceOne.source_revision], [true, null, sourceOne.source_revision], [true, 'r1', 'unknown']]) {
    fetchIosShipSources.mockResolvedValue(sourceCatalog({ can_approve: canApprove }))
    const hook = renderHook(() => useIosShipController({ ...props, revision }))
    await flush()
    await act(async () => { expect(await hook.result.current.approve(selected)).toBeUndefined() })
    expect(hook.result.current.approving).toBe(false)
    hook.unmount()
  }
  expect(requestIosShipApproval).not.toHaveBeenCalled()
})

it.each(['approval_consumed', 'approval_tuple_mismatch'])('B4D row6 preserves the %s refusal and rereads sources', async (code) => {
  requestIosShipApproval.mockResolvedValueOnce({ error: { code, message: `Approval refused: ${code}` } })
  const { result } = mount()
  await flush()
  await act(async () => { expect(await result.current.approve(sourceOne.source_revision)).toBeUndefined() })
  expect(result.current.sourcesError).toBe(`Approval refused: ${code}`)
  expect(result.current.approving).toBe(false)
  expect(fetchIosShipSources).toHaveBeenCalledTimes(2)
})

it('B4D row7 reads back a lost approval response before allowing another write', async () => {
  requestIosShipApproval.mockRejectedValueOnce(new Error('connection lost'))
  const { result } = mount()
  await flush()
  fetchIosShipSources.mockResolvedValue(sourceCatalog({ approvals: [sourceApproval()] }))
  await act(async () => { expect(await result.current.approve(sourceOne.source_revision)).toEqual({ approval: null, readBack: true }) })
  expect(result.current.sourcesError).toBeNull()
  expect(result.current.approvals).toEqual([sourceApproval()])
  expect(result.current.approving).toBe(false)
  expect(fetchIosShipReadiness).toHaveBeenCalledTimes(2)
  await act(async () => { expect(await result.current.approve(sourceOne.source_revision)).toBeUndefined() })
  expect(requestIosShipApproval).toHaveBeenCalledTimes(1)
})

it('B4D row7b permits a retry only after a readback with no matching approval', async () => {
  requestIosShipApproval.mockRejectedValueOnce(new Error('connection lost'))
  const { result } = mount()
  await flush()
  await act(async () => { await result.current.approve(sourceOne.source_revision) })
  expect(result.current.sourcesError).toBe('connection lost')
  expect(result.current.approving).toBe(false)
  expect(fetchIosShipSources).toHaveBeenCalledTimes(2)
  await act(async () => { await result.current.approve(sourceOne.source_revision) })
  expect(requestIosShipApproval).toHaveBeenCalledTimes(2)
})

it('B4D row8 an old approval reply cannot write into the next project', async () => {
  let rejectApproval
  requestIosShipApproval.mockReturnValueOnce(new Promise((resolve, reject) => { rejectApproval = reject }))
  const { result, rerender } = mount()
  await flush()
  let pending
  act(() => { pending = result.current.approve(sourceOne.source_revision) })
  fetchIosShipSources.mockResolvedValue(sourceCatalog({ sources: [], approvals: [], can_approve: false }))
  rerender({ ...props, projectId: 'p2' })
  await flush()
  await act(async () => { rejectApproval(new Error('old connection lost')); await pending })
  expect(result.current).toMatchObject({ sources: [], approvals: [], canApprove: false, sourcesError: null, approving: false })
  expect(fetchIosShipSources).toHaveBeenCalledTimes(2)
})

it('B4D row9 rejects secret fields and incomplete tuples without exposing their values', async () => {
  const { result } = mount()
  await flush()
  const previous = result.current.sources
  for (const [data, field] of [
    [sourceCatalog({ extra: { api_key: 'do-not-display' } }), 'api_key'],
    [sourceCatalog({ sources: [{ ...sourceOne, build_number: undefined }] }), 'build_number'],
  ]) {
    fetchIosShipSources.mockResolvedValueOnce(data)
    await act(async () => { expect(await result.current.refreshSources()).toBeNull() })
    expect(result.current.sourcesError).toContain(field)
    expect(result.current.sourcesError).not.toContain('do-not-display')
    expect(result.current.sources).toBe(previous)
  }
})

it('B4D row10 accepts the original catalog shape without granting approval', async () => {
  fetchIosShipSources.mockResolvedValue({ ok: true, sources: [sourceOne], approvals: [] })
  const { result } = mount()
  await flush()
  expect(result.current).toMatchObject({ sources: [sourceOne], approvals: [], sync: null, canApprove: false, sourcesError: null })
})

it.each([false, true])('B4D row21 holds an unknown approval until a successful readback, recorded %s', async (recorded) => {
  const { result } = mount()
  await flush()
  requestIosShipApproval.mockRejectedValueOnce(new Error('connection lost'))
  fetchIosShipSources.mockRejectedValueOnce(new Error('catalog unavailable'))
  await act(async () => { await result.current.approve(sourceOne.source_revision) })
  expect(result.current.approving).toBe(false)
  await act(async () => { expect(await result.current.approve(sourceOne.source_revision)).toBeUndefined() })
  expect(requestIosShipApproval).toHaveBeenCalledTimes(1)
  expect(result.current.sourcesError).toBe("The last approval's outcome is unknown. Refresh the sources to confirm it before approving again.")
  fetchIosShipSources.mockResolvedValueOnce(sourceCatalog({ approvals: recorded ? [sourceApproval()] : [] }))
  await act(async () => { await result.current.refreshSources() })
  expect(result.current.sourcesError).toBeNull()
  expect(result.current.approvals).toEqual(recorded ? [sourceApproval()] : [])
  await act(async () => { await result.current.approve(sourceOne.source_revision) })
  expect(requestIosShipApproval).toHaveBeenCalledTimes(recorded ? 1 : 2)
})

it('B4D row22 rejects invalid source grammars by field and retains the previous catalog', async () => {
  const { result } = mount()
  await flush()
  const previousSources = result.current.sources
  const previousApprovals = result.current.approvals
  for (const [field, value] of [
    ['bundle_identifier', '/tmp/build/Example.app'], ['catalog_key', '../../x'],
    ['repository', 'ftp://x'], ['source_revision', 'a'.repeat(41)],
    ['source_sha256', 'g'.repeat(64)], ['marketing_version', '1.0-beta'],
    ['build_number', '12x'], ['imported_at', 'x'.repeat(65)],
    ['source_revision', `${'a'.repeat(40)}\n`], ['repository', 'https://example.test/ bad'],
  ]) {
    const data = sourceCatalog({ sources: [{ ...sourceOne, [field]: value }] })
    fetchIosShipSources.mockResolvedValueOnce(data)
    let failure
    try { await readSources({ projectId: 'p1' }) } catch (cause) { failure = cause }
    expect(failure).toBeInstanceOf(Error)
    expect(failure.message).toContain(field)
    expect(failure.message).not.toContain(value)
    fetchIosShipSources.mockResolvedValueOnce(data)
    await act(async () => { expect(await result.current.refreshSources()).toBeNull() })
    expect(result.current.sourcesError).toContain(field)
    expect(result.current.sourcesError).not.toContain(value)
    expect(result.current.sources).toBe(previousSources)
    expect(result.current.approvals).toBe(previousApprovals)
  }
  await expect(readSources({ projectId: 'p1' })).resolves.toMatchObject({ sources: [sourceOne, sourceTwo] })
})

it('B4D row23 validates approval responses and holds retry while their outcome is unknown', async () => {
  const malformed = { ok: true, approval: sourceApproval(sourceOne, { approval_id: 'x'.repeat(4096) }) }
  requestIosShipApproval.mockResolvedValueOnce(malformed)
  let failure
  try { await postApproval({ projectId: 'p1', revision: 'r1', source: sourceOne }) } catch (cause) { failure = cause }
  expect(failure).toBeInstanceOf(Error)
  expect(failure.message).toContain('approval_id')
  expect(failure.message).not.toContain(malformed.approval.approval_id)
  expect(failure.status).toBeUndefined()
  requestIosShipApproval.mockClear()
  const { result } = mount()
  await flush()
  requestIosShipApproval.mockResolvedValueOnce(malformed)
  let rejectReadBack
  fetchIosShipSources.mockReturnValueOnce(new Promise((resolve, reject) => { rejectReadBack = reject }))
  let pending
  act(() => { pending = result.current.approve(sourceOne.source_revision) })
  await flush()
  expect(fetchIosShipSources).toHaveBeenCalledTimes(2)
  await act(async () => { expect(await result.current.approve(sourceOne.source_revision)).toBeNull() })
  expect(requestIosShipApproval).toHaveBeenCalledTimes(1)
  await act(async () => { rejectReadBack(new Error('catalog unavailable')); await pending })
  expect(result.current.sourcesError).toContain('approval_id')
  await act(async () => { expect(await result.current.approve(sourceOne.source_revision)).toBeUndefined() })
  expect(requestIosShipApproval).toHaveBeenCalledTimes(1)
  fetchIosShipSources.mockResolvedValueOnce(sourceCatalog({ approvals: [sourceApproval()] }))
  await act(async () => { await result.current.refreshSources() })
  await act(async () => { expect(await result.current.approve(sourceOne.source_revision)).toBeUndefined() })
  expect(result.current.sourcesError).toBeNull()
  expect(requestIosShipApproval).toHaveBeenCalledTimes(1)
  requestIosShipApproval.mockResolvedValueOnce({ ok: true, approval: { ...sourceApproval(), extra: 'discard' } })
  await expect(postApproval({ projectId: 'p1', revision: 'r1', source: sourceOne })).resolves.toEqual(sourceApproval())
})

it('I1 row1 maps named setup states and transport failures', async () => {
  for (const [record, phase, setupState] of [
    [emptyIosShipReadiness('no_approved_project_revision'), 'setup-required', 'no-approved-revision'],
    [emptyIosShipReadiness('provider_unavailable'), 'unavailable', 'executor-unavailable'],
    [validateIosShipReadiness({ record_kind: 'leaf.ios-ship-readiness.v1', healthy: false, launchable: false, reason: 'unhealthy', grant_status: 'expired' }, { projectId: 'p1' }), 'setup-required', 'grant-not-ready'],
    [emptyIosShipReadiness('unreachable'), 'unavailable', 'none'],
  ]) {
    fetchIosShipReadiness.mockResolvedValue(record)
    const { result, unmount } = mount()
    await flush()
    expect(result.current.phase).toBe(phase)
    expect(result.current.readiness.setupState).toBe(setupState)
    unmount()
  }
  expect(shipSetupState({ reason: 'mini_busy' })).toBe('executor-busy')
  expect(shipSetupState({ reason: 'revision_mismatch' })).toBe('no-approved-revision')
  expect(shipSetupState({ setupAction: 'renew-grant' })).toBe('grant-not-ready')
})

it('I1 row2 synchronously locks launch before awaiting one request', async () => {
  let resolveLaunch
  requestIosShipLaunch.mockReturnValue(new Promise((resolve) => { resolveLaunch = resolve }))
  const { result } = mount()
  await flush()
  expect(result.current.phase).toBe('ready')
  let first
  act(() => { first = result.current.launch(); result.current.launch() })
  expect(requestIosShipLaunch).toHaveBeenCalledTimes(1)
  expect(result.current.phase).toBe('launching')
  expect(requestIosShipLaunch.mock.calls[0][0].idempotencyKey).toBe('ios-ship:p1:a1')
  await act(async () => { resolveLaunch({ execution: queued }); await first })
  expect(result.current.phase).toBe('running')
  expect(result.current.execution).toEqual(queued)
  expect(JSON.parse(sessionStorage.getItem(key))).toMatchObject({ execution_id: 'e1', revision: 'r1' })
})

it('I1 row3 a slow old project cannot replace the new project or retain its execution', async () => {
  const { result, rerender } = mount()
  await flush()
  requestIosShipLaunch.mockResolvedValue({ execution: { ...queued, status: 'succeeded', receipt_id: 'receipt1' } })
  await launch(result)
  await flush()
  expect(result.current.receipt).toEqual(receipt)
  let resolveOld
  fetchIosShipReadiness.mockReturnValueOnce(new Promise((resolve) => { resolveOld = resolve }))
  act(() => { result.current.refresh() })
  fetchIosShipReadiness.mockResolvedValue(approvedReadiness({ projectId: 'p2' }))
  rerender({ ...props, projectId: 'p2' })
  expect(result.current.execution).toBeNull()
  expect(result.current.receipt).toBeNull()
  await flush()
  await act(async () => { resolveOld(approvedReadiness()) })
  expect(result.current.readiness.projectId).toBe('p2')
  expect(result.current.phase).toBe('ready')
})

it('I1 row4 follows to a receipt and preserves execution on a poll error', async () => {
  const { result } = mount()
  await flush()
  await launch(result)
  getIosShipExecution.mockRejectedValueOnce(new Error('temporary outage'))
  await tick()
  expect(result.current.execution).toEqual(queued)
  expect(result.current.error).toBe('temporary outage')
  getIosShipExecution.mockResolvedValueOnce({ execution: { ...queued, status: 'running', stage: 'BUILT' } })
    .mockResolvedValueOnce({ execution: { ...queued, status: 'succeeded', receipt_id: 'receipt1' } })
  await act(async () => { await result.current.refresh() })
  await tick()
  expect(result.current.execution.status).toBe('running')
  await tick()
  expect(result.current.phase).toBe('succeeded')
  expect(result.current.receipt).toEqual(receipt)
  expect(getIosShipReceipt).toHaveBeenCalledTimes(1)
  expect(sessionStorage.getItem(key)).toBeNull()
})

it('I1 row5 recovers only the tenant and project pointer and clears a 404', async () => {
  sessionStorage.setItem('leaf.ios-ship.pointer:tenant1:other', JSON.stringify({ execution_id: 'other' }))
  sessionStorage.setItem('leaf.ios-ship.pointer:other:p1', JSON.stringify({ execution_id: 'other-tenant' }))
  let hook = mount()
  await flush()
  expect(getIosShipExecution).not.toHaveBeenCalled()
  hook.unmount()
  sessionStorage.setItem(key, JSON.stringify({ execution_id: 'e1', revision: 'r1', at: '2026-09-17' }))
  hook = mount()
  await flush()
  expect(getIosShipExecution).toHaveBeenCalledWith({ projectId: 'p1', executionId: 'e1' })
  expect(hook.result.current.phase).toBe('running')
  await tick()
  expect(getIosShipExecution).toHaveBeenCalledTimes(2)
  hook.unmount()
  getIosShipExecution.mockRejectedValueOnce(Object.assign(new Error('missing'), { status: 404 }))
  hook = mount()
  await flush()
  expect(sessionStorage.getItem(key)).toBeNull()
  expect(hook.result.current.error).toBeNull()
  hook.unmount()
  sessionStorage.setItem(key, JSON.stringify({ execution_id: 'e1', revision: 'r1' }))
  getIosShipExecution.mockRejectedValueOnce(new Error('recovery unavailable'))
  hook = mount()
  await flush()
  expect(sessionStorage.getItem(key)).not.toBeNull()
  expect(hook.result.current.error).toBe('recovery unavailable')
})

it('I1 row6 readiness failure keeps a historical receipt', async () => {
  const { result } = mount()
  await flush()
  requestIosShipLaunch.mockResolvedValue({ execution: { ...queued, status: 'succeeded', receipt_id: 'receipt1' } })
  await launch(result)
  await flush()
  expect(result.current.receipt).toEqual(receipt)
  requestIosShipLaunch.mockRejectedValueOnce(new Error('launch unavailable'))
  await launch(result)
  expect(requestIosShipLaunch).toHaveBeenCalledTimes(2)
  expect(result.current.error).toBe('launch unavailable')
  expect(result.current.receipt).toEqual(receipt)
  fetchIosShipReadiness.mockResolvedValue(emptyIosShipReadiness('unreachable'))
  await act(async () => { await result.current.refresh() })
  expect(result.current.readiness.reason).toBe('unreachable')
  expect(result.current.receipt).toEqual(receipt)
  expect(getIosShipReceipt).toHaveBeenCalledTimes(1)
})

it('I1 row7 stops following at thirty minutes and refresh resumes it', async () => {
  const { result } = mount()
  await flush()
  await launch(result)
  await tick(30 * 60 * 1000)
  expect(result.current.error).toBe('still running; refresh to keep following')
  const calls = getIosShipExecution.mock.calls.length
  await tick(10000)
  expect(getIosShipExecution).toHaveBeenCalledTimes(calls)
  await act(async () => { await result.current.refresh() })
  await tick()
  expect(getIosShipExecution).toHaveBeenCalledTimes(calls + 1)
})

it('I1 row11 revision ownership clears execution and receipt and ignores old pointers and polls', async () => {
  const { result, rerender } = mount()
  await flush()
  requestIosShipLaunch.mockResolvedValueOnce({ execution: { ...queued, status: 'succeeded', receipt_id: 'receipt1' } })
  await launch(result)
  await flush()
  expect(result.current.receipt).toEqual(receipt)
  fetchIosShipReadiness.mockResolvedValue(approvedReadiness({ revision: 'r2' }))
  rerender({ ...props, revision: 'r2' })
  expect(result.current.execution).toBeNull()
  expect(result.current.receipt).toBeNull()
  await flush()
  fetchIosShipReadiness.mockResolvedValue(approvedReadiness())
  rerender(props)
  await flush()
  await launch(result)
  let resolvePoll
  getIosShipExecution.mockReturnValueOnce(new Promise((resolve) => { resolvePoll = resolve }))
  await tick()
  const pointer = sessionStorage.getItem(key)
  const calls = getIosShipExecution.mock.calls.length
  fetchIosShipReadiness.mockResolvedValue(approvedReadiness({ revision: 'r2' }))
  rerender({ ...props, revision: 'r2' })
  expect(result.current.execution).toBeNull()
  expect(result.current.receipt).toBeNull()
  await flush()
  expect(getIosShipExecution).toHaveBeenCalledTimes(calls)
  expect(sessionStorage.getItem(key)).toBe(pointer)
  await act(async () => { resolvePoll({ execution: { ...queued, status: 'succeeded', receipt_id: 'receipt1' } }) })
  expect(result.current.execution).toBeNull()
  expect(result.current.receipt).toBeNull()
  expect(sessionStorage.getItem(key)).toBe(pointer)
})

it('I1 row12 pending launch locks across disable and reenable until settlement', async () => {
  let resolveLaunch
  requestIosShipLaunch.mockReturnValueOnce(new Promise((resolve) => { resolveLaunch = resolve }))
  const { result, rerender } = mount()
  await flush()
  let first
  act(() => { first = result.current.launch() })
  rerender({ ...props, enabled: false })
  rerender({ ...props, enabled: true })
  await flush()
  expect(result.current.readiness.launchable).toBe(true)
  await act(async () => { expect(await result.current.launch()).toBeNull() })
  expect(requestIosShipLaunch).toHaveBeenCalledTimes(1)
  await act(async () => { resolveLaunch({ execution: queued }); await first })
  expect(result.current.execution).toBeNull()
  await launch(result)
  expect(requestIosShipLaunch).toHaveBeenCalledTimes(2)
  expect(result.current.execution).toEqual(queued)
})

it('I1 row13 refresh during a receipt read does not request it twice or discard its result', async () => {
  let resolveReceipt
  getIosShipReceipt.mockReturnValueOnce(new Promise((resolve) => { resolveReceipt = resolve }))
  const { result, rerender } = mount()
  await flush()
  await launch(result)
  getIosShipExecution.mockResolvedValueOnce({ execution: { ...queued, status: 'succeeded', receipt_id: 'receipt1' } })
  await tick()
  expect(getIosShipReceipt).toHaveBeenCalledTimes(1)
  // Start another settlement while the same receipt read is still pending.
  rerender({ ...props, enabled: false })
  rerender(props)
  await flush()
  await act(async () => { await result.current.refresh() })
  expect(getIosShipReceipt).toHaveBeenCalledTimes(1)
  expect(result.current.receipt).toBeNull()
  await act(async () => { resolveReceipt({ receipt }) })
  expect(result.current.receipt).toEqual(receipt)
  await act(async () => { await result.current.refresh() })
  expect(getIosShipReceipt).toHaveBeenCalledTimes(1)
})

it('I1 row14 busy ends with the launch response while execution is running', async () => {
  let resolveLaunch
  requestIosShipLaunch.mockReturnValueOnce(new Promise((resolve) => { resolveLaunch = resolve }))
  const { result } = mount()
  await flush()
  let pending
  act(() => { pending = result.current.launch() })
  expect(result.current.busy).toBe(true)
  expect(result.current.phase).toBe('launching')
  await act(async () => { resolveLaunch({ execution: queued }); await pending })
  expect(result.current.phase).toBe('running')
  expect(result.current.busy).toBe(false)
})

it('I1 row15 a rejected receipt read is retried on refresh', async () => {
  getIosShipReceipt.mockRejectedValueOnce(new Error('receipt unavailable'))
  const { result } = mount()
  await flush()
  await launch(result)
  getIosShipExecution.mockResolvedValueOnce({ execution: { ...queued, status: 'succeeded', receipt_id: 'receipt1' } })
  await tick()
  expect(result.current.execution.status).toBe('succeeded')
  expect(result.current.error).toBe('receipt unavailable')
  expect(result.current.receipt).toBeNull()
  expect(getIosShipReceipt).toHaveBeenCalledTimes(1)
  await act(async () => { await result.current.refresh() })
  await flush()
  expect(result.current.receipt).toEqual(receipt)
  expect(getIosShipReceipt).toHaveBeenCalledTimes(2)
  expect(getIosShipReceipt.mock.calls.map(([input]) => input.receiptId)).toEqual(['receipt1', 'receipt1'])
})

it('I1 row16 a stale follower rejection cannot restart the current follower or its deadline', async () => {
  const { result, rerender } = mount()
  await flush()
  await launch(result)
  let rejectOldPoll
  getIosShipExecution.mockReturnValueOnce(new Promise((resolve, reject) => { rejectOldPoll = reject }))
  await tick()
  expect(getIosShipExecution).toHaveBeenCalledTimes(1)
  fetchIosShipReadiness.mockResolvedValue(approvedReadiness({ revision: 'r2' }))
  rerender({ ...props, revision: 'r2' })
  await flush()
  const next = { execution_id: 'e2', status: 'running' }
  requestIosShipLaunch.mockResolvedValueOnce({ execution: next })
  getIosShipExecution.mockResolvedValue({ execution: next })
  await launch(result)
  await act(async () => { rejectOldPoll(new Error('old poll failed')) })
  expect(result.current.error).toBeNull()
  await tick()
  expect(getIosShipExecution).toHaveBeenCalledTimes(2)
  expect(getIosShipExecution).toHaveBeenLastCalledWith({ projectId: 'p1', executionId: 'e2' })
  await tick(500)
  await act(async () => { await result.current.refresh() })
  expect(getIosShipExecution).toHaveBeenCalledTimes(2)
  await tick(1499)
  expect(getIosShipExecution).toHaveBeenCalledTimes(2)
  await tick(1)
  expect(getIosShipExecution).toHaveBeenCalledTimes(3)
  await tick(30 * 60 * 1000 - 4000)
  expect(result.current.error).toBe('still running; refresh to keep following')
  expect(getIosShipExecution).toHaveBeenCalledTimes(900)
  await tick(4000)
  expect(getIosShipExecution).toHaveBeenCalledTimes(900)
})

it('I1 row17 a rejected relaunch preserves execution receipt and pointer', async () => {
  const previous = { ...queued, status: 'succeeded', receipt_id: 'receipt1' }
  requestIosShipLaunch.mockResolvedValueOnce({ execution: previous })
  const { result } = mount()
  await flush()
  await launch(result)
  await flush()
  expect(result.current.execution).toEqual(previous)
  expect(result.current.receipt).toEqual(receipt)
  const pointer = sessionStorage.getItem(key)
  let rejectLaunch
  requestIosShipLaunch.mockReturnValueOnce(new Promise((resolve, reject) => { rejectLaunch = reject }))
  let pending
  act(() => { pending = result.current.launch() })
  expect(result.current.busy).toBe(true)
  expect(result.current.execution).toEqual(previous)
  expect(result.current.receipt).toEqual(receipt)
  expect(sessionStorage.getItem(key)).toBe(pointer)
  await act(async () => { rejectLaunch(new Error('relaunch unavailable')); await pending })
  expect(result.current.execution).toEqual(previous)
  expect(result.current.receipt).toEqual(receipt)
  expect(sessionStorage.getItem(key)).toBe(pointer)
  expect(result.current.error).toBe('relaunch unavailable')
  expect(result.current.busy).toBe(false)
  expect(requestIosShipLaunch).toHaveBeenCalledTimes(2)
})
