import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('../telemetry.js', () => ({ track: vi.fn() }))

import {
  clearComposerDrafts,
  composerDraftAccountScope,
  composerDraftKey,
  composerDraftStorage,
  createComposerDraft,
} from './composerDraft.js'

const SECRET = `AKIA${'A'.repeat(16)}`
const drafts = []
const memoryStorage = () => {
  const values = new Map()
  return {
    get length() { return values.size },
    key: (index) => [...values.keys()][index] ?? null,
    getItem: vi.fn((key) => values.get(key) ?? null),
    setItem: vi.fn((key, value) => values.set(key, value)),
    removeItem: vi.fn((key) => values.delete(key)),
  }
}
function draft(storage, options = {}) {
  const result = createComposerDraft({ storage, accountScope: 'guest', route: '/try', ...options })
  drafts.push(result)
  return result
}
beforeEach(() => vi.useFakeTimers())
afterEach(() => {
  for (const item of drafts.splice(0)) item.dispose({ save: false })
  vi.useRealTimers()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('guarded command-bar drafts', () => {
  it('debounces for 400ms and restores the exact latest text across instances', () => {
    const storage = memoryStorage()
    const writer = draft(storage)
    writer.update('draw a fence')
    vi.advanceTimersByTime(300)
    writer.update('draw a 20 ft fence\nwith a gate')
    vi.advanceTimersByTime(399)
    expect(storage.setItem).not.toHaveBeenCalled()
    vi.advanceTimersByTime(1)
    expect(storage.setItem).toHaveBeenCalledExactlyOnceWith(writer.key, 'draw a 20 ft fence\nwith a gate')
    expect(draft(storage).restore()).toBe('draw a 20 ft fence\nwith a gate')
  })

  it('caps saved and restored text at 4000 characters', () => {
    const storage = memoryStorage()
    const writer = draft(storage)
    writer.update('a'.repeat(5000))
    writer.flush()
    expect(storage.getItem(writer.key)).toHaveLength(4000)
    storage.setItem(writer.key, 'b'.repeat(5000))
    expect(writer.restore()).toHaveLength(4000)
  })

  it.each([SECRET, `api_key: ${'x'.repeat(24)}`, `${'a'.repeat(4001)}\n${SECRET}`])(
    'never writes credential-shaped text, including beyond the cap', (text) => {
      const storage = memoryStorage()
      const writer = draft(storage)
      writer.update('innocent pending prefix')
      writer.update(text)
      vi.advanceTimersByTime(500)
      writer.flush()
      expect(storage.setItem).not.toHaveBeenCalled()
      expect(writer.restore()).toBe('')
    },
  )

  it('removes a previous safe draft when it is replaced by a credential', () => {
    const storage = memoryStorage()
    const writer = draft(storage)
    writer.update('previous safe draft')
    writer.flush()
    storage.setItem.mockClear()
    writer.update(SECRET)
    vi.runAllTimers()
    expect(storage.setItem).not.toHaveBeenCalled()
    expect(draft(storage).restore()).toBe('')
  })

  it('refuses and removes credential-shaped storage on restore', () => {
    const storage = memoryStorage()
    const reader = draft(storage)
    storage.setItem(reader.key, SECRET)
    expect(reader.restore()).toBe('')
    expect(storage.getItem(reader.key)).toBeNull()
  })

  it('isolates both account and route, including separator-shaped scope names', () => {
    const storage = memoryStorage()
    const a = draft(storage, { accountScope: 'account:a' })
    a.update('only for a on try')
    a.flush()
    expect(draft(storage, { accountScope: 'account:a' }).restore()).toBe('only for a on try')
    expect(draft(storage, { accountScope: 'account:b' }).restore()).toBe('')
    expect(draft(storage, { accountScope: 'account:a', route: '/app' }).restore()).toBe('')
    expect(draft(storage).restore()).toBe('')
    expect(composerDraftKey('a:b', 'c')).not.toBe(composerDraftKey('a', 'b:c'))
  })

  it('flushes on hidden, ignores visible, and detaches the listener on dispose', () => {
    const storage = memoryStorage()
    const document = new EventTarget()
    document.visibilityState = 'visible'
    const remove = vi.spyOn(document, 'removeEventListener')
    const writer = draft(storage, { document })
    writer.update('survives leaving the tab')
    document.dispatchEvent(new Event('visibilitychange'))
    expect(storage.setItem).not.toHaveBeenCalled()
    document.visibilityState = 'hidden'
    document.dispatchEvent(new Event('visibilitychange'))
    expect(writer.restore()).toBe('survives leaving the tab')
    writer.dispose()
    expect(remove).toHaveBeenCalledWith('visibilitychange', expect.any(Function))
    writer.update('disposed')
    vi.runAllTimers()
    expect(storage.setItem).toHaveBeenCalledTimes(1)
  })

  it('clears saved and pending text without a delayed resurrection', () => {
    const storage = memoryStorage()
    const writer = draft(storage)
    writer.update('saved')
    writer.flush()
    writer.update('pending')
    writer.clear()
    vi.runAllTimers()
    writer.flush()
    expect(writer.restore()).toBe('')
    writer.update('another')
    writer.update('')
    vi.runAllTimers()
    expect(writer.restore()).toBe('')
    expect(storage.setItem).toHaveBeenCalledTimes(1)
  })

  it('treats throwing reads, writes, removes and enumeration as a no-op', () => {
    const fail = () => { throw new Error('storage denied') }
    const storage = { getItem: fail, setItem: fail, removeItem: fail, key: fail, get length() { return fail() } }
    const writer = draft(storage)
    expect(writer.restore()).toBe('')
    expect(() => { writer.update('hello'); writer.flush(); writer.clear(); clearComposerDrafts(storage) }).not.toThrow()
    expect(composerDraftAccountScope(storage)).toBeNull()
  })

  it('survives a throwing browser storage getter', () => {
    const descriptor = Object.getOwnPropertyDescriptor(globalThis, 'localStorage')
    Object.defineProperty(globalThis, 'localStorage', { configurable: true, get() { throw new Error('denied') } })
    try {
      expect(composerDraftStorage()).toBeNull()
      expect(() => createComposerDraft().dispose()).not.toThrow()
      expect(() => clearComposerDrafts()).not.toThrow()
    } finally {
      if (descriptor) Object.defineProperty(globalThis, 'localStorage', descriptor)
      else delete globalThis.localStorage
    }
  })

  it('uses a stable guest or issuer/subject scope, never a bearer or shared account fallback', () => {
    const storage = memoryStorage()
    expect(composerDraftAccountScope(storage)).toBe('guest')
    const token = (sub) => `header.${btoa(JSON.stringify({ iss: 'https://auth.example/', sub }))}.signature`
    const a = token('a')
    storage.setItem('leaf.jwt', a)
    const scopeA = composerDraftAccountScope(storage)
    expect(scopeA).toBe('account:["https://auth.example/","a"]')
    expect(composerDraftKey(scopeA, '/try')).not.toContain(a)
    storage.setItem('leaf.jwt', token('b'))
    expect(composerDraftAccountScope(storage)).not.toBe(scopeA)
    storage.setItem('leaf.jwt', 'opaque-token')
    expect(composerDraftAccountScope(storage)).toBeNull()
    expect(draft(storage, { accountScope: null }).key).toBeNull()
  })

  it('clears every stored scope and live timer while preserving unrelated keys', () => {
    const storage = memoryStorage()
    const a = draft(storage, { accountScope: 'a' })
    const b = draft(storage, { accountScope: 'b', route: '/app' })
    a.update('saved a'); a.flush()
    b.update('saved b'); b.flush()
    a.update('pending a')
    storage.setItem('leaf.unrelated', 'keep')
    clearComposerDrafts(storage)
    vi.runAllTimers()
    expect(a.restore()).toBe('')
    expect(b.restore()).toBe('')
    expect(storage.getItem('leaf.unrelated')).toBe('keep')
    expect(storage.length).toBe(1)
  })
})
