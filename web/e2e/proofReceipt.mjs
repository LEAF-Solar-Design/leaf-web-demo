import { existsSync, mkdirSync, writeFileSync } from 'node:fs'
import { dirname } from 'node:path'

const EVIDENCE_TIERS = new Set(['contract', 'local-e2e', 'staging'])
const CAPABILITY_ID = /^[A-Z]{2}-\d{2}[A-Z]?$/
const SOURCE_COMMIT = /^(?:sha256:)?[0-9a-f]{7,64}$/i

function stagingSubCases(input) {
  const subCases = input.sub_cases
  if (!subCases || !Array.isArray(subCases.proven) || !Array.isArray(subCases.not_proven)) {
    throw new Error('staging proof receipt requires sub_cases.proven and sub_cases.not_proven arrays')
  }
  return {
    proven: [...subCases.proven],
    not_proven: [...subCases.not_proven],
    row_complete: subCases.not_proven.length === 0,
  }
}

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

  const sourceCommit = input.evidence_tier === 'staging'
    ? input.source_commit
    : input.source_commit || process.env.LEAF_SOURCE_COMMIT || 'local-worktree'
  if (input.evidence_tier === 'staging' && (typeof sourceCommit !== 'string' || !SOURCE_COMMIT.test(sourceCommit))) {
    throw new Error('staging proof receipt requires a well-formed source_commit')
  }

  const receipt = {
    schema: 'leaf.unified-surface-proof.v1',
    capability_ids: [...new Set(input.capability_ids)].sort(),
    evidence_tier: input.evidence_tier,
    route: input.route,
    runtime: input.runtime,
    source_commit: sourceCommit,
    api_endpoints: [...new Set(input.api_endpoints || [])].sort(),
    assertions: input.assertions || [],
    artifacts: input.artifacts || [],
    result: input.result,
    limitations: input.limitations || [],
  }
  if (input.evidence_tier === 'staging') receipt.sub_cases = stagingSubCases(input)
  return receipt
}

export function writeProofReceipt(path, input) {
  mkdirSync(dirname(path), { recursive: true })
  const receipt = makeProofReceipt(input)
  if (receipt.evidence_tier === 'staging') {
    for (const artifact of receipt.artifacts) {
      if (typeof artifact !== 'string' || !existsSync(artifact)) {
        throw new Error(`staging proof receipt artifact does not exist: ${artifact}`)
      }
    }
  }
  writeFileSync(path, `${JSON.stringify(receipt, null, 2)}\n`)
  return receipt
}
