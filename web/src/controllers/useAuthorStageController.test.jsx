import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, renderHook, screen } from '@testing-library/react'
import AuthorPanel from '../components/AuthorPanel.jsx'
import { config } from '../api.js'
import { AUTHOR_POINTER_TTL_MS, INFLIGHT_AUTHOR_KEY, authorAccountScope, readInflightAuthor } from '../authorStagePointer.js'
import { SecretRefusedError } from '../lib/secretGuardTransport.js'
import useAuthorStageController from './useAuthorStageController.js'

const description = 'Count panels'
const edited = 'Count selected panels'
const target = 'panel_counter'
const receipt = { change_set_id: 'stage-original', state: 'staged', details: { preserved: true } }
const staged = (id = 'stage-original') => ({
  tool: { name: target, description }, receipt: id === 'stage-original' ? receipt : { change_set_id: id, state: 'staged' },
  source: 'harness', code: 'def run(): pass',
})

function memoryStorage() {
  const values = new Map()
  return {
    getItem: (key) => values.get(key) ?? null,
    setItem: vi.fn((key, value) => values.set(key, String(value))),
    removeItem: vi.fn((key) => values.delete(key)),
  }
}

function original(storage) {
  const now = Date.now()
  return {
    idempotency_key: 'request-original', description, target_tool_name: target,
    created_at: now, expires_at: now + AUTHOR_POINTER_TTL_MS,
    account_scope: authorAccountScope(config.tenant, storage),
    change_set_id: 'stage-original', poll_url: '/api/author/stages/stage-original',
    terminal_staged: true, staged_result: staged(),
  }
}

function prepared(storage) {
  const pointer = original(storage)
  delete pointer.terminal_staged
  delete pointer.staged_result
  return { ...pointer, idempotency_key: 'request-revised', change_set_id: null, poll_url: null,
    draft_only: true, prior_staged: { idempotency_key: 'request-original', receipt } }
}

function save(storage, pointer) {
  storage.setItem(INFLIGHT_AUTHOR_KEY, JSON.stringify(pointer))
}

function options(storage = memoryStorage()) {
  return { storage,
    authorityProvider: vi.fn(async () => ({ sessionId: 'session-revised', turnId: 'turn-revised' })),
    stageAuthorTool: vi.fn(async () => staged('stage-revised')),
  }
}

function mountPanel(opts, onPublish = vi.fn(async (value) => ({ ...value, publication_status: 'denied' }))) {
  let controller
  const onCancelRevision = vi.fn()
  function Harness() {
    controller = useAuthorStageController(opts)
    return <AuthorPanel onAuthor={controller.stage} onPublish={onPublish} onUseAuthored={vi.fn()}
      targetToolName={target} onCancelRevision={onCancelRevision} stageActivity={controller} />
  }
  const view = render(<Harness />)
  return { ...view, current: () => controller, onPublish, onCancelRevision }
}

const field = () => screen.getByLabelText('What should the tool do?')
const button = (name) => screen.getByRole('button', { name })
const click = async (name) => { await act(async () => { fireEvent.click(button(name)) }) }

async function recovery(outcome) {
  const opts = options()
  save(opts.storage, original(opts.storage))
  const onPublish = vi.fn(async (value) => {
    if (outcome === 'denied') return { ...value, publication_status: 'denied' }
    throw Object.assign(new Error('Publication did not complete'), { status: outcome })
  })
  const panel = mountPanel(opts, onPublish)
  await act(async () => {})
  await click('Request publication')
  return { opts, panel }
}

beforeEach(() => {
  let count = 0
  vi.stubGlobal('crypto', { randomUUID: vi.fn(() => ++count === 1 ? 'request-revised' : `request-revised-${count}`) })
})
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

describe('prepared author drafts', () => {
  it('W21D1B-authority-fresh', async () => {
    const opts = options()
    let cached = null
    let mints = 0
    opts.authorityProvider.mockImplementation(async (_text, { forceFresh = false } = {}) => {
      if (!forceFresh && cached && Date.now() - cached.mintedAt < 120_000) {
        return { sessionId: cached.sessionId, turnId: cached.turnId }
      }
      cached = { sessionId: 'session-live', turnId: `turn-${++mints}`, mintedAt: Date.now() }
      return { sessionId: cached.sessionId, turnId: cached.turnId }
    })
    opts.stageAuthorTool.mockResolvedValueOnce(staged())
    mountPanel(opts)
    fireEvent.change(field(), { target: { value: description } })
    await click('Generate revision')
    expect(mints).toBe(1)
    expect(opts.authorityProvider.mock.calls[0]).toEqual([description, { allowSecretOnce: false }])
    const firstAuthority = opts.stageAuthorTool.mock.calls[0][3].authority
    await click('Request publication')
    expect(screen.getByText('Publication was denied. The staged tool was not published.')).toBeInTheDocument()
    await click('Revise')
    expect(mints).toBe(1)
    fireEvent.change(field(), { target: { value: edited } })
    await click('Generate revision')
    expect(mints).toBe(2)
    expect(opts.authorityProvider).toHaveBeenCalledTimes(2)
    expect(opts.authorityProvider.mock.calls[1]).toEqual([edited, { allowSecretOnce: false, forceFresh: true }])
    expect(opts.stageAuthorTool).toHaveBeenCalledTimes(2)
    expect(opts.stageAuthorTool.mock.calls[1][3].authority).toEqual({ sessionId: 'session-live', turnId: 'turn-2' })
    expect(opts.stageAuthorTool.mock.calls[1][3].authority.turnId).not.toBe(firstAuthority.turnId)
  })

  it('W21D1B-second-revision-receipt', async () => {
    const opts = options()
    save(opts.storage, original(opts.storage))
    const hook = renderHook(() => useAuthorStageController(opts))
    await act(async () => {})
    let firstRevision
    act(() => { firstRevision = hook.result.current.reviseDraft('request-original') })
    await act(async () => { await hook.result.current.stage(edited, target) })
    const immediateReceipt = hook.result.current.result.receipt
    let secondRevision
    act(() => { secondRevision = hook.result.current.reviseDraft(firstRevision.idempotency_key) })
    expect(secondRevision.idempotency_key).not.toBe(firstRevision.idempotency_key)
    expect(secondRevision.prior_staged).toEqual({ idempotency_key: firstRevision.idempotency_key, receipt: immediateReceipt })
    expect(secondRevision.prior_staged.idempotency_key).not.toBe('request-original')
    expect(secondRevision.prior_staged.receipt.change_set_id).toBe('stage-revised')
    expect(readInflightAuthor(opts.storage).prior_staged).toEqual(secondRevision.prior_staged)
    expect(hook.result.current.previousStagedReceipt).toEqual(immediateReceipt)
  })

  it('W21D1B-discard-progress', async () => {
    const opts = options()
    save(opts.storage, original(opts.storage))
    const hook = renderHook(() => useAuthorStageController(opts))
    await act(async () => {})
    expect(hook.result.current.progress).toBe('staged for review')
    expect(hook.result.current.result).not.toBeNull()
    act(() => { expect(hook.result.current.discardDraft('request-original')).toBe(true) })
    expect(hook.result.current).toMatchObject({ progress: null, error: null, result: null, phase: 'idle' })
    save(opts.storage, prepared(opts.storage))
    opts.stageAuthorTool.mockRejectedValueOnce(Object.assign(new Error('Authoring request refused'), { authorTerminal: true }))
    await act(async () => { await hook.result.current.stage(edited, target) })
    expect(hook.result.current.error).not.toBeNull()
    act(() => { expect(hook.result.current.discardDraft(hook.result.current.pointer.idempotency_key)).toBe(true) })
    expect(hook.result.current).toMatchObject({ progress: null, error: null, result: null, phase: 'idle' })
  })

  it('W21D1B-pointer-draft-only-null', () => {
    const storage = memoryStorage()
    save(storage, { ...prepared(storage), draft_only: null })
    expect(readInflightAuthor(storage)).toBeNull()
  })

  for (const [label, outcome] of [['denied', 'denied'], ['conflict', 409], ['expired', 410]]) {
    it(`W21D1-revise-${label}`, async () => {
      const { opts, panel } = await recovery(outcome)
      expect(field()).toBeDisabled()
      await click('Revise')
      expect(field()).toBeEnabled()
      expect(field()).toHaveFocus()
      expect(field()).toHaveValue(description)
      expect(screen.getByText('Revise the description, then generate a new draft.')).toBeInTheDocument()
      expect(screen.getByText('Previous staged receipt')).toBeInTheDocument()
      expect(screen.getByText('stage-original')).toBeInTheDocument()
      expect(screen.queryByRole('button', { name: /Request publication|Run it now/ })).toBeNull()
      expect(panel.current().pointer.idempotency_key).toBe('request-revised')
      expect(readInflightAuthor(opts.storage).prior_staged).toEqual({ idempotency_key: 'request-original', receipt })
      expect(opts.stageAuthorTool).not.toHaveBeenCalled()
      expect(opts.authorityProvider).not.toHaveBeenCalled()
      fireEvent.change(field(), { target: { value: edited } })
      await click('Generate revision')
      expect(opts.authorityProvider).toHaveBeenCalledTimes(1)
      expect(opts.authorityProvider).toHaveBeenCalledWith(edited, { allowSecretOnce: false, forceFresh: true })
      expect(opts.stageAuthorTool).toHaveBeenCalledTimes(1)
      expect(opts.stageAuthorTool).toHaveBeenCalledWith(false, edited, target, expect.objectContaining({
        idempotencyKey: 'request-revised', pollUrl: null, changeSetId: null,
        authority: { sessionId: 'session-revised', turnId: 'turn-revised' },
      }))
      expect(readInflightAuthor(opts.storage).prior_staged.receipt).toEqual(receipt)
      expect(panel.onPublish).toHaveBeenCalledTimes(1)
    })

    it(`W21D1-discard-${label}`, async () => {
      const { opts, panel } = await recovery(outcome)
      await click('Discard draft')
      expect(panel.current().pointer).toBeNull()
      expect(panel.current().result).toBeNull()
      expect(panel.current().phase).toBe('idle')
      expect(panel.current().elapsedMs).toBe(0)
      expect(opts.storage.getItem(INFLIGHT_AUTHOR_KEY)).toBeNull()
      expect(field()).toHaveValue('')
      expect(field()).toHaveFocus()
      expect(screen.getByLabelText('Tool to revise')).toHaveValue(target)
      expect(screen.getByText('Draft discarded.')).toBeInTheDocument()
      expect(button('Cancel revision')).toBeEnabled()
      expect(screen.queryByRole('button', { name: /Request publication|Run it now|Revise|Discard draft/ })).toBeNull()
      expect(opts.stageAuthorTool).not.toHaveBeenCalled()
      expect(opts.authorityProvider).not.toHaveBeenCalled()
      expect(panel.onPublish).toHaveBeenCalledTimes(1)
    })
  }

  it('W21D1-reload-prepared', async () => {
    const { opts, panel } = await recovery('denied')
    await click('Revise')
    const saved = opts.storage.getItem(INFLIGHT_AUTHOR_KEY)
    panel.unmount()
    const restored = mountPanel(opts)
    await act(async () => { await restored.current().resume() })
    expect(restored.current().phase).toBe('draft')
    expect(restored.current().result).toBeNull()
    expect(restored.current().draftOnly).toBe(true)
    expect(restored.current().previousStagedReceipt).toEqual(receipt)
    expect(restored.current().pointer.idempotency_key).toBe('request-revised')
    expect(field()).toHaveValue(description)
    expect(field()).toBeEnabled()
    expect(button('Generate revision')).toBeEnabled()
    expect(screen.queryByRole('button', { name: /Request publication|Run it now/ })).toBeNull()
    expect(opts.storage.getItem(INFLIGHT_AUTHOR_KEY)).toBe(saved)
    expect(opts.authorityProvider).not.toHaveBeenCalled()
    expect(opts.stageAuthorTool).not.toHaveBeenCalled()
  })

  it('W21D1-reload-restaged', async () => {
    const opts = options()
    save(opts.storage, prepared(opts.storage))
    opts.stageAuthorTool.mockImplementationOnce(async (_mock, _text, _target, callbacks) => {
      callbacks.onAccepted({ change_set_id: 'stage-revised', poll_url: '/api/author/stages/stage-revised' })
      throw new Error('Connection interrupted')
    })
    const panel = mountPanel(opts)
    fireEvent.change(field(), { target: { value: edited } })
    await click('Generate revision')
    expect(readInflightAuthor(opts.storage).prior_staged.receipt).toEqual(receipt)
    expect(panel.current().resumable).toBe(true)
    await act(async () => { await panel.current().resume() })
    expect(opts.stageAuthorTool.mock.calls[1][3]).toMatchObject({ pollUrl: '/api/author/stages/stage-revised', authority: null })
    panel.unmount()
    const restored = mountPanel(opts)
    await act(async () => {})
    expect(restored.current().result.receipt.change_set_id).toBe('stage-revised')
    expect(restored.current().previousStagedReceipt).toEqual(receipt)
    expect(restored.current().pointer.description).toBe(edited)
    expect(screen.getByText('Previous staged receipt')).toBeInTheDocument()
    expect(button('Request publication')).toBeInTheDocument()
    expect(opts.stageAuthorTool).toHaveBeenCalledTimes(2)
    expect(opts.authorityProvider).toHaveBeenCalledTimes(1)
  })

  it('W21D1-recovery-guards', async () => {
    for (const status of [null, 'awaiting_approval', 'published', 'independent_approval_pending']) {
      const opts = options()
      save(opts.storage, original(opts.storage))
      const publish = vi.fn(async (value) => {
        if (status === 'independent_approval_pending') throw Object.assign(new Error(status), { status: 409 })
        return { ...value, publication_status: status, published: status === 'published' }
      })
      const panel = mountPanel(opts, publish)
      await act(async () => {})
      if (status) await click('Request publication')
      expect(screen.queryByRole('button', { name: 'Revise' })).toBeNull()
      expect(screen.queryByRole('button', { name: 'Discard draft' })).toBeNull()
      if (status === 'awaiting_approval') expect(button('Check approval & resume')).toBeInTheDocument()
      expect(opts.stageAuthorTool).not.toHaveBeenCalled()
      expect(opts.authorityProvider).not.toHaveBeenCalled()
      panel.unmount()
    }
    const opts = options()
    save(opts.storage, original(opts.storage))
    const disabled = renderHook(() => useAuthorStageController({ ...opts, enabled: false }))
    const before = opts.storage.getItem(INFLIGHT_AUTHOR_KEY)
    act(() => {
      expect(disabled.result.current.reviseDraft('request-original')).toBeNull()
      expect(disabled.result.current.discardDraft('request-original')).toBe(false)
    })
    expect(opts.storage.getItem(INFLIGHT_AUTHOR_KEY)).toBe(before)
    disabled.unmount()
    let release
    opts.stageAuthorTool.mockImplementation(() => new Promise((resolve) => { release = resolve }))
    save(opts.storage, prepared(opts.storage))
    const active = renderHook(() => useAuthorStageController(opts))
    let pending
    act(() => { pending = active.result.current.stage(edited, target) })
    await act(async () => {})
    const activeSaved = opts.storage.getItem(INFLIGHT_AUTHOR_KEY)
    act(() => {
      expect(active.result.current.reviseDraft('request-revised')).toBeNull()
      expect(active.result.current.discardDraft('request-revised')).toBe(false)
    })
    expect(opts.storage.getItem(INFLIGHT_AUTHOR_KEY)).toBe(activeSaved)
    await act(async () => { release(staged('stage-revised')); await pending })
    active.unmount()
    save(opts.storage, original(opts.storage))
    const denied = { ...staged(), publication_status: 'denied' }
    let finishPublish
    const onPublish = vi.fn(() => new Promise((resolve) => { finishPublish = resolve }))
    const panel = mountPanel(opts, onPublish)
    await act(async () => {})
    fireEvent.click(button('Request publication'))
    expect(button('Publishing…')).toBeDisabled()
    expect(screen.queryByRole('button', { name: 'Revise' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Discard draft' })).toBeNull()
    expect(field()).toBeDisabled()
    expect(readInflightAuthor(opts.storage).idempotency_key).toBe('request-original')
    await act(async () => { finishPublish(denied) })
    expect(button('Revise')).toBeEnabled()
    expect(button('Discard draft')).toBeEnabled()
    panel.unmount()
    const disabledPanel = mountPanel({ ...opts, enabled: false })
    expect(screen.queryByRole('button', { name: 'Revise' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Discard draft' })).toBeNull()
    disabledPanel.unmount()
  })

  it('W21D1-duplicate-generate', async () => {
    const opts = options()
    save(opts.storage, prepared(opts.storage))
    let release
    opts.stageAuthorTool.mockImplementation(() => new Promise((resolve) => { release = resolve }))
    const hook = renderHook(() => useAuthorStageController(opts))
    let first, second
    act(() => {
      first = hook.result.current.stage(edited, target)
      second = hook.result.current.stage(edited, target)
    })
    expect(first).toBe(second)
    await act(async () => {})
    expect(opts.authorityProvider).toHaveBeenCalledTimes(1)
    expect(opts.stageAuthorTool).toHaveBeenCalledTimes(1)
    expect(opts.stageAuthorTool.mock.calls[0][3].idempotencyKey).toBe('request-revised')
    await act(async () => { release(staged('stage-revised')); await Promise.all([first, second]) })
  })

  it('W21D1-secret-refusal', async () => {
    const opts = options()
    save(opts.storage, prepared(opts.storage))
    const hook = renderHook(() => useAuthorStageController(opts))
    const before = opts.storage.getItem(INFLIGHT_AUTHOR_KEY)
    const pointerBefore = hook.result.current.pointer
    await act(async () => {
      await expect(hook.result.current.stage(`api_key: ${'x'.repeat(24)}`, target)).rejects.toBeInstanceOf(SecretRefusedError)
      expect(await hook.result.current.stage(edited, 'another_tool')).toBeNull()
    })
    expect(opts.storage.getItem(INFLIGHT_AUTHOR_KEY)).toBe(before)
    expect(hook.result.current.pointer).toBe(pointerBefore)
    expect(opts.authorityProvider).not.toHaveBeenCalled()
    expect(opts.stageAuthorTool).not.toHaveBeenCalled()
  })

  it('W21D1-preaccept-failure', async () => {
    const opts = options()
    save(opts.storage, prepared(opts.storage))
    crypto.randomUUID.mockReturnValue('request-revised-2')
    opts.stageAuthorTool.mockRejectedValue(Object.assign(new Error('Authoring request refused'), { authorTerminal: true }))
    const panel = mountPanel(opts)
    fireEvent.change(field(), { target: { value: edited } })
    await click('Generate revision')
    expect(panel.current().phase).toBe('draft')
    expect(panel.current().error.message).toBe('Authoring request refused')
    expect(field()).toHaveValue(edited)
    expect(field()).toBeEnabled()
    expect(screen.getByText(/Authoring request refused/)).toBeInTheDocument()
    expect(readInflightAuthor(opts.storage)).toMatchObject({ draft_only: true, description: edited,
      idempotency_key: 'request-revised-2', prior_staged: { idempotency_key: 'request-original', receipt } })
    expect(opts.stageAuthorTool).toHaveBeenCalledTimes(1)
    await act(async () => { await panel.current().resume() })
    expect(opts.stageAuthorTool).toHaveBeenCalledTimes(1)
    expect(opts.authorityProvider).toHaveBeenCalledTimes(1)
  })

  it('W21D1-pointer-schema', () => {
    const storage = memoryStorage()
    const normal = original(storage)
    const failed = { ...normal, terminal_staged: false, staged_result: null, terminal_failed: true,
      failed_at: Date.now(), failure: { status: 500, reason_code: 'author_failed', message: 'Authoring failed. Your request is saved for recovery.' } }
    const draft = prepared(storage)
    for (const pointer of [normal, failed, draft, { ...normal, draft_only: false, prior_staged: draft.prior_staged }]) {
      save(storage, pointer)
      expect(readInflightAuthor(storage)).toEqual(pointer)
    }
    const invalid = [
      { ...draft, draft_only: 'true' }, { ...draft, prior_staged: null }, { ...draft, prior_staged: [] },
      { ...draft, prior_staged: { ...draft.prior_staged, extra: true } },
      { ...draft, prior_staged: { ...draft.prior_staged, idempotency_key: '' } },
      { ...draft, prior_staged: { ...draft.prior_staged, idempotency_key: 1 } },
      ...[null, [], {}, { change_set_id: '' }, { change_set_id: 1 }].map((value) => ({ ...draft, prior_staged: { idempotency_key: 'original', receipt: value } })),
      { ...draft, prior_staged: undefined }, { ...draft, terminal_staged: true },
      { ...draft, terminal_failed: true }, { ...draft, staged_result: staged() },
      { ...draft, poll_url: '/api/author/stages/original' }, { ...draft, change_set_id: 'original' },
    ]
    for (const pointer of invalid) { save(storage, pointer); expect(readInflightAuthor(storage)).toBeNull() }
  })

  it('W21D1-stale-discard', async () => {
    const opts = options()
    save(opts.storage, original(opts.storage))
    const hook = renderHook(() => useAuthorStageController(opts))
    await act(async () => {})
    const local = hook.result.current.pointer
    const localResult = hook.result.current.result
    save(opts.storage, { ...original(opts.storage), idempotency_key: 'request-newer' })
    const newer = opts.storage.getItem(INFLIGHT_AUTHOR_KEY)
    act(() => {
      expect(hook.result.current.discardDraft('request-original')).toBe(false)
      expect(hook.result.current.reviseDraft('request-original')).toBeNull()
    })
    expect(opts.storage.getItem(INFLIGHT_AUTHOR_KEY)).toBe(newer)
    expect(hook.result.current.pointer).toBe(local)
    expect(hook.result.current.result).toBe(localResult)
    expect(opts.stageAuthorTool).not.toHaveBeenCalled()
    expect(opts.authorityProvider).not.toHaveBeenCalled()
  })

  it('W21D1-mock-isolation', async () => {
    const opts = options()
    save(opts.storage, original(opts.storage))
    const live = opts.storage.getItem(INFLIGHT_AUTHOR_KEY)
    opts.storage.setItem.mockClear(); opts.storage.removeItem.mockClear()
    const hook = renderHook(() => useAuthorStageController({ ...opts, mock: true }))
    opts.stageAuthorTool.mockResolvedValueOnce(staged())
    await act(async () => { await hook.result.current.stage(description, target) })
    const originalKey = hook.result.current.pointer.idempotency_key
    let revised
    act(() => { revised = hook.result.current.reviseDraft(originalKey) })
    expect(revised.idempotency_key).not.toBe(originalKey)
    expect(revised.prior_staged).toEqual({ idempotency_key: originalKey, receipt })
    expect(hook.result.current.draftOnly).toBe(true)
    await act(async () => { await hook.result.current.resume() })
    expect(opts.stageAuthorTool).toHaveBeenCalledTimes(1)
    await act(async () => { await hook.result.current.stage(edited, target) })
    expect(opts.stageAuthorTool.mock.calls[1]).toEqual([true, edited, target, expect.objectContaining({ idempotencyKey: revised.idempotency_key, pollUrl: null })])
    expect(hook.result.current.previousStagedReceipt).toEqual(receipt)
    act(() => { expect(hook.result.current.discardDraft(revised.idempotency_key)).toBe(true) })
    expect(hook.result.current.pointer).toBeNull()
    expect(hook.result.current.result).toBeNull()
    expect(opts.storage.getItem(INFLIGHT_AUTHOR_KEY)).toBe(live)
    expect(opts.storage.setItem).not.toHaveBeenCalled()
    expect(opts.storage.removeItem).not.toHaveBeenCalled()
    expect(opts.authorityProvider).toHaveBeenCalledTimes(2)
  })
})
