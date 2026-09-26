// @vitest-environment jsdom
import { describe, expect, it } from 'vitest'
import getDiagnostics, { createDiagnostics } from './diagnostics.js'

const guid = '11111111-1111-4111-8111-111111111111'
const sessionKey = 'BwcHBwcHBwcHBwcHBwcHBwcHBwcHBwcHBwcHBwcHBwcHBwc='

describe('bridge diagnostics', () => {
  it('drops the oldest entries and enforces the hard 200 entry ceiling', () => {
    for (const [capacity, expected] of [[2, 2], [200, 200], [9999, 200], [0, 0]]) {
      const diagnostics = createDiagnostics({ capacity, now: () => 0 })
      for (let count = 0; count < 205; count += 1) diagnostics.record('envelope-rejected', { count })
      const entries = diagnostics.entries()
      expect(entries).toHaveLength(expected)
      if (expected) {
        expect(entries[0].detail.count).toBe(205 - expected)
        expect(entries.at(-1).detail.count).toBe(204)
      }
    }
  })

  it('returns independent copies and clears the log', () => {
    const diagnostics = createDiagnostics()
    diagnostics.record('start', { kind: 'webview' })
    const entries = diagnostics.entries()
    entries[0].detail.kind = 'none'
    entries[0].phase = 'timeout'
    entries.push({})
    expect(diagnostics.entries()).toHaveLength(1)
    expect(diagnostics.entries()[0]).toMatchObject({ phase: 'start', detail: { kind: 'webview' } })
    diagnostics.clear()
    expect(diagnostics.entries()).toEqual([])
    expect(getDiagnostics()).toBe(getDiagnostics())
  })

  it('allows only diagnostic keys, fixed vocabulary and safe integers', () => {
    const diagnostics = createDiagnostics()
    diagnostics.record('command-outcome', {
      kind: 'command', reason: 'stale_document', status: 'stale', action: 'focus', ms: 10, count: 2,
      token: 'secret', payload: { objectId: 'panel:A1' }, sessionKey, drawingId: guid,
    })
    expect(diagnostics.entries()[0].detail).toEqual({
      kind: 'command', reason: 'stale_document', status: 'stale', action: 'focus', ms: 10, count: 2,
    })
    diagnostics.record('start', { kind: 'https://private.example', action: guid, status: sessionKey, ms: 1.5, count: Infinity })
    expect(diagnostics.entries()[1].detail).toEqual({})
    diagnostics.record(sessionKey, { reason: guid })
    expect(diagnostics.entries()).toHaveLength(2)
  })

  it('bounds detail strings without turning arbitrary text into accepted vocabulary', () => {
    const diagnostics = createDiagnostics()
    diagnostics.record('bind-result', { reason: 'a'.repeat(1000), kind: 'a'.repeat(1000) })
    diagnostics.record('command-outcome', { reason: 'failed\n' })
    expect(diagnostics.entries().map((entry) => entry.detail)).toEqual([{ reason: 'other' }, { reason: 'other' }])
    for (const entry of diagnostics.entries()) {
      for (const value of Object.values(entry.detail)) {
        if (typeof value === 'string') expect(value.length).toBeLessThanOrEqual(80)
      }
    }
  })

  it('formats a local plain-text header and ISO timestamped rows', () => {
    const diagnostics = createDiagnostics({ now: () => Date.parse('2026-09-25T12:00:00Z') })
    diagnostics.record('start', { kind: 'webview', count: 1 })
    diagnostics.record('hello-sent')
    expect(diagnostics.snapshot({ userAgent: 'Test browser/1.0', buildId: 'build-42' })).toBe([
      `Connection details  origin=${window.location.origin}  build=build-42  userAgent=Test browser/1.0`,
      '2026-09-25T12:00:00.000Z  start  kind=webview  count=1',
      '2026-09-25T12:00:00.000Z  hello-sent',
    ].join('\n'))
    expect(diagnostics.snapshot()).not.toContain('build=')
  })

  it('never includes supplied secrets, GUIDs, handles, payloads or signatures', () => {
    const diagnostics = createDiagnostics()
    const secrets = [guid, sessionKey, 'panel:private', '2F4A', 'f'.repeat(64), 'https://private.example/token']
    for (const secret of secrets) {
      diagnostics.record('bind-result', {
        kind: secret, status: secret, action: secret, reason: secret,
        sessionKey: secret, signature: secret, token: secret, objectHandles: [secret],
        objectId: secret, payload: { secret }, platformTenantId: secret,
        projectId: secret, drawingId: secret, drawingVersionId: secret,
      })
    }
    const output = JSON.stringify(diagnostics.entries()) + diagnostics.snapshot({ userAgent: sessionKey, buildId: guid })
    for (const secret of secrets) expect(output).not.toContain(secret)
  })
})
