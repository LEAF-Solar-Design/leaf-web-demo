import { SOLAREDGE_MIN_ALIGNMENT_TOLERANCE, SOLAREDGE_MAX_ALIGNMENT_TOLERANCE } from './solarImportClient.js'

const COUNT_ROWS = Object.freeze([
  ['pdf_matrices', 'PDF matrices'],
  ['pdf_panels', 'PDF panels'],
  ['matchable_grids', 'Matchable grids'],
  ['bridge_grids', 'Bridge grids'],
  ['frames', 'Panel groups'],
  ['matched_frames', 'Matched panel groups'],
  ['group_strings', 'Group strings'],
  ['bridge_strings', 'Bridge strings'],
  ['strings', 'Strings'],
  ['assigned_panels', 'Assigned panels'],
  ['unassigned_panels', 'Unassigned panels'],
  ['partial_strings', 'Partial strings'],
])

export function parseTolerance(text) {
  const literal = typeof text === 'string'
    ? text.match(/^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$/) : null
  if (!literal || literal[0] !== text) {
    return { ok: false, reason: 'Enter an alignment tolerance as a decimal number.' }
  }
  const value = Number(text)
  if (!Number.isFinite(value) || value < SOLAREDGE_MIN_ALIGNMENT_TOLERANCE
    || value > SOLAREDGE_MAX_ALIGNMENT_TOLERANCE) {
    return { ok: false, reason: 'Enter an alignment tolerance from 0.000001 through 1000000.' }
  }
  return { ok: true, value }
}

// The client validates these counts before returning a report.
export function reportSummary(value) {
  return COUNT_ROWS.map(([key, label]) => ({ key, label, value: value.counts[key] }))
}

export function acceptParams(graphRev, reportValue, currentDrawingId) {
  const artifactId = reportValue?.report?.artifact_id
  if (!reportValue || typeof currentDrawingId !== 'string' || currentDrawingId.length === 0
    || reportValue.drawing_id !== currentDrawingId
    || !Number.isInteger(graphRev) || graphRev < 0 || graphRev > 2147483647
    || typeof artifactId !== 'string' || !/^[0-9a-f]{64}$/.test(artifactId)
    || artifactId.length !== 64) return null
  return { expected_rev: graphRev, report_artifact_id: artifactId }
}
