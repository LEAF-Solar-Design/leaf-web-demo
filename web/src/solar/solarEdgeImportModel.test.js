import { describe, expect, it } from 'vitest'
import { acceptParams, parseTolerance, reportSummary } from './solarEdgeImportModel.js'

const artifactId = 'b'.repeat(64)
const report = { drawing_id: 'd1', report: { artifact_id: artifactId } }

describe('SolarEdge import model', () => {
  it.each(['', '0', '1e-7', '1000001', 'abc', '0x1', ' 1', 'NaN', '1 ', '1\n', 'Infinity', '1_000', '1e999'])
  ('SE8 rejects invalid tolerance %j', (text) => {
    expect(parseTolerance(text).ok).toBe(false)
    expect(parseTolerance(text).reason).toEqual(expect.any(String))
  })

  it.each([['1e-6', 0.000001], ['1000000', 1000000], ['0.5', 0.5]])
  ('SE9 parses decimal tolerance %s', (text, value) => {
    expect(parseTolerance(text)).toEqual({ ok: true, value })
  })

  it('SE10 closes accept params at the two declared keys and includes revision zero', () => {
    expect(acceptParams(12, report, 'd1')).toEqual({ expected_rev: 12, report_artifact_id: artifactId })
    expect(Object.keys(acceptParams(12, report, 'd1')).sort()).toEqual(['expected_rev', 'report_artifact_id'])
    expect(acceptParams(0, report, 'd1').expected_rev).toBe(0)
    expect(acceptParams(2147483647, report, 'd1').expected_rev).toBe(2147483647)
  })

  it('SE11 refuses missing reports and reports for another drawing', () => {
    expect(acceptParams(12, report, 'd2')).toBeNull()
    expect(acceptParams(12, null, 'd1')).toBeNull()
    expect(acceptParams(12, {}, undefined)).toBeNull()
  })

  it.each([undefined, null, -1, 1.5, 2147483648, NaN, Infinity, '12'])
  ('SE12 refuses revision %j', (rev) => {
    expect(acceptParams(rev, report, 'd1')).toBeNull()
  })

  it.each([undefined, '', 'a'.repeat(63), 'a'.repeat(65), 'A'.repeat(64), 'g'.repeat(64), 'a'.repeat(64) + '\n'])
  ('SE10 refuses invalid report artifact id %j', (id) => {
    expect(acceptParams(12, { ...report, report: { artifact_id: id } }, 'd1')).toBeNull()
  })

  it('SE1 summarizes every count in review order without changing its value', () => {
    const value = { counts: { partial_strings: 12, unassigned_panels: 11, assigned_panels: 10,
      strings: 9, bridge_strings: 8, group_strings: 7, matched_frames: 6, frames: 5,
      bridge_grids: 4, matchable_grids: 3, pdf_panels: 2, pdf_matrices: 1 } }
    expect(reportSummary(value).map((row) => [row.label, row.value])).toEqual([
      ['PDF matrices', 1], ['PDF panels', 2], ['Matchable grids', 3], ['Bridge grids', 4],
      ['Panel groups', 5], ['Matched panel groups', 6], ['Group strings', 7], ['Bridge strings', 8],
      ['Strings', 9], ['Assigned panels', 10], ['Unassigned panels', 11], ['Partial strings', 12],
    ])
    expect(new Set(reportSummary(value).map((row) => row.key)).size).toBe(12)
    expect(value.counts.partial_strings).toBe(12)
  })
})
