import { describe, expect, it } from 'vitest'
import { completionText, scopeOutline, STRING_PALETTE, stringResultOverlay, totalChipText } from './stringResultOverlay.js'

// Shape from builtins/string_panels.py and test_site.py: drawing-space pts,
// stats counts, electrical worst_string_voc/pass, module voltage ceiling.
const result = { result: {
  strings: [{ id: 'S001', modules: 2, pts: [[0, 0], [2, 0]], length_ft: 1 },
    { id: 'S002', modules: 1, pts: [[4, 0]], length_ft: 0 }],
  stats: { panel_count: 3, string_count: 2, modules_stringed: 3 },
  electrical: { worst_string_voc: 974, pass: true }, module: { max_system_voltage: 1000 },
} }

describe('string result primitives', () => {
  it('projects one drawing-space line per string with stable categorical colors', () => {
    const projected = stringResultOverlay(result)
    expect(projected.refusal).toBeNull()
    expect(projected.polylines).toHaveLength(2)
    expect(projected.polylines[0].pts).toEqual([[0, 0], [2, 0]])
    expect(projected.polylines.every(p => p.strokeWidth === 2 && STRING_PALETTE.includes(p.color))).toBe(true)
    const reversed = stringResultOverlay({ strings: [...result.result.strings].reverse() })
    expect(projected.polylines[0].color).toBe(reversed.polylines[1].color)
    expect(projected.polylines[0].color).not.toBe(projected.polylines[1].color)
  })
  it('tints assigned panels and keeps unstrung panels neutral', () => {
    const panel = x => ({ closed: true, pts: [[x - 1, -1], [x + 1, -1], [x + 1, 1], [x - 1, 1]] })
    const { panelFills, polylines } = stringResultOverlay(result, { panels: [panel(0), panel(10)] })
    expect(panelFills[0]).toMatchObject({ color: polylines[0].color, fillOpacity: 0.35 })
    expect(panelFills[1]).toMatchObject({ color: '#3f3f46', fillOpacity: 0.1 })
  })
  it('returns empty for absent and malformed results without throwing', () => {
    for (const value of [null, {}, { strings: [] }, { strings: [null] }, { strings: [{ id: 'a', pts: [[NaN, 0]] }] },
      new Proxy({}, { get() { throw new Error('bad') } })]) {
      expect(stringResultOverlay(value).polylines).toEqual([])
      expect(stringResultOverlay(value).panelFills).toEqual([])
    }
  })
  it('retains preview refusal limits', () => {
    expect(stringResultOverlay(result, { metersPerUnit: 0 }).refusal).toBe('invalid_scale')
    expect(stringResultOverlay({ strings: Array(4097).fill(result.result.strings[0]) }).refusal).toBe('overlay_limit')
    expect(stringResultOverlay({ strings: [{ id: 'S', pts: Array(100001).fill([0, 0]) }] }).refusal).toBe('overlay_limit')
  })
})

it('formats complete, partial and absent total chip fields without defaults', () => {
  expect(totalChipText(result)).toBe('3 panels · 2 strings · 974 V max')
  expect(totalChipText({ stats: { panel_count: 2345 } })).toBe('2,345 panels')
  expect(totalChipText({ stats: { string_count: 0 } })).toBe('0 strings')
  expect(totalChipText({})).toBeNull()
  expect(completionText(result)).toBe('2 strings · 0 over 1000 V')
  expect(completionText({ electrical: { pass: false }, module: { max_system_voltage: 1000 } })).toBeNull()
})

it('outlines the supplied scope while running and removes the glow when done', () => {
  const request = { scope: { pts: [[0, 0], [10, 0], [10, 10]] } }
  expect(scopeOutline(request, 'running')).toMatchObject({ strokeWidth: 2, glow: 8 })
  expect(scopeOutline(request, 'done')).toMatchObject({ strokeWidth: 2, glow: 0 })
  expect(scopeOutline(request, 'complete').pts.at(-1)).toEqual([0, 0])
  expect(scopeOutline(result, 'done')).toMatchObject({ glow: 0, pts: [[0, 0], [4, 0], [4, 0], [0, 0]] })
  expect(scopeOutline(request, 'failed')).toBeNull()
  expect(scopeOutline({}, 'running')).toBeNull()
  expect(scopeOutline({ scope: { pts: [[Infinity, 0]] } }, 'running')).toBeNull()
})
