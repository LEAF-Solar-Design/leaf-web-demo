import { describe, expect, it, vi } from 'vitest'
import { evidenceFor } from './hostEvidence.js'

const digest = 'a'.repeat(64)
const block = { registry: { id: 'count', version: '1.0.0' } }
const checks = () => ({ builds: { status: 'pass' }, safety: { status: 'pass' }, contract: { status: 'pass' },
  tests: { status: 'pass', passed: 3, total: 3 }, speed: { status: 'pass', p99_us: 8 },
  corpus: { status: 'pass', difference_count: 0 } })
const row = values => ({ data: { kind: 'count', version: '1.0.0', digest, draftDigest: digest, checks: checks(), ...values } })
const provider = (checkRows, versionRows = []) => ({ rowsOf: name => name === 'block-checks' ? checkRows : versionRows })
const version = (state, number, restart_required = state === 'pending') => ({ state, version: number, restart_required })

describe('authoritative host evidence', () => {
  it('C2 uses the newest matching announcement even when it is invalid or incomplete', () => {
    for (const newest of [{ digest: 'invalid' }, { draftDigest: null }, { checks: { tests: { status: 'pass', passed: 2, total: 2 } } }, { checks: null }]) {
      const result = evidenceFor(provider([row(), row(newest), row({ kind: 'other' })]), block)
      if ('digest' in newest) expect(result.digest).toBeNull()
      if ('draftDigest' in newest) expect(result.draftDigest).toBeNull()
      if ('checks' in newest) expect(result.checks.builds).toEqual({ status: 'pending' })
      if (newest.checks) expect(result.checks.tests.passed).toBe(2)
    }
  })

  it('C4 keeps valid checks and details while absent or malformed individual checks are pending', () => {
    const result = evidenceFor(provider([row({ checks: { ...checks(), safety: undefined,
      builds: { status: 'pass', detail: 'bad detail', passed: -1 },
      corpus: { status: 'fail', difference_count: 1, differences: [{ trade: 'one', live: 3, draft: 2 }] } } })]), block)
    expect(result.checks.builds).toEqual({ status: 'pending' })
    expect(result.checks.safety).toEqual({ status: 'pending' })
    expect(result.checks.tests).toEqual(checks().tests)
    expect(result.checks.corpus.differences).toEqual([{ trade: 'one', live: 3, draft: 2 }])
  })

  it('C9 uses one latest valid version snapshot with only one live version', () => {
    const result = evidenceFor(provider([], [
      row({ versions: [version('active', '1')] }),
      row({ versions: [version('active', '2'), version('prior', '1'), version('pending', '3')] }),
      row({ versions: [version('active', '4'), version('active', '5')] }),
    ]), block)
    expect(result.versions.map(item => item.version)).toEqual(['2', '1', '3'])
    expect(result.versions.filter(item => item.state === 'active')).toHaveLength(1)
    const cleared = evidenceFor(provider([row()], [row({ versions: [version('active', '1')] }), row({ versions: [] })]), block)
    expect(cleared.versions).toEqual([])
  })

  it('C10 rejects contradictory pending and active restart states', () => {
    const result = evidenceFor(provider([], [row({ versions: [
      version('active', 'bad-active', true), version('pending', 'bad-pending', false),
      version('active', 'live', false), version('pending', 'next', true),
    ] })]), block)
    expect(result.versions).toEqual([version('active', 'live', false), version('pending', 'next', true)])
  })

  it('C11 bounds each source to 32 newest rows and rejects oversized values before serialization', () => {
    let inspected = 0
    const outside = { get data() { inspected++; throw new Error('outside window') } }
    const read = vi.fn(name => name === 'block-checks'
      ? [outside, ...Array.from({ length: 32 }, () => row())]
      : [outside, ...Array.from({ length: 32 }, () => row({ versions: [version('active', 'live')] }))])
    const result = evidenceFor({ rowsOf: read }, block)
    expect(inspected).toBe(0)
    expect(read.mock.calls).toEqual([['block-checks'], ['block-versions']])
    expect(result.versions).toEqual([version('active', 'live')])
    const stringify = vi.spyOn(JSON, 'stringify')
    try {
      for (const differences of [['x'.repeat(2049)], ['é'.repeat(1025)], Array(65).fill(0), Array(65),
        [{ a: { b: { c: { d: { e: 1 } } } } }], [Object.fromEntries(Array.from({ length: 65 }, (_, i) => [i, 0]))]]) {
        stringify.mockClear()
        const bounded = evidenceFor(provider([row({ checks: { ...checks(), corpus: { status: 'fail', differences } } })]), block)
        expect(bounded.checks.corpus).toEqual({ status: 'pending' })
        expect(bounded.checks.tests.passed).toBe(3)
        expect(stringify).not.toHaveBeenCalled()
      }
    } finally { stringify.mockRestore() }
  })
})
