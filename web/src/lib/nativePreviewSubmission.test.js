import { webcrypto } from 'node:crypto'
import { afterEach, expect, it, vi } from 'vitest'
import { nativePreviewSubmissionKey } from './nativePreviewSubmission.js'

const identity = ['account-a', 'org-a', 'actor-a', 'project-a', 16, 'a'.repeat(64)]
const store = () => {
  const rows = new Map()
  return { rows, getItem: key => rows.get(key) ?? null, setItem: (key, value) => rows.set(key, value) }
}
afterEach(() => vi.unstubAllGlobals())

it('recovers the same key after a new caller loses its in-memory session', async () => {
  vi.stubGlobal('crypto', webcrypto)
  vi.stubGlobal('navigator', { locks: { request: async (_name, callback) => callback() } })
  const storage = store()
  const original = await nativePreviewSubmissionKey(storage, identity)
  expect(await nativePreviewSubmissionKey(storage, [...identity])).toBe(original)
  expect(storage.rows.size).toBe(1)
  expect([...storage.rows.values()].map(JSON.parse)).toEqual([{ version: 1, key: original }])
})

it('separates every account, organization, actor, project, revision and document', async () => {
  vi.stubGlobal('crypto', webcrypto)
  vi.stubGlobal('navigator', { locks: { request: async (_name, callback) => callback() } })
  const storage = store(), keys = [await nativePreviewSubmissionKey(storage, identity)]
  for (const [index, value] of identity.entries()) {
    const changed = [...identity]
    changed[index] = index === 4 ? 17 : index === 5 ? 'b'.repeat(64) : `${value}-other`
    keys.push(await nativePreviewSubmissionKey(storage, changed))
  }
  expect(new Set(keys).size).toBe(7)
})

it('rotates only when the caller explicitly requests a terminal-job retry', async () => {
  vi.stubGlobal('crypto', webcrypto)
  vi.stubGlobal('navigator', { locks: { request: async (_name, callback) => callback() } })
  const storage = store(), original = await nativePreviewSubmissionKey(storage, identity)
  const retry = await nativePreviewSubmissionKey(storage, identity, original)
  expect(retry).not.toBe(original)
  expect(await nativePreviewSubmissionKey(storage, identity)).toBe(retry)
  expect(await nativePreviewSubmissionKey(storage, identity, original)).toBe(retry)
})

it('refuses missing storage, failed persistence and corrupt saved keys', async () => {
  vi.stubGlobal('crypto', webcrypto)
  vi.stubGlobal('navigator', { locks: { request: async (_name, callback) => callback() } })
  await expect(nativePreviewSubmissionKey(null, identity)).rejects.toThrow()
  await expect(nativePreviewSubmissionKey({ getItem: () => null, setItem: () => {} }, identity)).rejects.toThrow()
  for (const value of ['bad json', '{}', JSON.stringify({ version: 1, key: 'foreign' }), 'x'.repeat(129)]) {
    await expect(nativePreviewSubmissionKey({ getItem: () => value }, identity)).rejects.toThrow()
  }
  await expect(nativePreviewSubmissionKey(store(), [...identity.slice(0, 5), 'bad digest'])).rejects.toThrow()
})

it('refuses submission identity creation without browser lock support', async () => {
  vi.stubGlobal('crypto', webcrypto)
  vi.stubGlobal('navigator', {})
  await expect(nativePreviewSubmissionKey(store(), identity)).rejects.toThrow('locking is unavailable')
})
