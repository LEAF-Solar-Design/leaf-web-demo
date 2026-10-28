import { evaluateLenses } from '../e2e/walk/lenses/index.mjs'
import { buildPersonaCoverage } from './personaCoverage.mjs'

const compare = (a, b) => a < b ? -1 : a > b ? 1 : 0
const object = (value) => value !== null && typeof value === 'object' && !Array.isArray(value)
const rank = { pass: 0, unknown: 1, finding: 2 }
const keyOf = ({ feature_id, state, viewport }) => JSON.stringify([feature_id, state, viewport])

export function buildPersonaReport({ featureMap, walkReceipt, runs = [] }) {
  if (!Array.isArray(runs)) throw new TypeError('runs must be an array')
  const attempts = []
  for (const run of runs) {
    if (!object(run)) throw new TypeError('run must be an object')
    const { feature_id, state, viewport, identity, evidence_ref } = run
    attempts.push({ feature_id, state, viewport, identity, evidence_ref, lenses: [] })
  }
  // Validate every attempt with the companion's inventory, identity and reference
  // contract, including retries that will not determine a lens verdict.
  const uncovered = buildPersonaCoverage({ featureMap, walkReceipt, evaluations: attempts })
  if (!runs.length) return { ...uncovered, evidence_bundles: [] }

  const groups = new Map()
  for (let i = 0; i < runs.length; i++) {
    const run = runs[i]
    const evidence = run.evidence
    if (!object(evidence) || evidence.schema !== 'leaf.walk-evidence.v1') {
      throw new TypeError('Expected leaf.walk-evidence.v1 evidence')
    }
    for (const [field, expected] of [['featureId', run.feature_id], ['state', run.state], ['viewport', run.viewport]]) {
      if (Object.hasOwn(evidence, field) && evidence[field] !== expected) {
        throw new Error(`Evidence ${field} must match controller triple`)
      }
    }
    const key = keyOf(run)
    if (!groups.has(key)) groups.set(key, { ...attempts[i], sources: new Set(), results: new Map() })
    const group = groups.get(key)
    group.sources.add(run.evidence_ref)
    // Pass evidence through unchanged: absent context and measurements must stay
    // absent so the real predicates can report unknown observations.
    for (const { lens, status } of evaluateLenses(evidence)) {
      if (!group.results.has(lens) || rank[status] > rank[group.results.get(lens)]) group.results.set(lens, status)
    }
  }
  const ordered = [...groups.values()].sort((a, b) => compare(a.feature_id, b.feature_id)
    || compare(a.state, b.state) || compare(a.viewport, b.viewport))
  const evaluations = [], evidence_bundles = []
  for (const [index, group] of ordered.entries()) {
    const evidence_ref = `persona-attempts/${String(index + 1).padStart(6, '0')}`
    // These IDs locate bundles in this JSON artifact. Source attachment retention
    // belongs to the caller; this pure adapter creates no files.
    evidence_bundles.push({ evidence_ref, sources: [...group.sources].sort(compare) })
    evaluations.push({ feature_id: group.feature_id, state: group.state, viewport: group.viewport,
      identity: group.identity, evidence_ref,
      lenses: [...group.results].map(([lens, status]) => ({ lens, status })) })
  }
  return { ...buildPersonaCoverage({ featureMap, walkReceipt, evaluations }), evidence_bundles }
}
