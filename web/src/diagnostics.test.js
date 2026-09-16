// @vitest-environment jsdom
import { describe, expect, it } from 'vitest'
import { collectRefusals, composeDiagnostics, DIAGNOSTICS_TITLE, taskRevisionOf } from './diagnostics.js'

const live = {
  buildHash: 'abc1234', mode: 'live', servedSourceSha: 'dddddddddddddddddddddddddddddddddddddddd', taskRevision: 'leaf-platform-app:838',
  pathname: '/workspace', at: '2026-09-15T12:00:00Z', sessionState: 'signed in',
  editLock: { state: 'read failed', status: 409, errorCode: 'LOCKED', errorId: '0123456789abcdef', at: '2026-09-15T11:00:00Z' },
  failures: [{ at: '2026-09-15T10:00:00Z', method: 'POST', endpointClass: '/api/drawing', status: 403, errorCode: 'DENIED', errorId: 'fedcba9876543210' }],
  refusals: [{ label: 'Save', code: 'REASONS.writeLocked' }],
}

describe('support diagnostics', () => {
  it('composes the full live block in order', () => {
    expect(composeDiagnostics(live)).toBe([
      DIAGNOSTICS_TITLE, 'build abc1234', 'mode live', 'served dddddddddddddddddddddddddddddddddddddddd', 'task leaf-platform-app:838',
      'page /workspace', 'time 2026-09-15T12:00:00Z', 'session signed in',
      'edit lock read failed code LOCKED error_id 0123456789abcdef at 2026-09-15T11:00:00Z',
      'request failures (1)', '  2026-09-15T10:00:00Z POST /api/drawing 403 DENIED error_id=fedcba9876543210',
      'unavailable controls (1) local', '  Save = REASONS.writeLocked',
    ].join('\n'))
  })

  it('names unavailable sample identity and empty lists', () => {
    const block = composeDiagnostics({ ...live, mode: 'sample data', servedSourceSha: null, taskRevision: null, editLock: null, failures: [], refusals: [] })
    expect(block).toContain('served not available (sample data)\ntask not available')
    expect(block).toContain('edit lock not applicable\nrequest failures (0)\n  none\nunavailable controls (0) local\n  none')
  })

  it('names unavailable live identity without blaming sample data', () => {
    const block = composeDiagnostics({ ...live, mode: 'live', servedSourceSha: null, taskRevision: null })
    expect(block).toContain('served not available\ntask not available')
    expect(block).not.toContain('(sample data)')
  })

  it('redacts credentials before truncation in identity and record fields', () => {
    const jwt = `${'a'.repeat(16)}.${'b'.repeat(16)}.${'c'.repeat(16)}`
    const block = composeDiagnostics({ ...live, buildHash: jwt, editLock: { state: jwt }, failures: [{ errorId: jwt }], refusals: [{ label: jwt, code: 'Bearer secret' }] })
    expect(block).not.toContain(jwt)
    expect(block).not.toContain('secret')
    expect(block).toContain('build [unexpected]')
    expect(block).toContain('error_id=[unexpected]')
    expect(block).toContain('  [unexpected] = [unexpected]')
  })

  it('strips controls but rejects values outside the field shape', () => {
    expect(composeDiagnostics({ ...live, buildHash: 'a\n\r\t\u0000b\u2028c', pathname: 42 })).toContain('build [unexpected]\nmode live')
    expect(composeDiagnostics({ ...live, pathname: 42 })).toContain('page [unexpected]')
    expect(composeDiagnostics({})).toContain('build unknown')
  })

  it('bounds request records and summarizes the remainder', () => {
    const block = composeDiagnostics({ ...live, failures: Array(9).fill(live.failures[0]) })
    expect(block.match(/POST \/api\/drawing/g)).toHaveLength(8)
    expect(block).toContain('request failures (9)')
    expect(block).toContain('  ... and 1 more')
  })


  it.each([
    ['errorCode', 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxIn0.a3cVhNXzaEYKgMLtfcg0sFuYnjdDn2PBy8KWXNUZFWI'],
    ['status', '401'],
    ['status', 999],
    ['method', 'get'],
    ['endpointClass', '/api/tools/one/two'],
    ['endpointClass', '/api/tools?token=secret'],
    ['at', 'not ISO'],
  ])('rejects malformed failure %s: %s', (name, value) => {
    const record = { ...live.failures[0], [name]: value }
    const block = composeDiagnostics({ ...live, failures: [record] })
    const expected = { ...record, [name]: '[unexpected]' }
    expect(block).toContain(`  ${expected.at} ${expected.method} ${expected.endpointClass} ${expected.status} ${expected.errorCode} error_id=${expected.errorId}`)
    if (name !== 'status') expect(block).not.toContain(value)
  })

  it.each([
    ['servedSourceSha', 'tenant-acme.internal?token=opaque-demo-key', 'served'],
    ['pathname', '/workspace?token=secret', 'page'],
    ['pathname', '/workspace@tenant', 'page'],
    ['at', 'not ISO', 'time'],
    ['sessionState', 'tenant-acme', 'session'],
  ])('rejects malformed %s', (name, value, prefix) => {
    const block = composeDiagnostics({ ...live, [name]: value })
    expect(block).toContain(`${prefix} [unexpected]`)
    expect(block).not.toContain(value)
  })

  it('rejects a private hostname and query in a collected refusal label', () => {
    const label = 'tenant-acme.internal?token=opaque-demo-key'
    const root = document.createElement('div')
    root.innerHTML = `<button disabled aria-label="${label}" data-reason-code="REASONS.running">Run</button>`
    const block = composeDiagnostics({ ...live, refusals: collectRefusals(root) })
    expect(block).toContain('  [unexpected] = REASONS.running')
    expect(block).not.toContain(label)
  })

  it('accepts the CI build fallback and a well-formed refusal', () => {
    const block = composeDiagnostics({ ...live, buildHash: '2026-09-15 12:00', refusals: [{ label: 'Save version', code: 'REASONS.unsavedEngineEdits' }] })
    expect(block).toContain('build 2026-09-15 12:00')
    expect(block).toContain('  Save version = REASONS.unsavedEngineEdits')
  })

  it('extracts task revisions only from slash-delimited strings', () => {
    expect(taskRevisionOf('arn:aws:ecs:region:account:task-definition/leaf-platform-app:838')).toBe('leaf-platform-app:838')
    expect(taskRevisionOf('bare')).toBeNull()
    expect(taskRevisionOf(null)).toBeNull()
  })

  it('collects coded controls in document order with plain labels and deduplication', () => {
    const root = document.createElement('div')
    root.innerHTML = '<button data-reason-code="A" aria-label="Save (unavailable: reason)" disabled>Save</button><button>Enabled</button><button data-reason-code="B">Run</button><button data-reason-code="A" aria-label="Save (unavailable: reason)">Duplicate</button>'
    expect(collectRefusals(root)).toEqual([{ label: 'Save', code: 'A' }, { label: 'Run', code: 'B' }])
    expect(collectRefusals(null)).toEqual([])
    expect(collectRefusals({})).toEqual([])
    root.innerHTML = '<button data-reason-code="A">Save</button>'.repeat(40) + '<button data-reason-code="B">Run</button>'
    expect(collectRefusals(root)).toEqual([{ label: 'Save', code: 'A' }])
  })

  it('ignores extra keys at every record level', () => {
    expect(composeDiagnostics({ ...live, token: 'secret', tenantId: 'tenant', editLock: { ...live.editLock, hostname: 'private' }, failures: [{ ...live.failures[0], body: 'private' }] })).toBe(composeDiagnostics(live))
  })

  it('bounds each value and the whole block', () => {
    const block = composeDiagnostics({ ...live, buildHash: 'x'.repeat(300), refusals: Array(40).fill({ label: 'y'.repeat(300), code: 'z'.repeat(300) }) })
    expect(block.length).toBeLessThanOrEqual(6000)
    expect(block.split('\n')[1]).toBe('build [unexpected]')
    expect(composeDiagnostics({ ...live, refusals: Array(41).fill({ label: 'a', code: 'b' }) })).toContain('  ... and 1 more')
  })

  it('uses HTTP status when the edit lock has no error code', () => {
    expect(composeDiagnostics({ ...live, editLock: { state: 'read failed', status: 409 } })).toContain('edit lock read failed code HTTP 409\n')
  })
})
