import test from 'node:test'
import assert from 'node:assert/strict'
import { runSurfaceConfigProof, surfaceConfigProofPage } from './verify_surface_config_live.mjs'

const sha = 'a'.repeat(40)
const baseUrl = 'https://platform-staging.leafdesign.ai'
const copy = (value) => structuredClone(value)
const source = (n) => ({ sha256: n.toString(16).padStart(64, '0'), authored_at: `2026-09-11T12:00:${String(n).padStart(2, '0')}Z` })

function fixture(options = {}) {
  const baseline = options.baseline ?? { cad: { authoring: true, toolbar: { ribbon: true } },
    sheets: { chrome: { tab: 'My sheets' } } }
  let overlay = copy(baseline), receipt = source(1), rendered = true, writes = 0, reloads = 0
  const events = []
  const transport = {
    async getSession(mock) {
      assert.equal(mock, false)
      events.push('identity')
      if (options.authFailure) throw new Error('Bearer secret-auth cookie=private')
      return { tenant: options.wrongTenant ? 'another' : 'dedicated-fold-fixture', tier: 'admin', org: 'private-org' }
    },
    async getSurfaceConfig(mock, args) {
      assert.equal(mock, false)
      assert.equal(args.fresh, true)
      events.push('get')
      return { surfaces: copy(overlay), source: copy(receipt) }
    },
    async submitSurfaceConfig(value) {
      events.push('post')
      writes++
      if (writes === 2 && options.restoreFailure) throw new Error('cookie=private-restoration')
      if (writes === 1 && options.readbackMismatch) return source(2)
      overlay = copy(value)
      receipt = source(writes + 1)
      if (writes === 1 && options.concurrentAtCommit) {
        overlay.sheets.chrome.tab = 'Other writer private text'
        receipt = source(8)
      }
      if (writes === 1 && options.lostResponse) throw new Error('Bearer lost-secret')
      return options.badReceipt && writes === 1 ? {} : copy(receipt)
    },
  }
  const page = {
    url: () => `${baseUrl}/try?surface=cad`,
    async reload() {
      events.push('reload')
      reloads++
      rendered = overlay.cad.authoring
      if (options.invisibleChange && reloads === 1) rendered = true
      if (options.concurrentAfterReload && reloads === 1) {
        overlay.sheets.chrome.tab = 'Private concurrent value'
        receipt = source(9)
      }
      if (options.restoreInvisible && reloads === 2) rendered = false
    },
    locator(selector) {
      return {
        async waitFor({ state }) {
          if (selector === '#workspace-tab-author'
            && (state === 'visible') !== (rendered && !options.invisible)) throw new Error('private DOM error')
        },
        async isVisible() { return selector === '#product-surface-tab-cad' || (rendered && !options.invisible) },
        async getAttribute(name) { assert.equal(name, 'aria-selected'); return 'true' },
      }
    },
    request: {
      async get(path) {
        assert.equal(path, '/api/ready')
        events.push('deployment')
        return {
          ok: () => true,
          status: () => 200,
          url: () => `${baseUrl}/api/ready`,
          json: async () => ({ source_revision: options.wrongSource ? 'b'.repeat(40) : sha,
            environment: 'staging', ready: true, degraded_mode: false }),
        }
      },
    },
  }
  const adapted = surfaceConfigProofPage(page, { transport, dedicatedTenantId: 'dedicated-fold-fixture' })
  return { run: () => runSurfaceConfigProof({ page: adapted, baseUrl, expectedSourceSha: sha }),
    events, baseline, overlay: () => overlay, writes: () => writes, reloads: () => reloads }
}

test('mutation, exact readback, visible change, source check and restoration', async () => {
  const f = fixture()
  const result = await f.run()
  assert.equal(result.ok, true)
  assert.equal(result.restoration, 'verified')
  assert.equal(result.current.equalsBaseline, true)
  assert.deepEqual(f.overlay(), f.baseline)
  assert.equal(f.writes(), 2)
  assert.equal(f.reloads(), 2)
  const post = f.events.indexOf('post')
  assert.equal(f.events[post + 1], 'get')
  assert.ok(result.checks.includes('visible_change'))
  assert.ok(result.checks.includes('application_source_unchanged'))
})

test('authentication failure refuses before mutation and sanitizes errors', async () => {
  const f = fixture({ authFailure: true })
  const result = await f.run()
  assert.equal(result.ok, false)
  assert.equal(f.writes(), 0)
  assert.equal(result.restoration, 'not_required')
  assert.doesNotMatch(JSON.stringify(result), /Bearer|secret-auth|cookie|private/)
})

test('lost POST response reads back successfully without retrying the candidate', async () => {
  const f = fixture({ lostResponse: true })
  const result = await f.run()
  assert.equal(result.ok, true)
  assert.equal(result.mutation, 'recovered_by_readback')
  assert.equal(f.writes(), 2)
  const post = f.events.indexOf('post')
  assert.equal(f.events[post + 1], 'get')
  assert.doesNotMatch(JSON.stringify(result), /lost-secret/)
})

test('readback mismatch fails even with a successful POST receipt', async () => {
  const f = fixture({ readbackMismatch: true })
  const result = await f.run()
  assert.equal(result.ok, false)
  assert.equal(result.failure, 'readback_mismatch')
  assert.equal(result.restoration, 'verified')
  assert.equal(f.writes(), 1)
})

for (const option of ['concurrentAtCommit', 'concurrentAfterReload']) {
  test(`${option}: concurrent change stops cleanup without overwriting`, async () => {
    const f = fixture({ [option]: true })
    const result = await f.run()
    assert.equal(result.ok, false)
    assert.equal(result.restoration, 'conflict_no_overwrite')
    assert.equal(f.writes(), 1)
    assert.equal(result.current.equalsBaseline, false)
    assert.equal(result.current.equalsCandidate, false)
    assert.doesNotMatch(JSON.stringify(result), /Private|Other writer/)
  })
}

test('restoration failure fails proof with explicit current candidate evidence', async () => {
  const f = fixture({ restoreFailure: true })
  const result = await f.run()
  assert.equal(result.ok, false)
  assert.equal(result.restoration, 'failed')
  assert.equal(result.restorationFailure, 'restoration_readback_mismatch')
  assert.equal(result.current.readable, true)
  assert.equal(result.current.equalsCandidate, true)
  assert.equal(result.current.equalsBaseline, false)
  assert.match(result.current.overlayDigest, /^[a-f0-9]{64}$/)
  assert.doesNotMatch(JSON.stringify(result), /cookie|private-restoration/)
})

for (const options of [{ invisible: true }, { baseline: { cad: { authoring: 'true' } } },
  { baseline: { cad: { authoring: true, unsupported: true } } },
  { wrongSource: true }, { wrongTenant: true }]) {
  test(`preflight refuses ${JSON.stringify(options)}`, async () => {
    const f = fixture(options)
    const result = await f.run()
    assert.equal(result.ok, false)
    assert.equal(f.writes(), 0)
  })
}

test('missing returned receipt fails and still restores', async () => {
  const f = fixture({ badReceipt: true })
  const result = await f.run()
  assert.equal(result.ok, false)
  assert.equal(result.failure, 'source_receipt_mismatch')
  assert.equal(result.restoration, 'verified')
})

test('HTTP success with no visible change fails and restores', async () => {
  const f = fixture({ invisibleChange: true })
  const result = await f.run()
  assert.equal(result.ok, false)
  assert.equal(result.restoration, 'verified')
  assert.deepEqual(f.overlay(), f.baseline)
})

test('visible restoration failure fails despite correct GET restoration', async () => {
  const f = fixture({ restoreInvisible: true })
  const result = await f.run()
  assert.equal(result.ok, false)
  assert.equal(result.restoration, 'failed')
  assert.equal(result.current.equalsBaseline, true)
})
