const object = value => value !== null && typeof value === 'object' && !Array.isArray(value)
const finite = value => typeof value === 'number' && Number.isFinite(value)

// Availability checks only. Raw evidence remains visible and is never corrected.
export function corpusReportDiagnostics(report) {
  const confirmation = report?.confirm_report
  if (confirmation == null) return []
  if (!object(confirmation)) return ['Confirmation evidence is invalid; treat statistical evidence as unknown.']
  const messages = []
  const calibration = confirmation.calibration
  if (calibration != null) {
    let invalid = !object(calibration), unavailable = false
    if (!invalid) for (const arm of ['parent', 'child']) {
      const value = calibration[arm]
      if (value == null) { unavailable = true; continue }
      if (!object(value) || !Number.isSafeInteger(value.n) || value.n < 0) { invalid = true; continue }
      if (value.n === 0) { unavailable = true; continue }
      for (const key of ['mean_claimed_prob', 'win_rate']) {
        const number = value[key]
        if (number == null) unavailable = true
        else if (!finite(number) || number < 0 || number > 1) invalid = true
      }
    }
    if (invalid) messages.push('Calibration evidence is invalid; treat calibration as unknown.')
    else if (unavailable) messages.push('Calibration evidence is unavailable or has no observations; calibration is unknown.')
  }
  const comparison = confirmation.confirm_comparison
  if (comparison != null) {
    let invalid = !object(comparison), unavailable = false
    if (!invalid) {
      const interval = comparison.paired_ci95_c
      if (interval == null) unavailable = true
      else if (!Array.isArray(interval) || interval.length !== 2) invalid = true
      else if (interval.some(value => value == null)) unavailable = true
      else if (!interval.every(finite) || interval[0] > interval[1]) invalid = true
      const mean = comparison.child_minus_parent_mean_c
      if (mean == null) unavailable = true
      else if (!finite(mean)) invalid = true
    }
    if (invalid) messages.push('Uncertainty evidence is invalid; treat uncertainty as unknown.')
    else if (unavailable) messages.push('Uncertainty evidence is unavailable; uncertainty is unknown.')
  }
  return messages
}
