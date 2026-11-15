import { webcrypto } from 'node:crypto'
import { afterEach, expect, it, vi } from 'vitest'
import { nativePreviewSubmissionKey } from './nativePreviewSubmission.js'

const identity = ['account-a', 'org-a', 'actor-a', 'project-a', 16, 'a'.repeat(64)]
const store = () => {
  const rows = new Map()
  return { rows, getItem: key => rows.get(key) ?? null, setItem: (key, value) => rows.set(key, value) }
}
afterEach(() => vi.unstubAllGlobals())

it('recovers the same key after a new caller loses its in-memory session', () => {
  vi.stubGlobal('crypto', webcrypto)
  const storage = store()
  const original = nativePreviewSubmissionKey(storage, identity)
  expect(nativePreviewSubmissionKey(storage, [...identity])).toBe(original)
  expect(storage.rows.size).toBe(1)
  expect([...storage.rows.values()].map(JSON.parse)).toEqual([{ version: 1, key: original }])
})

it('separates every account, organization, actor, project, revision and document', () => {
  vi.stubGlobal('crypto', webcrypto)
  const storage = store(), keys = [nativePreviewSubmissionKey(storage, identity)]
  identity.forEach((value, index) => {
    const changed = [...identity]
    changed[index] = index === 4 ? 17 : index === 5 ? 'b'.repeat(64) : `${value}-other`
    keys.push(nativePreviewSubmissionKey(storage, changed))
  })
  expect(new Set(keys).size).toBe(7)
})

it('rotates only when the caller explicitly requests a terminal-job retry', () => {
  vi.stubGlobal('crypto', webcrypto)
  const storage = store(), original = nativePreviewSubmissionKey(storage, identity)
  const retry = nativePreviewSubmissionKey(storage, identity, true)
  expect(retry).not.toBe(original)
  expect(nativePreviewSubmissionKey(storage, identity)).toBe(retry)
})

it('refuses missing storage, failed persistence and corrupt saved keys', () => {
  vi.stubGlobal('crypto', webcrypto)
  expect(() => nativePreviewSubmissionKey(null, identity)).toThrow()
  expect(() => nativePreviewSubmissionKey({ getItem: () => null, setItem: () => {} }, identity)).toThrow()
  for (const value of ['bad json', '{}', JSON.stringify({ version: 1, key: 'foreign' }), 'x'.repeat(129)]) {
    expect(() => nativePreviewSubmissionKey({ getItem: () => value }, identity)).toThrow()
  }
  expect(() => nativePreviewSubmissionKey(store(), [...identity.slice(0, 5), 'bad digest'])).toThrow()
})
