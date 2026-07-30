import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { describe, it } from 'node:test'
import {
  CANONICAL_DRAWING_ID,
  WORKBENCH_ID_KEY,
  freshDrawingId,
  liveDrawingId,
  operatorDrawingId,
} from './workbenchId.js'

function scopeWith(initial, { throwOnGet = false, throwOnSet = false } = {}) {
  const store = new Map(Object.entries(initial ?? {}))
  return {
    crypto: { randomUUID: () => '00000000-1111-2222-3333-444444444444' },
    location: { search: '' },
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
  it('uses the seeded id for the operator controller and surface', () => {
    const seeded = 'acceptance-authored-accept-20260101-r1-a'
    const scope = scopeWith({ [WORKBENCH_ID_KEY]: seeded })
    assert.equal(operatorDrawingId(false, scope), seeded)
  })

  it('keeps explicit proof builds and proof links on the fixture drawing', () => {
    const scope = scopeWith({ [WORKBENCH_ID_KEY]: 'acceptance-authored-accept-20260101-r1-a' })
    assert.equal(operatorDrawingId(true, scope), 'cat-panels')
    scope.location.search = '?proof=1'
    assert.equal(operatorDrawingId(false, scope), 'cat-panels')
  })

  it('seeds both SiteRoot controller inputs from the shared operator id', () => {
    // SiteRoot owns the drawing controller before ToolCast mounts. Pin both
    // inputs because a hard-coded cat-panels value here bypasses a valid
    // acceptance seed whenever the tenant session is already active.
    const siteRoot = readFileSync(new URL('./SiteRoot.jsx', import.meta.url), 'utf8')
    assert.match(siteRoot, /const OPERATOR_DRAWING_ID = operatorDrawingId\(/)
    assert.match(siteRoot, /drawing_id: OPERATOR_DRAWING_ID/)
    assert.match(siteRoot, /drawingId=\{scene === 'tool' \? OPERATOR_DRAWING_ID : 'rooftop_demo'\}/)
  })

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

  it('falls back to a fresh id when reading storage throws', () => {
    const resolved = liveDrawingId(scopeWith({}, { throwOnGet: true }))
    assert.match(resolved, /^cat-workbench-[0-9a-z-]+$/)
  })

  it('still returns a usable id when persisting throws', () => {
    // A write failure must not leave the surface without a drawing id. The id
    // is then per-load rather than per-session, which is the honest outcome
    // when the browser refuses to remember anything.
    const resolved = liveDrawingId(scopeWith({}, { throwOnSet: true }))
    assert.match(resolved, /^cat-workbench-[0-9a-z-]+$/)
    assert.ok(CANONICAL_DRAWING_ID.test(resolved))
  })

  it('rejects a trailing newline, unlike a bare python re.match', () => {
    // Documented divergence: python's re.match("$") tolerates one trailing
    // newline, so `demo\n` would pass a naive server-side match. This rule is
    // strictly tighter, which is the safe direction for a value that becomes a
    // storage key.
    assert.ok(!CANONICAL_DRAWING_ID.test('demo\n'))
    assert.notEqual(liveDrawingId(scopeWith({ [WORKBENCH_ID_KEY]: 'demo\n' })), 'demo\n')
  })

  it('mints ids that are themselves canonical without crypto.randomUUID', () => {
    const scope = scopeWith({})
    scope.crypto = {}
    assert.ok(CANONICAL_DRAWING_ID.test(freshDrawingId(scope)))
  })
})
