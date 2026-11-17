import { describe, expect, it } from 'vitest'
import { summarizeTourResult } from './tourResult.js'
import { TOUR_STEPS } from './tourScript.js'

const step = (id) => TOUR_STEPS.find((s) => s.id === id)
const env = (tool, result) => ({ ok: true, tool, result })

describe('summarizeTourResult', () => {
  it('reads the per-layer counts and the total', () => {
    expect(summarizeTourResult(step('count'), env('count-by-layer', { counts: { Panels: 2345 }, total: 2345 })))
      .toEqual([{ label: 'Layer Panels', value: '2,345' }, { label: 'Total entities', value: '2,345' }])
  })

  it('reads the setback count against the panel total', () => {
    expect(summarizeTourResult(step('edge'), env('highlight-panels-near-edge', { distance_in: 60, panels_near_edge: 412, panels_total: 2345 })))
      .toEqual([{ label: 'Panels within 60 in of the edge', value: '412 of 2,345' }])
  })

  it('reads the total area, panel count and the marked largest module', () => {
    expect(summarizeTourResult(step('measure'), env('measure-panel-area', { panels: 2345, total_area_sqft: 48712.3, max_area_sqft: 21.4 })))
      .toEqual([
        { label: 'Total panel area', value: '48,712.3 sq ft' },
        { label: 'Panels measured', value: '2,345' },
        { label: 'Largest module (marked)', value: '21.4 sq ft' },
      ])
  })

  it('reads the deleted panel and the before and after counts', () => {
    expect(summarizeTourResult(step('version'), env('delete-marked-panel', { removed: '7FA3', panels_before: 2345, panels_after: 2344 })))
      .toEqual([{ label: 'Deleted panel', value: '7FA3' }, { label: 'Panels', value: '2,345 before, 2,344 after' }])
  })

  it('refuses a stale envelope from another beat, a failed run, and malformed input', () => {
    expect(summarizeTourResult(step('measure'), env('count-by-layer', { total: 5 }))).toBe(null)
    expect(summarizeTourResult(step('count'), { ok: false, tool: 'count-by-layer', error: 'x' })).toBe(null)
    expect(summarizeTourResult(step('count'), null)).toBe(null)
    expect(summarizeTourResult(step('welcome'), env('count-by-layer', { total: 5 }))).toBe(null)
    expect(summarizeTourResult(step('count'), env('count-by-layer', { counts: { Panels: 'many' }, total: Number.NaN }))).toBe(null)
  })

  it('caps a long layer list', () => {
    const counts = Object.fromEntries(Array.from({ length: 7 }, (_, i) => [`L${i}`, 7 - i]))
    const rows = summarizeTourResult(step('count'), env('count-by-layer', { counts, total: 28 }))
    expect(rows.map((r) => r.label)).toEqual(['Layer L0', 'Layer L1', 'Layer L2', 'Layer L3', 'Other layers', 'Total entities'])
  })
})
