import { mkdirSync, writeFileSync } from 'node:fs'
import { dirname } from 'node:path'

const EVIDENCE_TIERS = new Set(['contract', 'local-e2e', 'staging'])
const CAPABILITY_ID = /^[A-Z]{2}-\d{2}$/

export function makeProofReceipt(input) {
  if (!Array.isArray(input.capability_ids) || input.capability_ids.length === 0) {
    throw new Error('proof receipt requires at least one capability id')
  }
  if (!input.capability_ids.every((id) => CAPABILITY_ID.test(id))) {
    throw new Error('proof receipt contains an invalid capability id')
  }
  if (!EVIDENCE_TIERS.has(input.evidence_tier)) {
    throw new Error('proof receipt contains an invalid evidence tier')
  }
  if (!input.route || !input.runtime || !input.result) {
    throw new Error('proof receipt requires route, runtime, and result')
  }

  return {
    schema: 'leaf.unified-surface-proof.v1',
    capability_ids: [...new Set(input.capability_ids)].sort(),
    evidence_tier: input.evidence_tier,
    route: input.route,
    runtime: input.runtime,
    source_commit: input.source_commit || process.env.LEAF_SOURCE_COMMIT || 'local-worktree',
    api_endpoints: [...new Set(input.api_endpoints || [])].sort(),
    assertions: input.assertions || [],
    artifacts: input.artifacts || [],
    result: input.result,
    limitations: input.limitations || [],
  }
}

export function writeProofReceipt(path, input) {
  mkdirSync(dirname(path), { recursive: true })
  const receipt = makeProofReceipt(input)
  writeFileSync(path, `${JSON.stringify(receipt, null, 2)}\n`)
  return receipt
}
