// The build queue record (slice 11a): every source's mapping against the
// SHARED cases the server mirror is pinned to, the two-stage terminal rules,
// and the fail-closed validator on malformed input.
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { describe, expect, it } from 'vitest'

import {
  BUILD_ACTIONS,
  BUILD_LANES,
  BUILD_LIMITS,
  BUILD_STATES,
  RECEIPT_KINDS,
  brokerStateTag,
  formatCostUsd,
  fromBrokerJob,
  fromFleetTask,
  fromFoldState,
  isTerminalBuild,
  parseBuildRecord,
  parseBuildRecords,
  parseReceipt,
  promotionReceipt,
  runningBuildCount,
  toEpochMs,
  validateBuildRecord,
} from './buildQueue.js'

const CASES_PATH = join(dirname(fileURLToPath(import.meta.url)), '..', '..', '..', 'contract', 'build-queue.v1.cases.json')
const CASES = JSON.parse(readFileSync(CASES_PATH, 'utf8'))

const MAPPERS = {
  broker: (input) => fromBrokerJob(input),
  fold: (input) => fromFoldState(input.state, input.meta),
  fleet: (input) => fromFleetTask(input),
}

// A frozen record -> plain JSON, so toEqual compares values, not freezing.
const plain = (record) => JSON.parse(JSON.stringify(record))

describe('the shared mapping cases (contract/build-queue.v1.cases.json)', () => {
  it('covers every lane with at least one refusal each', () => {
    for (const lane of BUILD_LANES) {
      const mine = CASES.cases.filter((c) => c.source === lane)
      expect(mine.length, lane).toBeGreaterThanOrEqual(3)
      expect(mine.some((c) => c.expected === null), `${lane} has a refusal case`).toBe(true)
    }
  })

  for (const testCase of CASES.cases) {
    it(testCase.name, () => {
      const record = MAPPERS[testCase.source](testCase.input)
      if (testCase.expected === null) {
        expect(record).toBeNull()
        return
      }
      expect(plain(record)).toEqual(testCase.expected)
      // Every mapped record passes the wire validator by construction.
      const parsed = parseBuildRecord(plain(record))
      expect(parsed.ok, parsed.reason).toBe(true)
      expect(plain(parsed.record)).toEqual(testCase.expected)
    })
  }

  it('every expected record is frozen deep enough that a host cannot mutate it', () => {
    const record = fromBrokerJob({ job_id: 'j', tool: 't', status: 'complete', cost: { usd_est: 0.5 } })
    expect(Object.isFrozen(record)).toBe(true)
    expect(Object.isFrozen(record.terminal)).toBe(true)
    expect(Object.isFrozen(record.receipts)).toBe(true)
    expect(Object.isFrozen(record.actions)).toBe(true)
    expect(Object.isFrozen(record.status)).toBe(true)
  })
})

describe('the two-stage terminal, never inferred', () => {
  it('a complete broker job is verified by its own completion and never promoted on its own', () => {
    const r = fromBrokerJob({ job_id: 'j', tool: 't', status: 'complete', elapsed_ms: 1 })
    expect(r.terminal).toEqual({ verified: true, promoted: false })
  })

  it('a failed broker job is neither', () => {
    const r = fromBrokerJob({ job_id: 'j', tool: 't', status: 'failed', error: { message: 'x' } })
    expect(r.terminal).toEqual({ verified: false, promoted: false })
  })

  it('the wire refuses promoted:true without a promotion receipt', () => {
    const base = plain(fromBrokerJob({ job_id: 'j', tool: 't', status: 'complete' }))
    const parsed = parseBuildRecord({ ...base, terminal: { verified: true, promoted: true } })
    expect(parsed.ok).toBe(false)
    expect(parsed.reason).toMatch(/promoted: true without a promotion receipt/)
  })

  it('a fold run is verified only by a milestone verified_at under a real oracle', () => {
    const state = { run_id: 'r', rounds: 2, mission_complete: true, milestones: { a: { status: 'done' } } }
    expect(fromFoldState(state, {}).terminal.verified).toBe(false)
    expect(fromFoldState({ ...state, milestones: { a: { status: 'done', verified_at: '2026-09-04T00:00:00Z' } } }, {}).terminal.verified).toBe(true)
    expect(fromFoldState({ ...state, mission_complete_vacuous: true, milestones: { a: { status: 'done', verified_at: '2026-09-04T00:00:00Z' } } }, {}).terminal.verified).toBe(false)
  })

  it("a fleet task's completion is transcript evidence, so only a verification or gate-proof receipt verifies it", () => {
    const row = { task_id: 't', title: 'x', state: 'complete' }
    expect(fromFleetTask(row).terminal.verified).toBe(false)
    expect(fromFleetTask({ ...row, receipts: [{ kind: 'artifact', ref: 'a' }] }).terminal.verified).toBe(false)
    expect(fromFleetTask({ ...row, receipts: [{ kind: 'verification', ref: 'v' }] }).terminal.verified).toBe(true)
    expect(fromFleetTask({ ...row, receipts: [{ kind: 'gate-proof', ref: 'g' }] }).terminal.verified).toBe(true)
  })

  it('a promotion artifact is recognised by its schema and its dispatch, not by its key', () => {
    expect(promotionReceipt({ prewarm_relay: { schema: 'leaf.staging-prewarm-relay.v1', relay_run_id: 1, dispatched: [{}] } })).toMatchObject({ kind: 'promotion', ref: 'leaf.staging-prewarm-relay.v1#1' })
    expect(promotionReceipt({ prewarm_relay: { schema: 'leaf.staging-prewarm-relay.v0', relay_run_id: 1, dispatched: [{}] } })).toBeNull()
    expect(promotionReceipt({ prewarm_relay: { schema: 'leaf.staging-prewarm-relay.v1', relay_run_id: 1, dispatched: [] } })).toBeNull()
    expect(promotionReceipt({ app_store_connect_result: { status: 'failed', build_id: '1' } })).toBeNull()
    expect(promotionReceipt({ app_store_connect_result: { status: 'succeeded' } })).toBeNull()
    expect(promotionReceipt({ app_store_connect_result: { status: 'succeeded', build_id: '7' } })).toMatchObject({ ref: 'app_store_connect#7' })
    expect(promotionReceipt({ promotion_stage: { status: 'pending', ref: 'x' } })).toBeNull()
    expect(promotionReceipt({ promotion_stage: { status: 'promoted', ref: 'x' } })).toMatchObject({ ref: 'x' })
    expect(promotionReceipt({ promotion: true })).toBeNull()
    expect(promotionReceipt(null)).toBeNull()
  })

  it('promote is offered only to a verified, unpromoted terminal record of a lane that can promote', () => {
    expect(fromBrokerJob({ job_id: 'j', tool: 't', status: 'complete' }).actions).toEqual([])
    const fold = fromFoldState({ run_id: 'r', rounds: 1, mission_complete: true, milestones: { a: { status: 'done', verified_at: '2026-09-04T00:00:00Z' } } }, {})
    expect(fold.actions).toEqual(['promote'])
    expect(fromFleetTask({ task_id: 't', title: 'x', state: 'complete', receipts: [{ kind: 'gate-proof', ref: 'g' }] }).actions).toEqual(['promote'])
    expect(fromFleetTask({ task_id: 't', title: 'x', state: 'complete' }).actions).toEqual([])
  })
})

describe("JobRail's vocabulary, preserved verbatim", () => {
  it('stateTag: running / submitted / spend cap / plan / failed / degraded / complete / pending', () => {
    expect(brokerStateTag({ status: 'running' })).toEqual({ tint: 'ok', label: 'running' })
    expect(brokerStateTag({ status: 'submitted' })).toEqual({ tint: 'mut', label: 'submitted' })
    expect(brokerStateTag({ status: 'failed', error: { error_code: 'quota_exceeded' } })).toEqual({ tint: 'warn', label: 'spend cap' })
    expect(brokerStateTag({ status: 'failed', error_code: 'quota_exceeded' })).toEqual({ tint: 'warn', label: 'spend cap' })
    expect(brokerStateTag({ status: 'failed', entitlement_required: true })).toEqual({ tint: 'warn', label: 'plan' })
    expect(brokerStateTag({ status: 'failed' })).toEqual({ tint: 'err', label: 'failed' })
    expect(brokerStateTag({ status: 'complete', degraded_mode: true })).toEqual({ tint: 'warn', label: 'degraded' })
    expect(brokerStateTag({ status: 'complete' })).toEqual({ tint: 'ok', label: 'complete' })
    expect(brokerStateTag({ status: 'weird' })).toEqual({ tint: 'mut', label: 'weird' })
    expect(brokerStateTag({})).toEqual({ tint: 'mut', label: 'pending' })
  })

  it('costUsd: four decimals under a cent, two above, nothing at zero or garbage', () => {
    expect(formatCostUsd(0.0042)).toBe('$0.0042')
    expect(formatCostUsd(0.0123)).toBe('$0.01')
    expect(formatCostUsd(0.01)).toBe('$0.01')
    expect(formatCostUsd(1.5)).toBe('$1.50')
    expect(formatCostUsd(0)).toBeNull()
    expect(formatCostUsd(-1)).toBeNull()
    expect(formatCostUsd('abc')).toBeNull()
    expect(formatCostUsd(Infinity)).toBeNull()
  })

  it('a job whose cost sits in its stored envelope still reports it', () => {
    expect(fromBrokerJob({ job_id: 'j', tool: 't', status: 'complete', result: { cost: { usd_est: 0.02 } } }).status.detail).toBe('$0.02')
  })
})

describe('parseBuildRecord fails closed on malformed input', () => {
  const good = () => plain(fromBrokerJob({ job_id: 'j-1', tool: 'count-by-layer', status: 'complete', created_at: 1725400000, elapsed_ms: 10, cost: { usd_est: 0.5 } }))

  const refuse = (mutate, pattern) => {
    const input = good()
    mutate(input)
    const parsed = parseBuildRecord(input)
    expect(parsed.ok, JSON.stringify(input)).toBe(false)
    expect(parsed.reason).toMatch(pattern)
  }

  it('accepts its own good record', () => {
    expect(parseBuildRecord(good()).ok).toBe(true)
  })

  it('non-objects', () => {
    for (const bad of [null, undefined, 1, 'x', [], true]) {
      expect(parseBuildRecord(bad).ok).toBe(false)
    }
  })

  it('id: missing, empty, over bound, wrong type', () => {
    refuse((r) => { delete r.id }, /^id:/)
    refuse((r) => { r.id = '' }, /^id:/)
    refuse((r) => { r.id = 'x'.repeat(BUILD_LIMITS.id + 1) }, /^id:/)
    refuse((r) => { r.id = 12 }, /^id:/)
  })

  it('lane and state are closed enums', () => {
    refuse((r) => { r.lane = 'ci' }, /^lane:/)
    refuse((r) => { r.state = 'complete' }, /^state:/)
    for (const lane of BUILD_LANES) expect(parseBuildRecord({ ...good(), lane }).ok).toBe(true)
    for (const state of BUILD_STATES) {
      expect(parseBuildRecord({ ...good(), state, terminal: { verified: false, promoted: false } }).ok).toBe(true)
    }
  })

  it('title and requested_by are bounded text', () => {
    refuse((r) => { r.title = '' }, /^title:/)
    refuse((r) => { r.title = 'x'.repeat(BUILD_LIMITS.text + 1) }, /^title:/)
    refuse((r) => { r.requested_by = '' }, /^requested_by:/)
    refuse((r) => { r.requested_by = 7 }, /^requested_by:/)
    expect(parseBuildRecord({ ...good(), requested_by: 'evan' }).record.requested_by).toBe('evan')
  })

  it('timestamps and numbers: finite, non-negative, integral where declared', () => {
    refuse((r) => { r.started = 'yesterday' }, /^started:/)
    refuse((r) => { r.started = -1 }, /^started:/)
    refuse((r) => { r.elapsed_ms = -1 }, /^elapsed_ms:/)
    refuse((r) => { r.elapsed_ms = 'fast' }, /^elapsed_ms:/)
    refuse((r) => { r.elapsed_ms = Number.NaN }, /^elapsed_ms:/)
    refuse((r) => { r.estimate_ms = 0 }, /^estimate_ms:/)
    refuse((r) => { r.cost_usd = -0.5 }, /^cost_usd:/)
    refuse((r) => { r.cost_usd = Number.POSITIVE_INFINITY }, /^cost_usd:/)
    refuse((r) => { r.cost_usd = true }, /^cost_usd:/)
    expect(parseBuildRecord({ ...good(), started: '2026-09-04T00:00:00Z' }).record.started).toBe(1788480000000)
    expect(parseBuildRecord({ ...good(), started: 1725400000 }).record.started).toBe(1725400000000)
  })

  it('receipts: an array of well-formed receipts, bounded', () => {
    refuse((r) => { r.receipts = null }, /^receipts:/)
    refuse((r) => { r.receipts = [{ kind: 'terminal', ref: '' }] }, /^receipts\[0\]/)
    refuse((r) => { r.receipts = [{ kind: 'nope', ref: 'x' }] }, /^receipts\[0\]/)
    refuse((r) => { r.receipts = [{ kind: 'terminal', ref: 'x', at: 'never' }] }, /^receipts\[0\]/)
    refuse((r) => { r.receipts = [{ kind: 'terminal', ref: 'x'.repeat(BUILD_LIMITS.ref + 1) }] }, /^receipts\[0\]/)
    refuse((r) => { r.receipts = Array.from({ length: BUILD_LIMITS.receipts + 1 }, () => ({ kind: 'artifact', ref: 'x' })) }, /^receipts: more than/)
    const ok = parseBuildRecord({ ...good(), receipts: [{ kind: 'artifact', ref: 'x' }] })
    expect(ok.ok).toBe(true)
    expect(ok.record.receipts).toEqual([{ kind: 'artifact', ref: 'x', at: null }])
  })

  it('terminal: two booleans, nothing else', () => {
    refuse((r) => { r.terminal = null }, /^terminal:/)
    refuse((r) => { r.terminal = { verified: 'yes', promoted: false } }, /^terminal\.verified:/)
    refuse((r) => { r.terminal = { verified: true, promoted: 0 } }, /^terminal\.promoted:/)
  })

  it('actions: declared verbs only, no duplicates, no more than the vocabulary', () => {
    refuse((r) => { r.actions = 'cancel' }, /^actions:/)
    refuse((r) => { r.actions = ['restart'] }, /^actions:/)
    refuse((r) => { r.actions = ['retry', 'retry'] }, /^actions:/)
    refuse((r) => { r.actions = [...BUILD_ACTIONS, 'cancel'] }, /^actions:/)
    expect(parseBuildRecord({ ...good(), actions: [...BUILD_ACTIONS] }).ok).toBe(true)
  })

  it('status: a bounded word, a closed tint, an optional bounded detail', () => {
    refuse((r) => { r.status = null }, /^status:/)
    refuse((r) => { r.status = { word: '', tint: 'ok', detail: null } }, /^status\.word:/)
    refuse((r) => { r.status = { word: 'ok', tint: 'green', detail: null } }, /^status\.tint:/)
    refuse((r) => { r.status = { word: 'ok', tint: 'ok', detail: '' } }, /^status\.detail:/)
    refuse((r) => { r.status = { word: 'ok', tint: 'ok', detail: 'x'.repeat(BUILD_LIMITS.text + 1) } }, /^status\.detail:/)
  })

  it('never throws on hostile shapes', () => {
    const hostile = [
      { id: 'a', lane: 'broker', state: 'done', title: 't', receipts: 'x', terminal: 1, actions: 1, status: 1 },
      { id: { toString: () => 'x' }, lane: ['broker'], state: {}, title: () => 't' },
      Object.create(null),
      new Proxy({}, { get: () => { throw new Error('boom') } }),
    ]
    for (const bad of hostile.slice(0, 3)) expect(parseBuildRecord(bad).ok).toBe(false)
    // A throwing proxy is the one input that CAN throw: it is not data, and
    // the validator does not catch its own property reads on purpose (a
    // swallowed exception there would hide a bug in the caller).
    expect(() => parseBuildRecord(hostile[3])).toThrow(/boom/)
  })

  it('validateBuildRecord throws the same reason', () => {
    expect(() => validateBuildRecord({ ...good(), lane: 'ci' })).toThrow(/lane: not one of/)
    expect(validateBuildRecord(good()).id).toBe('j-1')
  })

  it('parseBuildRecords keeps the good rows, counts the bad ones, and caps the list', () => {
    const list = [good(), { nope: true }, { ...good(), id: 'j-2' }, 'x']
    const { records, dropped } = parseBuildRecords(list)
    expect(records.map((r) => r.id)).toEqual(['j-1', 'j-2'])
    expect(dropped).toEqual([{ index: 1, reason: 'id: missing or over bound' }, { index: 3, reason: 'record: not an object' }])
    expect(parseBuildRecords(null).dropped[0].reason).toMatch(/not an array/)
    const many = Array.from({ length: BUILD_LIMITS.records + 5 }, (_, i) => ({ ...good(), id: `j-${i}` }))
    expect(parseBuildRecords(many).records).toHaveLength(BUILD_LIMITS.records)
  })
})

describe('the mappers bound what they carry', () => {
  it('clips a long title, detail and requester with an ellipsis instead of dropping the row', () => {
    const long = 'x'.repeat(BUILD_LIMITS.text + 50)
    const r = fromFleetTask({ task_id: 't', title: long, state: 'active', detail: long, owner: long })
    expect(r.title).toHaveLength(BUILD_LIMITS.text)
    expect(r.title.endsWith('…')).toBe(true)
    expect(r.status.detail).toHaveLength(BUILD_LIMITS.text)
    expect(r.requested_by).toHaveLength(BUILD_LIMITS.text)
    expect(parseBuildRecord(plain(r)).ok).toBe(true)
  })

  it('refuses an over-bound id rather than truncating an identifier', () => {
    expect(fromFleetTask({ task_id: 'x'.repeat(BUILD_LIMITS.id + 1), title: 't', state: 'active' })).toBeNull()
    expect(fromBrokerJob({ job_id: 'x'.repeat(BUILD_LIMITS.id + 1), tool: 't', status: 'running' })).toBeNull()
  })

  it('caps receipts at the bound', () => {
    const many = Array.from({ length: BUILD_LIMITS.receipts + 10 }, (_, i) => ({ kind: 'artifact', ref: `a${i}` }))
    const r = fromFleetTask({ task_id: 't', title: 't', state: 'active', receipts: many })
    expect(r.receipts).toHaveLength(BUILD_LIMITS.receipts)
  })

  it('a milestone table that is not an object counts as no milestones', () => {
    const r = fromFoldState({ run_id: 'r', rounds: 1, milestones: ['a', 'b'] }, {})
    expect(r.status.detail).toBeNull()
  })

  it('refuses non-object state or meta', () => {
    expect(fromFoldState(null, {})).toBeNull()
    expect(fromFoldState({ run_id: 'r' }, null)).toBeNull()
    expect(fromFleetTask('x')).toBeNull()
    expect(fromBrokerJob(undefined)).toBeNull()
  })
})

describe('small helpers', () => {
  it('toEpochMs: seconds, milliseconds, ISO, and nothing else', () => {
    expect(toEpochMs(1725400000)).toBe(1725400000000)
    expect(toEpochMs(1725400000000)).toBe(1725400000000)
    expect(toEpochMs('2026-09-04T00:00:00Z')).toBe(1788480000000)
    expect(toEpochMs(0)).toBeNull()
    expect(toEpochMs(-5)).toBeNull()
    expect(toEpochMs('soon')).toBeNull()
    expect(toEpochMs('x'.repeat(65))).toBeNull()
    expect(toEpochMs({})).toBeNull()
    expect(toEpochMs(true)).toBeNull()
  })

  it('parseReceipt: kind in the vocabulary, bounded ref, optional timestamp', () => {
    expect(RECEIPT_KINDS).toContain('promotion')
    expect(parseReceipt({ kind: 'terminal', ref: 'r' })).toEqual({ kind: 'terminal', ref: 'r', at: null })
    expect(parseReceipt({ kind: 'terminal', ref: 'r', at: 1725400000 })).toEqual({ kind: 'terminal', ref: 'r', at: 1725400000000 })
    expect(parseReceipt({ kind: 'terminal', ref: 'r', at: 'bad' })).toBeNull()
    expect(parseReceipt({ kind: 'terminal' })).toBeNull()
    expect(parseReceipt(null)).toBeNull()
  })

  it('runningBuildCount counts queued, running and verifying only', () => {
    const rows = ['queued', 'running', 'verifying', 'done', 'failed'].map((state) => ({ state }))
    expect(runningBuildCount(rows)).toBe(3)
    expect(runningBuildCount([])).toBe(0)
    expect(runningBuildCount(null)).toBe(0)
    expect(runningBuildCount([null, { state: 'running' }])).toBe(1)
  })

  it('isTerminalBuild: done and failed', () => {
    expect(isTerminalBuild({ state: 'done' })).toBe(true)
    expect(isTerminalBuild({ state: 'failed' })).toBe(true)
    expect(isTerminalBuild({ state: 'verifying' })).toBe(false)
    expect(isTerminalBuild(null)).toBe(false)
  })
})
