import { describe, expect, it } from 'vitest'

import { cadTimingRows } from './cadTimingPresentation.js'

describe('CAD timing presentation', () => {
  it('names every contract span and keeps unavailable values honest', () => {
    const rows = cadTimingRows({
      execution_provenance: {
        cad_timing: {
          contract: 'leaf.cad-timing.v1',
          total_ms: 506715,
          provider_accounted_ms: 501200,
          spans_ms: { queue: 490000, engine: 6290, publish: 15 },
        },
      },
    })

    expect(rows).toContain('CAD total 506,715 ms')
    expect(rows).toContain('provider queue 490,000 ms')
    expect(rows).toContain('CAD engine 6,290 ms')
    expect(rows).toContain('image pull unavailable')
    expect(rows).toContain('client delivery unavailable')
    expect(rows).toHaveLength(12)
  })

  it('does not invent timing for an old result', () => {
    expect(cadTimingRows({ timing_ms: 10 })).toEqual([])
  })
})
