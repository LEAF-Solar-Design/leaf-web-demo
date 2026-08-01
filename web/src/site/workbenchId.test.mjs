import assert from 'node:assert/strict'
import { describe, it } from 'node:test'
import {
  CANONICAL_DRAWING_ID,
  WORKBENCH_ID_KEY,
  liveDrawingId,
  rememberLiveDrawingId,
} from './workbenchId.js'

function scopeWith(initial, { throwOnGet = false, throwOnSet = false } = {}) {
  const store = new Map(Object.entries(initial ?? {}))
  return {
    crypto: { randomUUID: () => '00000000-1111-2222-3333-444444444444' },
    sessionStorage: {
      getItem(key) {
        if (throwOnGet) throw new Error('storage disabled')
        return store.has(key) ? store.get(key) : null
      },
      setItem(key, value) {
        // Real browsers throw QuotaExceededError here, and Safari private
        // mode historically threw on every write.
        if (throwOnSet) throw new Error('quota exceeded')
        store.set(key, value)
      },
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

  it('accepts the canonical ids the shared server rule accepts', () => {
    for (const id of ['demo', 'cat-workbench-abc', 'org_leaf_demo', 'a', '0'.repeat(63)]) {
      assert.equal(liveDrawingId(scopeWith({ [WORKBENCH_ID_KEY]: id })), id, id)
      assert.ok(CANONICAL_DRAWING_ID.test(id), id)
    }
  })

  it('rejects every id the server would reject without replacing it', () => {
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
      assert.equal(liveDrawingId(scope), null, id)
      assert.equal(scope._store.get(WORKBENCH_ID_KEY), id, id)
      assert.ok(!CANONICAL_DRAWING_ID.test(id), id)
    }
  })

  it('returns an honest empty state when nothing is seeded', () => {
    const scope = scopeWith({})
    assert.equal(liveDrawingId(scope), null)
    assert.equal(scope._store.has(WORKBENCH_ID_KEY), false)
  })

  it('returns an honest empty state when reading storage throws', () => {
    assert.equal(liveDrawingId(scopeWith({}, { throwOnGet: true })), null)
  })

  it('persists only a server-canonical completed upload id', () => {
    const scope = scopeWith({})
    assert.equal(rememberLiveDrawingId('u-completed-upload', scope), true)
    assert.equal(scope._store.get(WORKBENCH_ID_KEY), 'u-completed-upload')
    assert.equal(rememberLiveDrawingId('../invented', scope), false)
    assert.equal(scope._store.get(WORKBENCH_ID_KEY), 'u-completed-upload')
  })

  it('keeps the in-memory upload usable when persistence is unavailable', () => {
    assert.equal(rememberLiveDrawingId('u-completed-upload', scopeWith({}, { throwOnSet: true })), false)
  })

  it('rejects a trailing newline, unlike a bare python re.match', () => {
    // Documented divergence: python's re.match("$") tolerates one trailing
    // newline, so `demo\n` would pass a naive server-side match. This rule is
    // strictly tighter, which is the safe direction for a value that becomes a
    // storage key.
    assert.ok(!CANONICAL_DRAWING_ID.test('demo\n'))
    assert.equal(liveDrawingId(scopeWith({ [WORKBENCH_ID_KEY]: 'demo\n' })), null)
  })
})
