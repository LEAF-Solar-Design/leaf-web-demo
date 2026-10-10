import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, renderHook, screen, waitFor } from '@testing-library/react'
import AuthorPanel from '../components/AuthorPanel.jsx'
import useAuthorStageController, { authorStartFailureOf } from './useAuthorStageController.js'

// This repository runs vitest with globals off, so nothing cleans up between tests.
afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  try { window.localStorage.clear(); window.sessionStorage.clear() } catch { /* no storage here */ }
})

const description = 'Count the panels on each roof plane.'
const changeSetId = '24b18e9d-3aaa-4e75-b443-6cab404ffd4c'
const pollUrl = `/api/author/stages/${changeSetId}`
const GENERIC = 'Could not start authoring in this conversation. Wait for the current turn to finish, then try again.'
const LINK_GATE = 'Link your Claude account to author with the agent.'
const SERVICE_GATE = 'Authoring service is temporarily unavailable.'
const NOTE = 'Your description is preserved.'
const IDENTITY = 'Tool authoring requires a verified workspace identity for the requester. The approved request was not executed.'
const SENTENCES = {
  grant: 'Link your Claude account to start authoring.',
  quota: 'Your Claude usage limit has been reached; try again when usage is available.',
  rate_limited: 'Authoring is temporarily rate limited; wait before trying again.',
  auth: 'Sign in to Leaf again to start authoring.',
  network: 'Could not reach the authoring service; check your connection and try again.',
  transport: 'The authoring service is temporarily unavailable; try again.',
}
const recordOf = (kind) => ({ kind, message: SENTENCES[kind] })
const refusalOf = () => ({ id: 'r1', reason: 'This looks like a credential.', masked: 'sk-a***', overridable: true })

function memoryStorage() {
  const values = new Map()
  return {
    values,
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, String(value)),
    removeItem: (key) => values.delete(key),
  }
}

function fail(message, fields = {}, Type = Error) {
  return Object.assign(new Type(message), fields)
}

// Every entry builds a fresh error, so no run can see anything another run left.
const CLASSIFIED = [
  ['grant', 'a grant flag on the body', () => fail('refused', { status: 401, body: { grant_required: true } })],
  ['grant', 'a grant flag on the nested error', () => fail('refused', { status: 403, body: { error: { grant_required: true } } })],
  ['grant', 'a mixed-case grant code', () => fail('refused', { status: 403, code: 'Grant_Required' })],
  ['quota', 'a quota code on the error', () => fail('limit', { status: 429, errorCode: 'llm_quota_exhausted' })],
  ['quota', 'an upper-case nested quota code', () => fail('limit', { status: 429, body: { error: { error_code: 'LLM_QUOTA_EXHAUSTED' } } })],
  ['rate_limited', 'a nested rate code', () => fail('slow', { status: 429, body: { error: { error_code: 'llm_rate_limited' } } })],
  ['rate_limited', 'an upper-case body reason code', () => fail('slow', { status: 429, body: { reason_code: 'LLM_RATE_LIMITED' } })],
  ['auth', 'a plain 401', () => fail('unauthorized', { status: 401 })],
  ['network', 'a fetch TypeError', () => fail('Failed to fetch', {}, TypeError)],
  ['network', 'a Firefox network TypeError', () => fail('NetworkError when attempting to fetch resource.', {}, TypeError)],
  ['network', 'a Safari load failure with status 0', () => fail('Load failed', { status: 0 })],
  ['transport', 'a 502 with a broker code', () => fail('bad', { status: 502, body: { error: { error_code: 'BROKER_UNREACHABLE' } } })],
  ['transport', 'a 503 with a stage reason', () => fail('bad', { status: 503, body: { reason_code: 'customization_stage_failed' } })],
  ['transport', 'a 503 with an upper-case stage reason', () => fail('bad', { status: 503, body: { reason_code: 'CUSTOMIZATION_STAGE_FAILED' } })],
  ['transport', 'a 503 naming the harness outage', () => fail('bad', { status: 503, body: { error: { error_code: 'BAD_PARAMS' }, reason_code: 'customization_harness_unavailable' } })],
  ['transport', 'a 503 whose only code is the request class', () => fail('bad', { status: 503, body: { error: { error_code: 'BAD_PARAMS' } } })],
  ['transport', 'a 503 with two passing reasons', () => fail('bad', { status: 503, reasonCode: 'customization_stage_failed', body: { reason: 'customization_harness_unavailable' } })],
  ['transport', 'a bare 504', () => fail('bad', { status: 504 })],
  ['transport', 'a harness code with no status', () => fail('bad', { code: 'harness_unreachable' })],
]

async function runProvider(error, extra = {}) {
  const storage = memoryStorage()
  const stageAuthorTool = vi.fn()
  const authorityProvider = typeof error === 'function' ? vi.fn(error) : vi.fn(async () => { throw error })
  const hook = renderHook(() => useAuthorStageController({ storage, stageAuthorTool, authorityProvider, ...extra }))
  await act(async () => { await hook.result.current.stage(description) })
  const { error: shown, phase, resumable } = hook.result.current
  hook.unmount()
  return { error: shown, phase, resumable, storage, stageAuthorTool, authorityProvider }
}

async function runStage(error, { accept = false, ...extra } = {}) {
  const storage = 'storage' in extra ? extra.storage : memoryStorage()
  const stageAuthorTool = vi.fn(async (_mock, _description, _target, opts) => {
    if (accept) opts.onAccepted({ change_set_id: changeSetId, poll_url: pollUrl, retry_after_ms: 1 })
    throw error
  })
  const authorityProvider = vi.fn(async () => ({ sessionId: 'session-1', turnId: 'turn-1' }))
  const hook = renderHook(() => useAuthorStageController({ ...(storage ? { storage } : {}), stageAuthorTool, authorityProvider }))
  await act(async () => { await hook.result.current.stage(description) })
  const { error: shown, phase, resumable } = hook.result.current
  hook.unmount()
  return { error: shown, phase, resumable, storage, stageAuthorTool, authorityProvider }
}

// An error the controller itself classified: the only kind the panel will read.
async function classified(make, run = runProvider) {
  return (await run(make())).error
}

function stored(storage) {
  return JSON.stringify([...storage.values.entries()])
}

// What the browser's own two stores hold, read entry by entry, so a value that
// was put there without calling setItem is seen as well.
function browserStored() {
  const held = []
  for (const store of [window.localStorage, window.sessionStorage]) {
    for (let index = 0; index < store.length; index += 1) {
      const key = store.key(index)
      held.push(`${key}=${store.getItem(key)}`)
    }
  }
  return held
}

function view(error, activity = {}, props = {}) {
  return <AuthorPanel onAuthor={vi.fn()} stageActivity={{ error, ...activity }} onResumeAuthor={vi.fn()} {...props} />
}

function panel(error, activity, props) {
  return render(view(error, activity, props))
}

const box = () => document.querySelector('.inline-error')

describe('author start failures: the controller', () => {
  it('C8A-01 a failed turn start is classified from structured fields', async () => {
    for (const [kind, label, make] of CLASSIFIED) {
      const { error, phase, resumable, stageAuthorTool } = await runProvider(make())
      expect(authorStartFailureOf(error), label).toEqual(recordOf(kind))
      expect(phase, label).toBe('failed')
      expect(resumable, label).toBe(false)
      expect(stageAuthorTool, label).not.toHaveBeenCalled()
    }
  })

  it('C8A-02 a stage request refused before acceptance is classified the same way', async () => {
    for (const [kind, label, make] of CLASSIFIED) {
      const { error, phase, resumable, stageAuthorTool } = await runStage(make())
      expect(authorStartFailureOf(error), label).toEqual(recordOf(kind))
      expect(phase, label).toBe('interrupted')
      expect(resumable, label).toBe(true)
      expect(stageAuthorTool, label).toHaveBeenCalledTimes(1)
    }
  })

  it('C8A-03 the daily authoring quota is never classified', async () => {
    const daily = [
      ['on the body', () => fail('cap', { status: 429, body: { quota_kind: 'daily_author' } })],
      ['on the nested error, upper case', () => fail('cap', { status: 429, body: { error: { quota_kind: 'DAILY_AUTHOR' } } })],
      ['beside a provider quota code', () => fail('cap', { status: 429, errorCode: 'llm_quota_exhausted', body: { quota_kind: 'Daily_Author' } })],
    ]
    for (const [label, make] of daily) {
      for (const run of [runProvider, runStage]) {
        const { error } = await run(make())
        expect(error, label).toBeTruthy()
        expect(authorStartFailureOf(error), label).toBeNull()
      }
    }
  })

  it('C8A-04 precedence between the kinds is fixed', async () => {
    const ordered = [
      ['grant', 'a grant flag beats a quota code and a 401', () => fail('x', { status: 401, errorCode: 'llm_quota_exhausted', body: { grant_required: true } })],
      ['quota', 'a quota code beats a rate code', () => fail('x', { status: 429, errorCode: 'llm_quota_exhausted', code: 'llm_rate_limited' })],
      ['rate_limited', 'a rate code beats a 401', () => fail('x', { status: 401, code: 'llm_rate_limited' })],
      ['auth', 'a 401 beats a fetch message', () => fail('Failed to fetch', { status: 401 })],
      ['transport', 'a 503 with a fetch message is the service, not the network', () => fail('Failed to fetch', { status: 503 })],
      ['quota', 'a quota code beats a 503 that names a lasting fault', () => fail('x', { status: 503, code: 'llm_quota_exhausted', body: { reason_code: 'customization_not_configured' } })],
      [null, 'the auth-off stage refusal is left alone even with a grant flag', () => fail('x', { status: 401, body: { reason_code: 'customization_auth_required', grant_required: true } })],
      [null, 'the daily quota beats a provider quota code', () => fail('x', { status: 429, code: 'llm_quota_exhausted', body: { error: { quota_kind: 'daily_author' } } })],
      [null, 'a lasting 503 with a fetch message is not the network either', () => fail('Failed to fetch', { status: 503, body: { reason_code: 'customization_not_configured' } })],
    ]
    for (const [kind, label, make] of ordered) {
      for (const run of [runProvider, runStage]) {
        const { error } = await run(make())
        if (kind) expect(authorStartFailureOf(error), label).toEqual(recordOf(kind))
        else expect(authorStartFailureOf(error), label).toBeNull()
      }
    }
  })

  it('C8A-05 unknown failure shapes stay unclassified', async () => {
    const unknown = [
      ['a bare 403', () => fail('forbidden', { status: 403 })],
      ['a bare 429', () => fail('too many', { status: 429 })],
      ['a 409 with a busy code', () => fail('busy', { status: 409, body: { error: { error_code: 'BUSY' } } })],
      ['a plain error', () => fail('boom')],
      ['a programming TypeError', () => fail("Cannot read properties of undefined (reading 'x')", {}, TypeError)],
      ['a bare 500', () => fail('server', { status: 500 })],
      ['a fetch message beside another status', () => fail('Failed to fetch', { status: 418 })],
      ['a code that only contains a known code', () => fail('x', { code: 'not_llm_quota_exhausted' })],
      ['a grant flag that is not exactly true', () => fail('x', { status: 403, body: { grant_required: 'true' } })],
    ]
    for (const [label, make] of unknown) {
      for (const run of [runProvider, runStage]) {
        const { error } = await run(make())
        expect(error, label).toBeTruthy()
        expect(authorStartFailureOf(error), label).toBeNull()
      }
    }
  })

  it('C8A-06 a credential refusal, a cancel and the demo refusal are never classified', async () => {
    const left = [
      ['a credential refusal carrying a 401', () => fail('refused', { secretRefused: true, refusal: refusalOf(), status: 401 })],
      ['a cancel carrying a 503', () => fail('The operation was aborted.', { name: 'AbortError', status: 503 })],
      ['a demo refusal carrying a 401', () => fail('demo', { reasonCode: 'signed-out-demo', status: 401 })],
    ]
    for (const [label, make] of left) {
      for (const run of [runProvider, runStage]) {
        const { error } = await run(make())
        expect(authorStartFailureOf(error), label).toBeNull()
      }
    }
    const demo = await runProvider(async () => null, { mock: true })
    expect(demo.error.reasonCode).toBe('signed-out-demo')
    expect(authorStartFailureOf(demo.error)).toBeNull()
  })

  it('C8A-07 the client grant heuristic alone never selects the grant sentence', async () => {
    const heuristic = [
      ['auth', 'beside a 401', () => fail('x', { status: 401, grantRequired: true })],
      [null, 'beside a 403', () => fail('x', { status: 403, grantRequired: true })],
      [null, 'with no status', () => fail('x', { grantRequired: true })],
    ]
    for (const [kind, label, make] of heuristic) {
      for (const run of [runProvider, runStage]) {
        const { error } = await run(make())
        if (kind) expect(authorStartFailureOf(error), label).toEqual(recordOf(kind))
        else expect(authorStartFailureOf(error), label).toBeNull()
      }
    }
  })

  it('C8A-08 a connection lost after acceptance is not a start failure', async () => {
    const lost = [
      ['a fetch TypeError', () => fail('Failed to fetch', {}, TypeError)],
      ['a 503', () => fail('bad', { status: 503 })],
      ['a 401', () => fail('unauthorized', { status: 401 })],
    ]
    for (const [label, make] of lost) {
      const { error, stageAuthorTool } = await runStage(make(), { accept: true })
      expect(error, label).toBeTruthy()
      expect(authorStartFailureOf(error), label).toBeNull()
      expect(stageAuthorTool, label).toHaveBeenCalledTimes(1)
    }
  })

  it('C8A-09 a missing authority keeps the generic sentence and never calls the stage', async () => {
    for (const authority of [null, undefined, {}, { sessionId: 'session-1' }, { turnId: 'turn-1' }]) {
      const { error, stageAuthorTool } = await runProvider(async () => authority)
      expect(error.message).toBe(GENERIC)
      expect(authorStartFailureOf(error)).toBeNull()
      expect(stageAuthorTool).not.toHaveBeenCalled()
    }
  })

  it('C8A-10 the classification never reaches any storage', async () => {
    const setItem = vi.spyOn(Storage.prototype, 'setItem')
    // The reading sees a write the spy cannot: one made without calling setItem.
    expect(browserStored()).toEqual([])
    window.sessionStorage.probe = 'held'
    expect(browserStored()).toEqual(['probe=held'])
    expect(setItem).not.toHaveBeenCalled()
    window.sessionStorage.clear()
    expect(browserStored()).toEqual([])
    const injected = [
      await runProvider(fail('limit', { status: 429, errorCode: 'llm_quota_exhausted' })),
      await runStage(fail('Failed to fetch', {}, TypeError)),
      await runStage(fail('bad', { status: 503 })),
    ]
    for (const { error, storage } of injected) {
      expect(authorStartFailureOf(error)).toBeTruthy()
      const text = stored(storage)
      expect(text).not.toContain('authorStartFailure')
      for (const sentence of Object.values(SENTENCES)) expect(text).not.toContain(sentence)
    }
    // The injected stores never touch the browser's own storage, by any route.
    expect(setItem).not.toHaveBeenCalled()
    expect(browserStored()).toEqual([])
    // With no store handed in, the pointer goes to the browser's storage: the
    // spy must see that write, and the write must carry no classification.
    const browser = await runStage(fail('bad', { status: 503 }), { storage: null })
    expect(authorStartFailureOf(browser.error)).toEqual(recordOf('transport'))
    expect(browser.resumable).toBe(true)
    expect(setItem).toHaveBeenCalled()
    // Each value as it was written, then what the stores hold afterwards however
    // it got there: both carry the request and neither carries a classification.
    const written = setItem.mock.calls.map(([key, value]) => `${key}=${value}`).join(' ')
    const held = browserStored().join(' ')
    for (const text of [written, held]) {
      expect(text).toContain(description)
      // The pointer is read as it is stored, so a "kind" entry would be seen.
      expect(text).toContain('"idempotency_key":"')
      expect(text).not.toContain('authorStartFailure')
      for (const kind of Object.keys(SENTENCES)) {
        expect(text).not.toContain(SENTENCES[kind])
        expect(text).not.toContain(`"kind":"${kind}"`)
      }
    }
  })

  it('C8A-11 a classified failure keeps the thrown error and adds nothing of its own to it', async () => {
    const thrown = fail('PROVIDER private details', { status: 429, errorCode: 'llm_quota_exhausted' })
    const providerKeys = Reflect.ownKeys(thrown)
    const provider = await runProvider(thrown)
    expect(provider.error).toBe(thrown)
    expect(provider.error.message).toBe('PROVIDER private details')
    // The turn-start path stamps its own two fields, as it did before this change.
    expect(Reflect.ownKeys(thrown).filter((key) => !providerKeys.includes(key)).sort()).toEqual(['authorTerminal', 'description'])
    expect('authorStartFailure' in thrown).toBe(false)
    expect(authorStartFailureOf(thrown)).toEqual(recordOf('quota'))

    const staged = fail('STAGE private details', { status: 502 })
    const stagedKeys = Reflect.ownKeys(staged)
    const stage = await runStage(staged)
    expect(stage.error).toBe(staged)
    expect(stage.error.message).toBe('STAGE private details')
    expect(Reflect.ownKeys(staged)).toEqual(stagedKeys)
    expect('authorStartFailure' in staged).toBe(false)
    expect(authorStartFailureOf(staged)).toEqual(recordOf('transport'))

    // One frozen record per kind, shared: nothing a reader does can change it.
    const first = await classified(() => fail('one', { status: 401 }))
    const second = await classified(() => fail('two', { status: 401 }), runStage)
    expect(authorStartFailureOf(first)).toBe(authorStartFailureOf(second))
    expect(Object.isFrozen(authorStartFailureOf(first))).toBe(true)
    expect(Object.keys(authorStartFailureOf(first)).sort()).toEqual(['kind', 'message'])
    expect(() => { authorStartFailureOf(first).message = 'changed' }).toThrow(TypeError)
    expect(authorStartFailureOf(second).message).toBe(SENTENCES.auth)

    const sentences = Object.values(SENTENCES)
    expect(new Set(sentences).size).toBe(6)
    for (const sentence of sentences) {
      expect(sentence).not.toMatch(/PROVIDER|STAGE|\d/)
      expect(sentence.endsWith('.')).toBe(true)
    }
  })

  it('C8A-17 a 502, 503 or 504 that names a lasting fault keeps the server sentence', async () => {
    const lasting = [
      ['the identity binding', () => fail(IDENTITY, { status: 503, body: { error: { error_code: 'BAD_PARAMS', message: IDENTITY }, reason_code: 'tenant_identity_binding_unavailable' } })],
      ['a receipt fault on a 502', () => fail('bad', { status: 502, body: { reason_code: 'invalid_staged_receipt' } })],
      ['one passing reason beside one lasting reason', () => fail('bad', { status: 503, reasonCode: 'customization_stage_failed', body: { reason_code: 'customization_store_unsupported' } })],
      ['a lasting nested reason on a 504', () => fail('bad', { status: 504, body: { error: { reason_code: 'effective_catalog_malformed' } } })],
      ['a lasting body reason', () => fail('bad', { status: 503, body: { reason: 'staff_authority_unavailable' } })],
      ['a lasting reason on the error itself', () => fail('bad', { status: 503, reasonCode: 'customization_publish_incomplete' })],
      ['a harness code beside a lasting reason', () => fail('bad', { code: 'harness_unreachable', body: { reason_code: 'customization_not_configured' } })],
    ]
    for (const [label, make] of lasting) {
      for (const run of [runProvider, runStage]) {
        const { error } = await run(make())
        expect(error, label).toBeTruthy()
        expect(authorStartFailureOf(error), label).toBeNull()
      }
    }
    // The same statuses with no reason, or only a passing one, are the outage.
    for (const make of [
      () => fail('bad', { status: 503 }),
      () => fail('bad', { status: 503, body: { error: { error_code: 'BAD_PARAMS' }, reason_code: 'customization_harness_unavailable' } }),
      () => fail('bad', { status: 502, body: { reason_code: 'customization_stage_failed' } }),
    ]) {
      expect(authorStartFailureOf(await classified(make, runStage))).toEqual(recordOf('transport'))
    }
  })

  it('C8A-18 an error that cannot take a new property is still classified', async () => {
    for (const lock of [Object.freeze, Object.seal, Object.preventExtensions]) {
      const thrown = lock(fail('bad', { status: 503 }))
      const { error, phase, resumable } = await runStage(thrown)
      expect(error, lock.name).toBe(thrown)
      expect(authorStartFailureOf(thrown), lock.name).toEqual(recordOf('transport'))
      expect(phase, lock.name).toBe('interrupted')
      expect(resumable, lock.name).toBe(true)
    }
    // Declared limit, unchanged from before: the turn-start path stamps the
    // error it caught, so a frozen one from there surfaces as that failure.
    const frozen = Object.freeze(fail('unauthorized', { status: 401 }))
    const { error } = await runProvider(frozen)
    expect(error).toBeTruthy()
    expect(authorStartFailureOf(frozen)).toBeNull()
    expect(authorStartFailureOf(error)).toBeNull()
  })

  it('C8A-19 an error thrown a second time does not keep the record of its first throw', async () => {
    const reused = fail('bad', { status: 503 })
    await runStage(reused)
    expect(authorStartFailureOf(reused)).toEqual(recordOf('transport'))
    // The same object, thrown after the request was accepted, is no start failure.
    const accepted = await runStage(reused, { accept: true })
    expect(accepted.error).toBe(reused)
    expect(authorStartFailureOf(reused)).toBeNull()
    // Thrown again before acceptance it is classified again, from its fields now.
    await runStage(reused)
    expect(authorStartFailureOf(reused)).toEqual(recordOf('transport'))
    reused.status = 401
    await runStage(reused)
    expect(authorStartFailureOf(reused)).toEqual(recordOf('auth'))
    reused.status = 500
    await runStage(reused)
    expect(authorStartFailureOf(reused)).toBeNull()
  })

  it('C8A-20 a record is read only for the exact error the controller recorded', async () => {
    for (const value of [null, undefined, 0, 1, '', 'text', true, Symbol('s'), 10n, {}, [], () => {}, new Error('x')]) {
      expect(authorStartFailureOf(value)).toBeNull()
    }
    const forged = { kind: 'quota', message: 'FORGED sentence' }
    const marked = fail('boom', { authorStartFailure: forged })
    const { error } = await runStage(marked)
    expect(error).toBe(marked)
    expect(authorStartFailureOf(marked)).toBeNull()
    // The controller neither reads nor removes a property somebody else set.
    expect(marked.authorStartFailure).toBe(forged)
    // A copy of a classified error is a different object: it has no record.
    const original = await classified(() => fail('unauthorized', { status: 401 }), runStage)
    expect(authorStartFailureOf(original)).toEqual(recordOf('auth'))
    expect(authorStartFailureOf(Object.assign(new Error(original.message), original))).toBeNull()
    expect(authorStartFailureOf(Object.create(original))).toBeNull()
  })

  it('C8A-30 a failure whose fields cannot be read settles as it always did, unclassified', async () => {
    const unreadable = (field) => {
      const error = fail('bad')
      Object.defineProperty(error, field, { get() { throw new Error(`${field} cannot be read`) } })
      return error
    }
    const hostile = [
      ['a status that throws', () => unreadable('status')],
      ['a body that throws', () => unreadable('body')],
      ['a reason code that throws', () => unreadable('reasonCode')],
      ['a message that throws', () => unreadable('message')],
      ['a credential flag that throws', () => unreadable('secretRefused')],
      ['a status that is a symbol', () => fail('bad', { status: Symbol('status') })],
      ['a proxy that refuses one read', () => new Proxy(fail('bad', { status: 503 }), {
        get(target, key, receiver) {
          if (key === 'secretRefused') throw new Error('this read is refused')
          return Reflect.get(target, key, receiver)
        },
      })],
    ]
    // Identity is compared outside expect, so a failing row never has to print
    // an object whose fields throw.
    for (const [label, make] of hostile) {
      const staged = make()
      const stage = await runStage(staged)
      expect(stage.error === staged, label).toBe(true)
      expect(stage.phase, label).toBe('interrupted')
      expect(stage.resumable, label).toBe(true)
      expect(stage.stageAuthorTool, label).toHaveBeenCalledTimes(1)
      expect(authorStartFailureOf(staged), label).toBeNull()

      const refused = make()
      const provider = await runProvider(refused)
      expect(provider.error === refused, label).toBe(true)
      expect(provider.phase, label).toBe('failed')
      expect(provider.resumable, label).toBe(false)
      expect(provider.stageAuthorTool, label).not.toHaveBeenCalled()
      expect(authorStartFailureOf(refused), label).toBeNull()
    }

    // A record from an earlier throw does not survive a throw that cannot be read.
    const reused = fail('bad', { status: 503 })
    await runStage(reused)
    expect(authorStartFailureOf(reused)).toEqual(recordOf('transport'))
    Object.defineProperty(reused, 'status', { get() { throw new Error('status cannot be read') } })
    const second = await runStage(reused)
    expect(second.error === reused).toBe(true)
    expect(second.phase).toBe('interrupted')
    expect(authorStartFailureOf(reused)).toBeNull()

    // What cannot even be recognised as an error fails as it did before this
    // change: after the request is marked interrupted, with its own error.
    const opaque = new Proxy(fail('bad', { status: 503 }), {
      getPrototypeOf() { throw new RangeError('no prototype to read') },
    })
    const opaqueStorage = memoryStorage()
    const stageAuthorTool = vi.fn(async () => { throw opaque })
    const authorityProvider = vi.fn(async () => ({ sessionId: 'session-1', turnId: 'turn-1' }))
    const hook = renderHook(() => useAuthorStageController({ storage: opaqueStorage, stageAuthorTool, authorityProvider }))
    let rejection = null
    await act(async () => {
      await hook.result.current.stage(description).catch((error) => { rejection = error })
    })
    expect(rejection).toBeInstanceOf(RangeError)
    expect(rejection.message).toBe('no prototype to read')
    expect(hook.result.current.phase).toBe('interrupted')
    expect(hook.result.current.resumable).toBe(true)
    expect(hook.result.current.error).toBeNull()
    expect(stageAuthorTool).toHaveBeenCalledTimes(1)
    hook.unmount()
  })
})

describe('author start failures: the panel', () => {
  it('C8A-12 a classified failure replaces only the generic line', async () => {
    const shown = [
      ['quota', () => fail('usage limit', { status: 429, errorCode: 'llm_quota_exhausted' }), runProvider, {}],
      ['rate_limited', () => fail('slow down', { status: 429, code: 'llm_rate_limited' }), runProvider, {}],
      ['auth', () => fail('unauthorized', { status: 401 }), runProvider, {}],
      ['network', () => fail('Failed to fetch', {}, TypeError), runProvider, {}],
      ['network', () => fail('Failed to fetch', {}, TypeError), runStage, { resumable: true }],
      ['transport', () => fail('bad', { status: 503 }), runStage, { resumable: true }],
    ]
    for (const [kind, make, run, activity] of shown) {
      panel(await classified(make, run), activity)
      const line = screen.getByTestId('author-start-failure')
      expect(line.textContent, kind).toBe(SENTENCES[kind])
      expect(box().getAttribute('role'), kind).toBe('alert')
      expect(box().textContent, kind).toBe(`${SENTENCES[kind]}${activity.resumable ? 'Resume authoring' : 'Retry R'}${NOTE}`)
      expect(screen.getByRole('button', { name: activity.resumable ? 'Resume authoring' : /Retry/ }), kind).toBeTruthy()
      cleanup()
    }

    // What the server said, who acts and what to do next belong to the generic
    // line: a classified sentence shows none of them.
    const detailed = await classified(() => fail('PROVIDER private text', {
      status: 429,
      body: { error: { error_code: 'llm_quota_exhausted', message: 'PROVIDER private text', next_action: 'Contact billing', actor: 'operator' } },
    }))
    panel(detailed)
    expect(box().textContent).toBe(`${SENTENCES.quota}Retry R${NOTE}`)
    cleanup()

    // A request saved for recovery keeps its recovery controls beside the sentence.
    const failedRequest = { idempotency_key: 'key-1', change_set_id: changeSetId, poll_url: pollUrl, failure: { reason_code: 'customization_author_job_failed' } }
    panel(await classified(() => fail('usage limit', { status: 429, errorCode: 'llm_quota_exhausted' })), { failedRequest })
    expect(screen.getByTestId('author-start-failure').textContent).toBe(SENTENCES.quota)
    expect(box().getAttribute('role')).toBe('alert')
    expect(box().textContent).toBe(
      `${SENTENCES.quota}Request ${changeSetId} is saved for recovery. No new request was started.`
      + `customization_author_job_failedCheck statusStart new attempt${NOTE}`,
    )
    expect(screen.queryByRole('button', { name: /Retry|Resume/ })).toBeNull()
  })

  it('C8A-13 an unclassified failure keeps the whole generic line and no alert role', async () => {
    panel(fail('boom'))
    expect(box().getAttribute('role')).toBeNull()
    expect(box().textContent).toMatch(/^Couldn.t author the tool . boomRetry RYour description is preserved\.$/)
    expect(screen.queryByTestId('author-start-failure')).toBeNull()
    expect(screen.getByRole('button', { name: /Retry/ })).toBeTruthy()
    cleanup()

    // Passed through the controller and left unclassified, it reads the same,
    // with the server's code, next step and actor.
    const busy = await classified(() => fail('server text', {
      status: 409,
      body: { error: { error_code: 'BUSY', message: 'The workspace is busy.', next_action: 'Wait for the other request', actor: 'operator' } },
    }), runStage)
    expect(authorStartFailureOf(busy)).toBeNull()
    panel(busy, { resumable: true })
    expect(box().getAttribute('role')).toBeNull()
    expect(box().textContent).toMatch(
      /^Couldn.t author the tool . The workspace is busy\. BUSYNext: Wait for the other requestSupportResume authoringYour description is preserved\.$/,
    )
    expect(screen.queryByTestId('author-start-failure')).toBeNull()
  })

  it('C8A-14 a grant refusal is said once: by the link gate, or by its sentence', async () => {
    const make = () => fail('forbidden', { status: 403, body: { grant_required: true } })
    const grant = await classified(make)
    expect(authorStartFailureOf(grant)).toEqual(recordOf('grant'))

    // Not resumable: the link gate, and no failure line at all.
    const mounted = panel(grant)
    expect(screen.getAllByText(LINK_GATE)).toHaveLength(1)
    expect(box()).toBeNull()
    expect(screen.queryByTestId('author-start-failure')).toBeNull()

    // The same mounted panel, the same error, now resumable: the gate stays and
    // the failure line reads as it did before, so the grant is not said twice.
    mounted.rerender(view(grant, { resumable: true }))
    expect(screen.getAllByText(LINK_GATE)).toHaveLength(1)
    expect(screen.queryByTestId('author-start-failure')).toBeNull()
    expect(box().getAttribute('role')).toBeNull()
    expect(box().textContent).toMatch(/^Couldn.t author the tool . forbiddenResume authoringYour description is preserved\.$/)
    expect(document.body.textContent).not.toContain(SENTENCES.grant)
    cleanup()

    // Resumable from the start, with no gate showing: the sentence.
    panel(await classified(make, runStage), { resumable: true })
    expect(screen.getByTestId('author-start-failure').textContent).toBe(SENTENCES.grant)
    expect(box().getAttribute('role')).toBe('alert')
    expect(screen.getByRole('button', { name: 'Resume authoring' })).toBeTruthy()
    expect(screen.queryByText(LINK_GATE)).toBeNull()
    cleanup()

    // Resumable while the account is known to be unlinked: the gate already says it.
    panel(await classified(make, runStage), { resumable: true }, { notLinked: true })
    expect(screen.getAllByText(LINK_GATE)).toHaveLength(1)
    expect(screen.queryByTestId('author-start-failure')).toBeNull()
    expect(box().textContent).toMatch(/^Couldn.t author the tool . forbiddenResume authoringYour description is preserved\.$/)
    expect(document.body.textContent).not.toContain(SENTENCES.grant)
    cleanup()

    // Another kind is never hidden by the link gate.
    panel(await classified(() => fail('unauthorized', { status: 401 }), runStage), { resumable: true }, { notLinked: true })
    expect(screen.getAllByText(LINK_GATE)).toHaveLength(1)
    expect(screen.getByTestId('author-start-failure').textContent).toBe(SENTENCES.auth)
  })

  it('C8A-28 the link gate stands in for the grant sentence only while it is on screen', async () => {
    const make = () => fail('forbidden', { status: 403, body: { grant_required: true } })
    // A gate that outranks the link gate hides it, so the sentence has to speak.
    const above = [
      ['the plan gate', () => fail('plan', { status: 403, entitlementRequired: true }), /Your plan doesn.t include tool authoring/],
      ['the daily quota gate', () => fail('daily cap', { status: 429, quotaExceeded: true, limit: 5, used: 5 }), /used your tool authoring for today/],
      ['the service gate', () => fail('bad', { status: 503 }), SERVICE_GATE],
    ]
    for (const [label, first, gate] of above) {
      const grant = await classified(make)
      const mounted = panel(first())
      expect(screen.getByText(gate), label).toBeTruthy()
      // The grant refusal arrives behind that gate, then becomes resumable.
      mounted.rerender(view(grant, {}))
      expect(screen.getByText(gate), label).toBeTruthy()
      expect(screen.queryByText(LINK_GATE), label).toBeNull()
      // Not resumable yet, and the hidden link gate cannot speak: the sentence does.
      expect(screen.getAllByTestId('author-start-failure'), label).toHaveLength(1)
      expect(box().textContent, label).toBe(SENTENCES.grant)
      expect(box().getAttribute('role'), label).toBe('alert')
      mounted.rerender(view(grant, { resumable: true }))
      expect(screen.getByText(gate), label).toBeTruthy()
      expect(screen.queryByText(LINK_GATE), label).toBeNull()
      // Resumable, the failure line carries the sentence, once, with its way forward.
      expect(screen.getAllByTestId('author-start-failure'), label).toHaveLength(1)
      expect(document.querySelectorAll('.inline-error'), label).toHaveLength(1)
      expect(box().textContent, label).toBe(`${SENTENCES.grant}Resume authoring${NOTE}`)
      cleanup()
    }
    // An account whose plan has no authoring never sees the link gate either.
    panel(await classified(make, runStage), { resumable: true }, { notLinked: true, buildEntitled: false })
    expect(screen.getByText(/Your plan doesn.t include tool authoring/)).toBeTruthy()
    expect(screen.queryByText(LINK_GATE)).toBeNull()
    expect(screen.getByTestId('author-start-failure').textContent).toBe(SENTENCES.grant)
  })

  it('C8A-29 a grant refusal the link gate cannot voice is said by its sentence', async () => {
    const make = () => fail('forbidden', { status: 403, body: { grant_required: true } })
    const plan = /Your plan doesn.t include tool authoring/
    const grant = await classified(make)

    // The plan gate holds the screen: the sentence, and nothing to press beside it.
    const mounted = panel(grant, {}, { buildEntitled: false })
    expect(screen.getByText(plan)).toBeTruthy()
    expect(screen.queryByText(LINK_GATE)).toBeNull()
    expect(screen.getAllByTestId('author-start-failure')).toHaveLength(1)
    expect(box().textContent).toBe(SENTENCES.grant)
    expect(box().getAttribute('role')).toBe('alert')
    expect(screen.queryByRole('button', { name: /Retry|Resume/ })).toBeNull()
    // The plan gate goes: the link gate takes over and the sentence leaves.
    mounted.rerender(view(grant, {}, { buildEntitled: true }))
    expect(screen.getAllByText(LINK_GATE)).toHaveLength(1)
    expect(box()).toBeNull()
    expect(screen.queryByTestId('author-start-failure')).toBeNull()
    expect(document.body.textContent).not.toContain(SENTENCES.grant)
    // Back behind the plan gate the sentence speaks again, and leaves with the error.
    mounted.rerender(view(grant, {}, { buildEntitled: false }))
    expect(box().textContent).toBe(SENTENCES.grant)
    mounted.rerender(view(null, { active: true }, { buildEntitled: false }))
    expect(box()).toBeNull()
    expect(screen.queryByTestId('author-start-failure')).toBeNull()
    cleanup()

    // The client's own guess at a grant refusal, with no record behind it, says nothing.
    panel(fail('forbidden', { status: 403, grantRequired: true }), {}, { buildEntitled: false })
    expect(screen.getByText(plan)).toBeTruthy()
    expect(box()).toBeNull()
    expect(screen.queryByTestId('author-start-failure')).toBeNull()
    cleanup()

    // A later failure of another kind, behind the service gate, is that gate's to say.
    const later = panel(fail('bad', { status: 503 }))
    expect(screen.getByText(SERVICE_GATE)).toBeTruthy()
    later.rerender(view(await classified(make), {}))
    expect(box().textContent).toBe(SENTENCES.grant)
    const outage = await classified(() => fail('bad', { status: 503 }))
    expect(authorStartFailureOf(outage)).toEqual(recordOf('transport'))
    later.rerender(view(outage, {}))
    expect(screen.getByText(SERVICE_GATE)).toBeTruthy()
    expect(box()).toBeNull()
    expect(document.body.textContent).not.toContain(SENTENCES.transport)
    cleanup()

    // A submit from the panel clears the gates, and the sentence goes with the
    // gate it stood in for although the controller still holds the refusal.
    const refusal = refusalOf()
    const onAuthor = vi.fn(async () => { throw fail('refused', { secretRefused: true, refusal }) })
    const kept = await classified(make)
    const again = panel(fail('bad', { status: 503 }), {}, { onAuthor })
    again.rerender(view(kept, {}, { onAuthor }))
    expect(screen.getByText(SERVICE_GATE)).toBeTruthy()
    expect(box().textContent).toBe(SENTENCES.grant)
    fireEvent.change(screen.getByLabelText('What should the tool do?'), { target: { value: description } })
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Generate tool' })) })
    await waitFor(() => expect(screen.getByTestId('author-secret-notice-reason').textContent).toBe(refusal.reason))
    expect(onAuthor).toHaveBeenCalledTimes(1)
    expect(screen.queryByText(SERVICE_GATE)).toBeNull()
    expect(screen.queryByText(LINK_GATE)).toBeNull()
    expect(box()).toBeNull()
    expect(screen.queryByTestId('author-start-failure')).toBeNull()
    cleanup()

    // Declared limit, as before this change: a failure line already on screen is
    // not replaced by a grant refusal that arrives behind a gate.
    const stale = panel(fail('boom'))
    expect(box().textContent).toMatch(/^Couldn.t author the tool . boom/)
    stale.rerender(view(fail('bad', { status: 503 }), {}))
    expect(screen.getByText(SERVICE_GATE)).toBeTruthy()
    stale.rerender(view(await classified(make), {}))
    expect(screen.queryByText(LINK_GATE)).toBeNull()
    expect(screen.queryByTestId('author-start-failure')).toBeNull()
    expect(document.querySelectorAll('.inline-error')).toHaveLength(1)
    expect(box().textContent).toMatch(/^Couldn.t author the tool . boom/)
  })

  it('C8A-15 the daily authoring quota keeps its gate', async () => {
    const daily = await classified(() => fail('daily cap', { status: 429, quotaExceeded: true, limit: 5, used: 5, body: { quota_kind: 'daily_author' } }))
    expect(authorStartFailureOf(daily)).toBeNull()
    panel(daily)
    expect(screen.getByText(/used your tool authoring for today/)).toBeTruthy()
    expect(box()).toBeNull()
    expect(screen.queryByTestId('author-start-failure')).toBeNull()
  })

  it('C8A-16 a credential refused while the turn starts shows the secret notice', async () => {
    for (const resumable of [false, true]) {
      const refusal = refusalOf()
      const refused = await classified(() => fail('refused', { secretRefused: true, refusal, status: 401 }))
      expect(authorStartFailureOf(refused)).toBeNull()
      panel(refused, { resumable })
      expect(screen.getByTestId('author-secret-notice'), String(resumable)).toBeTruthy()
      expect(screen.getByTestId('author-secret-notice-reason').textContent).toBe(refusal.reason)
      expect(box(), String(resumable)).toBeNull()
      expect(screen.queryByTestId('author-start-failure')).toBeNull()
      cleanup()
    }
  })

  it('C8A-21 a later failure retires the credential notice on the mounted panel', async () => {
    const refused = fail('refused', { secretRefused: true, refusal: refusalOf() })
    const mounted = panel(refused)
    expect(screen.getByTestId('author-secret-notice')).toBeTruthy()
    // The same refusal, read again because the activity changed, keeps its notice.
    mounted.rerender(view(refused, { resumable: true }))
    expect(screen.getByTestId('author-secret-notice')).toBeTruthy()
    expect(box()).toBeNull()
    // A classified failure arrives: the sentence, and no notice beside it.
    mounted.rerender(view(await classified(() => fail('unauthorized', { status: 401 })), {}))
    expect(screen.queryByTestId('author-secret-notice')).toBeNull()
    expect(screen.getByTestId('author-start-failure').textContent).toBe(SENTENCES.auth)
    // And back: a refusal replaces the sentence with its notice.
    mounted.rerender(view(fail('refused', { secretRefused: true, refusal: refusalOf() }), {}))
    expect(screen.getByTestId('author-secret-notice')).toBeTruthy()
    expect(box()).toBeNull()
    // An unclassified failure retires it as well.
    mounted.rerender(view(fail('boom'), {}))
    expect(screen.queryByTestId('author-secret-notice')).toBeNull()
    expect(box().textContent).toMatch(/^Couldn.t author the tool . boomRetry R/)
  })

  it('C8A-22 a run under way retires the credential notice on the mounted panel', () => {
    const mounted = panel(fail('refused', { secretRefused: true, refusal: refusalOf() }))
    expect(screen.getByTestId('author-secret-notice')).toBeTruthy()
    // The error cleared with nothing running: the refusal is still the last thing that happened.
    mounted.rerender(view(null, { active: false }))
    expect(screen.getByTestId('author-secret-notice')).toBeTruthy()
    mounted.rerender(view(null, { active: true }))
    expect(screen.queryByTestId('author-secret-notice')).toBeNull()
    expect(box()).toBeNull()
  })

  it('C8A-23 a failure read again does not retire a notice raised since', async () => {
    const older = fail('boom')
    const refusal = refusalOf()
    const onAuthor = vi.fn(async () => { throw fail('refused', { secretRefused: true, refusal }) })
    const mounted = panel(older, {}, { onAuthor })
    expect(box().textContent).toMatch(/^Couldn.t author the tool . boom/)
    fireEvent.change(screen.getByLabelText('What should the tool do?'), { target: { value: description } })
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Generate tool' })) })
    await waitFor(() => expect(screen.getByTestId('author-secret-notice-reason').textContent).toBe(refusal.reason))
    expect(onAuthor).toHaveBeenCalledTimes(1)
    // The older failure is read again only because the activity became resumable.
    mounted.rerender(view(older, { resumable: true }, { onAuthor }))
    expect(screen.getByTestId('author-secret-notice')).toBeTruthy()
    expect(box().textContent).toMatch(/^Couldn.t author the tool . boomResume authoring/)
  })

  it('C8A-24 a property set on an error by anyone else is never rendered', async () => {
    const forgeries = [
      { kind: 'quota', message: 'FORGED sentence' },
      { kind: 'quota', message: SENTENCES.quota },
      { kind: 'grant', message: SENTENCES.grant },
      { kind: 'quota' },
      true,
    ]
    for (const authorStartFailure of forgeries) {
      for (const activity of [{}, { resumable: true }]) {
        panel(fail('boom', { authorStartFailure }), activity)
        expect(box().getAttribute('role')).toBeNull()
        expect(screen.queryByTestId('author-start-failure')).toBeNull()
        expect(box().textContent).toMatch(/^Couldn.t author the tool . boom(Retry R|Resume authoring)Your description is preserved\.$/)
        expect(screen.queryByText(LINK_GATE)).toBeNull()
        cleanup()
      }
    }
    // Through the controller as well: an unclassified error with the property stays generic.
    const marked = await classified(() => fail('boom', { authorStartFailure: { kind: 'auth', message: 'FORGED sentence' } }), runStage)
    panel(marked, { resumable: true })
    expect(screen.queryByTestId('author-start-failure')).toBeNull()
    expect(document.body.textContent).not.toContain('FORGED')
  })

  it('C8A-25 a 503 that names the identity binding shows what the server said', async () => {
    const make = () => fail(IDENTITY, {
      status: 503,
      body: { error: { error_code: 'BAD_PARAMS', message: IDENTITY }, reason_code: 'tenant_identity_binding_unavailable' },
    })
    const refused = await runStage(make())
    expect(refused.resumable).toBe(true)
    expect(authorStartFailureOf(refused.error)).toBeNull()
    panel(refused.error, { resumable: true })
    expect(box().getAttribute('role')).toBeNull()
    expect(screen.queryByTestId('author-start-failure')).toBeNull()
    // 27 characters of the generic lead-in, then exactly what the server said.
    expect(box().textContent).toMatch(/^Couldn.t author the tool . Tool authoring requires a verified workspace identity/)
    expect(box().textContent.slice(27)).toBe(`${IDENTITY} BAD_PARAMSResume authoring${NOTE}`)
    expect(document.body.textContent).not.toContain(SENTENCES.transport)
    cleanup()

    // Not resumable, it keeps the calm service gate it had before.
    panel(await classified(make))
    expect(screen.getByText(SERVICE_GATE)).toBeTruthy()
    expect(box()).toBeNull()
    cleanup()

    // The passing outage, resumable, is the one that gets the sentence.
    panel(await classified(() => fail('bad', { status: 503, body: { error: { error_code: 'BAD_PARAMS' }, reason_code: 'customization_harness_unavailable' } }), runStage), { resumable: true })
    expect(screen.getByTestId('author-start-failure').textContent).toBe(SENTENCES.transport)
  })
})

describe('author start failures: the controller and the panel together', () => {
  function Wired({ storage, authorityProvider, stageAuthorTool }) {
    const controller = useAuthorStageController({ storage, authorityProvider, stageAuthorTool })
    return <AuthorPanel onAuthor={controller.stage} stageActivity={controller} onResumeAuthor={controller.resume} />
  }
  const generate = () => act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Generate tool' })) })

  it('C8A-26 a failed turn start reaches the drafter as its sentence, then a notice, then a sentence', async () => {
    const refusal = refusalOf()
    const authorityProvider = vi.fn()
      .mockRejectedValueOnce(fail('unauthorized', { status: 401 }))
      .mockRejectedValueOnce(fail('refused', { secretRefused: true, refusal }))
      .mockRejectedValueOnce(fail('usage limit', { status: 429, errorCode: 'llm_quota_exhausted' }))
    const stageAuthorTool = vi.fn()
    render(<Wired storage={memoryStorage()} authorityProvider={authorityProvider} stageAuthorTool={stageAuthorTool} />)
    fireEvent.change(screen.getByLabelText('What should the tool do?'), { target: { value: description } })

    await generate()
    await waitFor(() => expect(screen.getByTestId('author-start-failure').textContent).toBe(SENTENCES.auth))
    expect(box().getAttribute('role')).toBe('alert')
    expect(box().textContent).toBe(`${SENTENCES.auth}Retry R${NOTE}`)
    expect(screen.queryByTestId('author-secret-notice')).toBeNull()

    await generate()
    await waitFor(() => expect(screen.getByTestId('author-secret-notice-reason').textContent).toBe(refusal.reason))
    expect(box()).toBeNull()
    expect(screen.queryByTestId('author-start-failure')).toBeNull()

    await generate()
    await waitFor(() => expect(screen.getByTestId('author-start-failure').textContent).toBe(SENTENCES.quota))
    expect(screen.queryByTestId('author-secret-notice')).toBeNull()
    expect(authorityProvider).toHaveBeenCalledTimes(3)
    expect(stageAuthorTool).not.toHaveBeenCalled()
    expect(screen.getByLabelText('What should the tool do?').value).toBe(description)
  })

  it('C8A-27 a stage request refused before acceptance offers Resume beside its sentence', async () => {
    const cases = [
      [() => fail('bad', { status: 503 }), SENTENCES.transport, true],
      [() => fail('Failed to fetch', {}, TypeError), SENTENCES.network, true],
      [() => fail(IDENTITY, { status: 503, body: { error: { error_code: 'BAD_PARAMS', message: IDENTITY }, reason_code: 'tenant_identity_binding_unavailable' } }), IDENTITY, false],
    ]
    for (const [make, sentence, isClassified] of cases) {
      const stageAuthorTool = vi.fn(async () => { throw make() })
      const authorityProvider = vi.fn(async () => ({ sessionId: 'session-1', turnId: 'turn-1' }))
      render(<Wired storage={memoryStorage()} authorityProvider={authorityProvider} stageAuthorTool={stageAuthorTool} />)
      fireEvent.change(screen.getByLabelText('What should the tool do?'), { target: { value: description } })
      await generate()
      await waitFor(() => expect(screen.getByRole('button', { name: 'Resume authoring' })).toBeTruthy())
      expect(box().textContent, sentence).toContain(sentence)
      expect(box().getAttribute('role'), sentence).toBe(isClassified ? 'alert' : null)
      expect(!!screen.queryByTestId('author-start-failure'), sentence).toBe(isClassified)
      expect(stageAuthorTool, sentence).toHaveBeenCalledTimes(1)
      cleanup()
    }
  })

  it('C8A-31 a grant refusal that arrives behind the service gate is said by its sentence', async () => {
    // The controller is driven directly, as a caller other than this panel
    // would drive it: nothing clears the panel's gates between the two failures.
    const handle = { controller: null }
    const storage = memoryStorage()
    function Driven({ authorityProvider, stageAuthorTool }) {
      const controller = useAuthorStageController({ storage, authorityProvider, stageAuthorTool })
      handle.controller = controller
      return <AuthorPanel onAuthor={controller.stage} stageActivity={controller} onResumeAuthor={controller.resume} />
    }
    const authorityProvider = vi.fn()
      .mockRejectedValueOnce(fail('bad', { status: 503 }))
      .mockRejectedValueOnce(fail('forbidden', { status: 403, body: { grant_required: true } }))
    const stageAuthorTool = vi.fn()
    render(<Driven authorityProvider={authorityProvider} stageAuthorTool={stageAuthorTool} />)

    await act(async () => { await handle.controller.stage(description) })
    await waitFor(() => expect(screen.getByText(SERVICE_GATE)).toBeTruthy())
    expect(box()).toBeNull()
    expect(screen.queryByTestId('author-start-failure')).toBeNull()

    await act(async () => { await handle.controller.stage(description) })
    await waitFor(() => expect(screen.getAllByTestId('author-start-failure')).toHaveLength(1))
    expect(authorStartFailureOf(handle.controller.error)).toEqual(recordOf('grant'))
    expect(handle.controller.resumable).toBe(false)
    expect(screen.getByText(SERVICE_GATE)).toBeTruthy()
    expect(screen.queryByText(LINK_GATE)).toBeNull()
    expect(box().textContent).toBe(SENTENCES.grant)
    expect(box().getAttribute('role')).toBe('alert')
    expect(authorityProvider).toHaveBeenCalledTimes(2)
    expect(stageAuthorTool).not.toHaveBeenCalled()
  })
})
