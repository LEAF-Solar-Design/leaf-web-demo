import assert from 'node:assert/strict'
import { describe, it } from 'node:test'
import {
  CANONICAL_DRAWING_ID,
  WORKBENCH_ID_KEY,
  freshDrawingId,
  liveDrawingId,
} from './workbenchId.js'

function scopeWith(initial, { throwOnGet = false } = {}) {
  const store = new Map(Object.entries(initial ?? {}))
  return {
    crypto: { randomUUID: () => '00000000-1111-2222-3333-444444444444' },
    sessionStorage: {
      getItem(key) {
        if (throwOnGet) throw new Error('storage disabled')
        return store.has(key) ? store.get(key) : null
      },
      setItem(key, value) { store.set(key, value) },
    },
    _store: store,
  }
}

describe('live workbench drawing id', () => {
  it('uses a seeded acceptance id exactly as given', () => {
    // The exact shape the protected staging acceptance driver seeds.
    const seeded = 'acceptance-authored-accept-20260101-r1-a'
    const scope = scopeWith({ [WORKBENCH_ID_KEY]: seeded })
    assert.equal(liveDrawingId(scope), seeded)
    assert.equal(scope._store.get(WORKBENCH_ID_KEY), seeded)
  })

  it('accepts any id the shared server rule accepts', () => {
    for (const id of ['demo', 'cat-workbench-abc', 'org_leaf_demo', 'a', '0'.repeat(63)]) {
      assert.equal(liveDrawingId(scopeWith({ [WORKBENCH_ID_KEY]: id })), id, id)
      assert.ok(CANONICAL_DRAWING_ID.test(id), id)
    }
  })

  it('replaces any id the server would reject, and persists the replacement', () => {
    const rejected = [
      '../evil',
      'Evil-Drawing',
      'has space',
      'dot.separated',
      'back\\slash',
      '-leading-hyphen',
      '_leading-underscore',
      '',
      'x'.repeat(64),
    ]
    for (const id of rejected) {
      const scope = scopeWith({ [WORKBENCH_ID_KEY]: id })
      const resolved = liveDrawingId(scope)
      assert.notEqual(resolved, id, id)
      assert.match(resolved, /^cat-workbench-[0-9a-z-]+$/, id)
      assert.equal(scope._store.get(WORKBENCH_ID_KEY), resolved, id)
      assert.ok(!CANONICAL_DRAWING_ID.test(id), id)
    }
  })

  it('mints and persists a fresh id when nothing is seeded', () => {
    const scope = scopeWith({})
    const resolved = liveDrawingId(scope)
    assert.match(resolved, /^cat-workbench-[0-9a-z-]+$/)
    assert.equal(scope._store.get(WORKBENCH_ID_KEY), resolved)
    assert.ok(CANONICAL_DRAWING_ID.test(resolved))
  })

  it('falls back to a fresh id when storage throws', () => {
    const resolved = liveDrawingId(scopeWith({}, { throwOnGet: true }))
    assert.match(resolved, /^cat-workbench-[0-9a-z-]+$/)
  })

  it('mints ids that are themselves canonical without crypto.randomUUID', () => {
    const scope = scopeWith({})
    scope.crypto = {}
    assert.ok(CANONICAL_DRAWING_ID.test(freshDrawingId(scope)))
  })
})
