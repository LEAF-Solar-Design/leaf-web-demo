import { lenses } from '../e2e/walk/lenses/index.mjs'

const identityFields = ['deployment_identity', 'fixture_version', 'catalog_version', 'browser_version', 'attempt']
const personas = lenses.map(({ name }) => name)
const featureId = /^(?:(?:action|surface|drawer|tool|control):[a-z0-9]+(?:-[a-z0-9]+)*|tab:[a-z0-9]+(?:-[a-z0-9]+)*:[a-z0-9]+(?:-[a-z0-9]+)*)$/
const wireId = /^[A-Za-z][A-Za-z0-9_-]*(?:[.:][A-Za-z0-9_-]+)*$/
const compare = (a, b) => a < b ? -1 : a > b ? 1 : 0
const object = (value) => value !== null && typeof value === 'object' && !Array.isArray(value)
const nonempty = (value) => typeof value === 'string' && value.trim().length > 0
const keyOf = ({ feature_id, state, viewport }) => JSON.stringify([feature_id, state, viewport])
const baseVerdicts = new Map([['pass', 'PASS'], ['fail', 'FAIL'], ['unsupported_local', 'UNAVAILABLE'],
  ['staging_only', 'UNAVAILABLE'], ['queued', 'UNKNOWN']])
const personaVerdicts = new Map([['pass', 'PASS'], ['finding', 'FAIL'], ['unknown', 'UNKNOWN']])
const rank = { PASS: 0, UNKNOWN: 1, UNAVAILABLE: 2, FAIL: 3 }

function requireArray(value, label, nonEmpty = false) {
  if (!Array.isArray(value) || (nonEmpty && !value.length)) throw new TypeError(`${label} must be ${nonEmpty ? 'a nonempty' : 'an'} array`)
  return value
}

function identifier(value, label, pattern = wireId, max = 200) {
  if (!nonempty(value) || value.length > max || !pattern.test(value)) throw new TypeError(`Invalid ${label}`)
  return value
}

function triple(value, label) {
  if (!object(value)) throw new TypeError(`${label} must be an object`)
  identifier(value.feature_id, `${label}.feature_id`)
  identifier(value.state, `${label}.state`, /^[^\s\x00-\x1f\x7f]+$/, 64)
  if (!['desktop', 'phone'].includes(value.viewport)) throw new TypeError(`Invalid ${label}.viewport`)
  return keyOf(value)
}

// Canonical JSON both detaches identity objects and compares object keys without
// treating their insertion order as part of the receipt's identity.
function jsonCopy(value, ancestors = new Set()) {
  if (value === null || typeof value === 'string' || typeof value === 'boolean'
      || (typeof value === 'number' && Number.isFinite(value))) return value
  if (!Array.isArray(value) && !object(value)) throw new TypeError('Identity must contain JSON values')
  if (ancestors.has(value)) throw new TypeError('Identity must be acyclic JSON')
  ancestors.add(value)
  const result = Array.isArray(value) ? value.map((item) => jsonCopy(item, ancestors))
    : Object.fromEntries(Object.keys(value).sort(compare).map((key) => [key, jsonCopy(value[key], ancestors)]))
  ancestors.delete(value)
  return result
}

export function buildPersonaCoverage({ featureMap, walkReceipt, evaluations = [] }) {
  if (walkReceipt?.schema !== 'leaf.studio-walk.v1') throw new TypeError('Expected leaf.studio-walk.v1 receipt')
  if (!nonempty(featureMap?.catalog_version) || featureMap.catalog_version !== walkReceipt.catalog_version) {
    throw new Error('Feature map catalog_version must match receipt catalog_version')
  }
  const identity = Object.fromEntries(identityFields.map((field) => [field,
    walkReceipt[field] === undefined ? null : jsonCopy(walkReceipt[field])]))
  const inventory = new Map()
  for (const entry of requireArray(featureMap.entries, 'featureMap.entries')) {
    identifier(entry?.id, 'feature id', featureId)
    requireArray(entry.states, `${entry.id}.states`, true)
    requireArray(entry.viewports, `${entry.id}.viewports`, true)
    for (const viewport of entry.viewports) {
      if (!['desktop', 'phone'].includes(viewport)) throw new TypeError(`Invalid ${entry.id}.viewport`)
    }
    if (entry.state_viewports !== undefined) {
      if (!object(entry.state_viewports)) throw new TypeError('state_viewports must be an object')
      for (const [state, viewports] of Object.entries(entry.state_viewports)) {
        if (!entry.states.includes(state)) throw new Error('state_viewports names unknown state')
        requireArray(viewports, 'state_viewports viewports', true)
      }
    }
    for (const state of entry.states) for (const viewport of
      (Object.hasOwn(entry.state_viewports || {}, state) ? entry.state_viewports[state] : entry.viewports)) {
      const row = { feature_id: entry.id, state, viewport }
      const key = triple(row, 'inventory triple')
      if (inventory.has(key)) throw new Error('Duplicate inventory triple')
      inventory.set(key, row)
    }
  }
  const observations = new Map()
  for (const row of requireArray(walkReceipt.evidence?.cases, 'receipt.evidence.cases')) {
    const key = triple(row, 'receipt case')
    // Journey and control census cases do not enlarge the feature denominator.
    if (!inventory.has(key)) continue
    const verdict = baseVerdicts.get(row.verdict) || 'UNKNOWN'
    if (!observations.has(key) || rank[verdict] > rank[observations.get(key)]) observations.set(key, verdict)
  }
  const gaps = new Set()
  const exactGapFeatures = new Set()
  for (const row of requireArray(walkReceipt.evidence.coverage_gaps === undefined ? [] : walkReceipt.evidence.coverage_gaps, 'receipt.evidence.coverage_gaps')) {
    const key = triple(row, 'coverage gap')
    exactGapFeatures.add(row.feature_id)
    if (inventory.has(key)) gaps.add(key)
  }
  // Wire gaps have only feature_id/reason. Prefer the lossless projection; if
  // unavailable, keep the whole feature unknown rather than parsing prose.
  for (const gap of requireArray(walkReceipt.coverage_gaps === undefined ? [] : walkReceipt.coverage_gaps, 'receipt.coverage_gaps')) {
    identifier(gap?.feature_id, 'coverage gap feature_id')
    if (!exactGapFeatures.has(gap.feature_id)) for (const [key, row] of inventory) {
      if (row.feature_id === gap.feature_id) gaps.add(key)
    }
  }
  const evaluated = new Map()
  for (const evaluation of requireArray(evaluations, 'evaluations')) {
    const key = triple(evaluation, 'evaluation triple')
    if (!inventory.has(key)) throw new Error('Evaluation names unknown inventory triple')
    if (!object(evaluation.identity) || Object.keys(evaluation.identity).length !== identityFields.length
        || identityFields.some((field) => !Object.hasOwn(evaluation.identity, field))
        || JSON.stringify(jsonCopy(evaluation.identity)) !== JSON.stringify(jsonCopy(identity))) {
      throw new Error('Evaluation identity must match receipt identity')
    }
    const reference = evaluation.evidence_ref
    if (!nonempty(reference) || reference.length > 2000 || !/^[A-Za-z0-9][A-Za-z0-9_./:#-]*$/.test(reference)
        || reference.includes('..') || reference.includes('://')) throw new TypeError('Invalid evidence_ref: expected a safe nonempty reference')
    for (const result of requireArray(evaluation.lenses, 'evaluation.lenses')) {
      if (!personas.includes(result?.lens)) throw new Error('Unknown lens name')
      if (!personaVerdicts.has(result.status)) throw new Error('Invalid lens status')
      const resultKey = JSON.stringify([key, result.lens])
      if (evaluated.has(resultKey)) throw new Error('Duplicate triple/lens evaluation')
      evaluated.set(resultKey, { verdict: personaVerdicts.get(result.status), reference })
    }
  }
  const rows = [], uncovered = []
  const totals = { PASS: 0, FAIL: 0, UNKNOWN: 0, UNAVAILABLE: 0 }
  const triples = [...inventory.entries()].sort(([, a], [, b]) =>
    compare(a.feature_id, b.feature_id) || compare(a.state, b.state) || compare(a.viewport, b.viewport))
  for (const [key, context] of triples) for (const persona of [...personas].sort(compare)) {
    let case_verdict = observations.get(key) || 'UNKNOWN'
    if (gaps.has(key) && case_verdict === 'PASS') case_verdict = 'UNKNOWN'
    const result = evaluated.get(JSON.stringify([key, persona]))
    const persona_verdict = result?.verdict || 'UNKNOWN'
    const row = { ...context, persona, case_verdict, persona_verdict,
      verdict: case_verdict === 'PASS' ? persona_verdict : case_verdict, evidence_ref: result?.reference || null }
    rows.push(row)
    totals[row.verdict]++
    const reason = gaps.has(key) ? 'coverage_gap' : !observations.has(key) ? 'missing_case'
      : !result ? 'missing_persona' : persona_verdict === 'UNKNOWN' ? 'unknown_persona' : null
    if (reason) uncovered.push({ ...row, reason })
  }
  return { schema: 'leaf.studio-persona-coverage.v1', ...identity, rows, totals, uncovered, denominator: rows.length }
}
