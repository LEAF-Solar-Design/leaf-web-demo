import { describe, expect, it } from 'vitest'
import { corpusReportDiagnostics } from './corpusReportDiagnostics.js'
const arm = () => ({ n: 3, mean_claimed_prob: .5, win_rate: .4 })
const report = () => ({ confirm_report: { calibration: { parent: arm(), child: arm() }, confirm_comparison: { paired_ci95_c: [-1, 1], child_minus_parent_mean_c: 0 } } })
describe('corpus report field availability', () => {
  it('leaves valid finite evidence unchanged without declaring success', () => {
    const value = report(), before = structuredClone(value)
    expect(corpusReportDiagnostics(value)).toEqual([])
    expect(value).toEqual(before)
  })
  it('leaves absent confirmation to the existing unavailable fields', () => {
    expect(corpusReportDiagnostics({ confirm_report: null })).toEqual([])
  })
  it('labels zero counts and null intervals as unknown', () => {
    const value = report(); value.confirm_report.calibration.parent.n = 0; value.confirm_report.confirm_comparison.paired_ci95_c = [null, null]
    expect(corpusReportDiagnostics(value)).toEqual([expect.stringContaining('no observations'), expect.stringContaining('uncertainty is unknown')])
  })
  it.each(['not a metric', 5, []])('rejects a malformed calibration container %j', value => {
    expect(corpusReportDiagnostics({ confirm_report: { calibration: value } })).toEqual([expect.stringContaining('invalid')])
  })
  it.each([true, -1, 1.5, Infinity])('rejects a malformed count %s', n => {
    const value = report(); value.confirm_report.calibration.child.n = n
    expect(corpusReportDiagnostics(value)).toEqual([expect.stringContaining('invalid')])
  })
  it.each(['0.5', true, NaN, Infinity, -.1, 1.1])('keeps malformed probabilities unknown %s', probability => {
    const value = report(); value.confirm_report.calibration.child.mean_claimed_prob = probability
    const before = structuredClone(value)
    expect(corpusReportDiagnostics(value)).toEqual([expect.stringContaining('invalid')]); expect(value).toEqual(before)
  })
  it('refuses inverted intervals without replacing them', () => {
    const value = report(); value.confirm_report.confirm_comparison.paired_ci95_c = [2, 1]
    expect(corpusReportDiagnostics(value)).toEqual([expect.stringContaining('invalid')]); expect(value.confirm_report.confirm_comparison.paired_ci95_c).toEqual([2, 1])
  })
})
