import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import {
  DETAIL_MAX_CHARS,
  contextPct,
  finiteOrNull,
  fmtDetail,
  orDash,
  usageCost,
  usageModel,
} from './usage.js'

describe('finiteOrNull — the no-fabrication guard', () => {
  it('treats every flavour of absent as absent, NOT as zero', () => {
    // Number(null), Number(''), Number([]) and Number(false) are all 0. A
    // naive Number.isFinite check therefore reports a confident 0 for a value
    // the backend never sent, which is the bug this guard exists to stop.
    for (const absent of [null, undefined, '', '   ', [], false, true, {}, NaN, 'abc', Infinity]) {
      assert.equal(finiteOrNull(absent), null, `expected ${JSON.stringify(absent)} to be absent`)
    }
  })

  it('keeps real numerics, including zero and numeric strings', () => {
    assert.equal(finiteOrNull(0), 0)
    assert.equal(finiteOrNull(12.5), 12.5)
    assert.equal(finiteOrNull('42'), 42)
    assert.equal(finiteOrNull(-3), -3)
  })
})

describe('contextPct', () => {
  it('is unknown until BOTH contract fields arrive', () => {
    assert.equal(contextPct(undefined), null)
    assert.equal(contextPct({}), null)
    assert.equal(contextPct({ context_used_tokens: 100 }), null)
    assert.equal(contextPct({ context_window_tokens: 1000 }), null)
    // The exact fabrication case: nulls must not read as 0%.
    assert.equal(contextPct({ context_used_tokens: null, context_window_tokens: null }), null)
  })

  it('never divides by zero', () => {
    assert.equal(contextPct({ context_used_tokens: 10, context_window_tokens: 0 }), null)
    assert.equal(contextPct({ context_used_tokens: 10, context_window_tokens: -5 }), null)
  })

  it('rounds and clamps to 0..100', () => {
    assert.equal(contextPct({ context_used_tokens: 250, context_window_tokens: 1000 }), 25)
    assert.equal(contextPct({ context_used_tokens: 0, context_window_tokens: 1000 }), 0)
    assert.equal(contextPct({ context_used_tokens: 5000, context_window_tokens: 1000 }), 100)
  })
})

describe('usageModel — reads the real contract field', () => {
  it('joins the models ARRAY (ConverseTurnUsage.models)', () => {
    assert.equal(usageModel({ models: ['claude-fable-5'] }), 'claude-fable-5')
    assert.equal(usageModel({ models: ['a', 'b'] }), 'a · b')
  })

  it('is unknown for an absent or empty models list', () => {
    assert.equal(usageModel(undefined), null)
    assert.equal(usageModel({}), null)
    assert.equal(usageModel({ models: [] }), null)
    assert.equal(usageModel({ models: ['', '  '] }), null)
  })

  it('tolerates a bare model string without inventing one', () => {
    assert.equal(usageModel({ model: 'sonnet' }), 'sonnet')
    assert.equal(usageModel({ model: '' }), null)
    assert.equal(usageModel({ model: 42 }), null)
  })
})

describe('usageCost — reads total_cost_usd, not an invented name', () => {
  it('returns the contract field', () => {
    assert.equal(usageCost({ total_cost_usd: 0.125 }), 0.125)
    assert.equal(usageCost({ total_cost_usd: 0 }), 0)
  })

  it('is unknown when absent, and ignores the wrong field name', () => {
    assert.equal(usageCost({}), null)
    assert.equal(usageCost({ total_cost_usd: null }), null)
    // session_cost_usd is NOT the contract; reading it would have silently
    // shown "—" for turns whose real cost was known.
    assert.equal(usageCost({ session_cost_usd: 9.99 }), null)
  })
})

describe('orDash', () => {
  it('renders the honest em dash for absent readings', () => {
    assert.equal(orDash(null), '—')
    assert.equal(orDash(undefined), '—')
  })

  it('formats known readings, including zero', () => {
    assert.equal(orDash(0, (p) => `${p}%`), '0%')
    assert.equal(orDash(25, (p) => `${p}%`), '25%')
    assert.equal(orDash(0.5, (c) => `~$${c.toFixed(3)}`), '~$0.500')
  })
})

describe('fmtDetail', () => {
  it('passes strings through and pretty-prints objects', () => {
    assert.equal(fmtDetail('hello'), 'hello')
    assert.equal(fmtDetail({ a: 1 }), '{\n  "a": 1\n}')
  })

  it('bounds oversized payloads so the transcript stays scrollable', () => {
    const big = 'x'.repeat(DETAIL_MAX_CHARS + 500)
    const out = fmtDetail(big)
    assert.ok(out.startsWith('x'.repeat(DETAIL_MAX_CHARS)))
    assert.match(out, /… 500 more characters$/)
  })

  it('survives circular and non-serialisable values', () => {
    const circular = { name: 'loop' }
    circular.self = circular
    assert.equal(typeof fmtDetail(circular), 'string')
    assert.equal(typeof fmtDetail(undefined), 'string')
    assert.equal(typeof fmtDetail(() => {}), 'string')
  })
})
