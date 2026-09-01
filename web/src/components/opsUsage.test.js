import { describe, expect, it } from 'vitest'

import { formatUsageCount, formatUsageUsd, normalizeOpsUsage } from './opsUsage.js'

describe('ops usage normalisation', () => {
  it('reads the joined usage body at both scopes', () => {
    const snapshot = normalizeOpsUsage({
      tenants: [{
        tenant_id: 'profile-a',
        runs: 4, usd_est: 0.2,
        llm_turns: 2, llm_cost_tokens: 1250, llm_usd_est: 0.03,
      }],
      platform: {
        profiles: 3,
        autocad_backend: { runs: 9, usd_est: 0.5 },
        llm: { turns: 7, cost_tokens: 4200, usd_est: 0.1 },
      },
    })

    expect(snapshot.tenants[0].llm_cost_tokens).toBe(1250)
    expect(snapshot.platform.profiles).toBe(3)
    expect(snapshot.platform.autocad_backend.runs).toBe(9)
    expect(snapshot.platform.llm.cost_tokens).toBe(4200)
  })

  it('prefers the server platform block over the rows it can see', () => {
    // The server counts tenants that spent LLM turns without ever reaching the
    // broker, so its totals legitimately exceed the sum of the listed rows.
    // Recomputing from the rows here would silently under-report the platform.
    const snapshot = normalizeOpsUsage({
      tenants: [{ tenant_id: 'a', runs: 1, usd_est: 0.01, llm_cost_tokens: 10 }],
      platform: {
        profiles: 5,
        autocad_backend: { runs: 1, usd_est: 0.01 },
        llm: { turns: 40, cost_tokens: 9000, usd_est: 0.9 },
      },
    })

    expect(snapshot.platform.profiles).toBe(5)
    expect(snapshot.platform.llm.cost_tokens).toBe(9000)
  })

  it('falls back to row totals when an older backend omits the platform block', () => {
    const snapshot = normalizeOpsUsage({
      tenants: [
        { tenant_id: 'a', runs: 2, usd_est: 0.1, llm_cost_tokens: 300 },
        { tenant_id: 'b', runs: 3, usd_est: 0.2, llm_cost_tokens: 500 },
      ],
    })

    expect(snapshot.platform.profiles).toBe(2)
    expect(snapshot.platform.autocad_backend.runs).toBe(5)
    expect(snapshot.platform.llm.cost_tokens).toBe(800)
  })

  it('keeps an unreadable ledger UNKNOWN instead of reporting a confident zero', () => {
    // This is the defect the derivation source carried: `Number(x) || 0` turned
    // "we could not read the ledger" into "this profile spent nothing".
    const snapshot = normalizeOpsUsage({
      tenants: [{
        tenant_id: 'a', runs: 2, usd_est: 0.1,
        llm_turns: null, llm_cost_tokens: null, llm_usd_est: null,
      }],
    })

    expect(snapshot.tenants[0].llm_cost_tokens).toBeNull()
    expect(snapshot.platform.llm.cost_tokens).toBeNull()
    expect(snapshot.platform.autocad_backend.runs).toBe(2)
  })

  it('refuses a partial total: one unknown contributor makes the sum unknown', () => {
    const snapshot = normalizeOpsUsage({
      tenants: [
        { tenant_id: 'a', runs: 2, usd_est: 0.1, llm_cost_tokens: 300 },
        { tenant_id: 'b', runs: 3, usd_est: 0.2 },
      ],
    })

    expect(snapshot.platform.llm.cost_tokens).toBeNull()
    expect(snapshot.platform.autocad_backend.runs).toBe(5)
  })

  it('survives a body that is empty, malformed, or not an object', () => {
    for (const body of [undefined, null, {}, { tenants: 'nope' }, 7]) {
      const snapshot = normalizeOpsUsage(body)
      expect(snapshot.tenants).toEqual([])
      expect(snapshot.platform.profiles).toBe(0)
    }
  })
})

describe('ops usage formatting', () => {
  it('renders known readings compactly', () => {
    expect(formatUsageCount(1250)).toBe('1.3K')
    expect(formatUsageCount(0)).toBe('0')
    expect(formatUsageUsd(0.125)).toBe('$0.125')
    expect(formatUsageUsd(0)).toBe('$0.000')
  })

  it('renders an unknown reading as an em dash, never as zero', () => {
    expect(formatUsageCount(null)).toBe('—')
    expect(formatUsageCount(undefined)).toBe('—')
    expect(formatUsageUsd(null)).toBe('—')
    expect(formatUsageUsd(undefined)).toBe('—')
  })
})
