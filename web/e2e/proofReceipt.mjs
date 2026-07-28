import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const EVIDENCE_TIERS = new Set(['contract', 'local-e2e', 'staging'])
const CAPABILITY_ID = /^[A-Z]{2}-\d{2}[A-Z]?$/
const SOURCE_COMMIT = /^(?:[0-9a-f]{7,64}|sha256:[0-9a-f]{64})$/i
const LEDGER_PATH = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..', 'plans', 'UNIFIED-SURFACE-E2E-EXECUTION.md')
let ledgerText
const ledgerRows = new Map()

function ledgerSubCases(capabilityId) {
  if (!ledgerText) {
    try {
      ledgerText = readFileSync(LEDGER_PATH, 'utf8')
    } catch (error) {
      throw new Error(`staging proof receipt could not read ledger: ${error.message}`)
    }
  }

  if (!ledgerRows.has(capabilityId)) {
    // Fail-closed row resolution: collect EVERY table row whose first cell is
    // this id, across BOTH ledger tables. Exactly one match is required — a
    // duplicated or wrapped row must throw, never silently win by position.
    const matches = ledgerText.split(/\r?\n/)
      .map((line) => line.split('|').slice(1, -1).map((cell) => cell.trim()))
      .filter((cells) => cells[0] === capabilityId)
    if (matches.length !== 1) {
      throw new Error(
        `staging proof receipt ledger must contain exactly one row for ${capabilityId}, found ${matches.length}`,
      )
    }
    const row = matches[0]
    let subCases
    if (row.length === 6) {
      // Main capability table: | ID | Capability | Mechanism | Surface |
      // STATUS | subcase, subcase, ... |
      if (row.some((cell) => !cell)) {
        throw new Error(`staging proof receipt ledger row is unparseable for ${capabilityId}`)
      }
      subCases = row.at(-1).split(',').map((subCase) => subCase.trim())
    } else if (row.length === 4) {
      // Granular behavior-pin table: | ID | Behavior | Initial status |
      // Required proof |. A pin IS its single behavior — the sub-case set is
      // exactly that one behavior string.
      if (row.some((cell) => !cell)) {
        throw new Error(`staging proof receipt ledger row is unparseable for ${capabilityId}`)
      }
      subCases = [row[1]]
    } else {
      throw new Error(`staging proof receipt ledger row is missing or unparseable for ${capabilityId}`)
    }
    if (subCases.length === 0 || subCases.some((subCase) => !subCase) || new Set(subCases).size !== subCases.length) {
      throw new Error(`staging proof receipt found an unparseable sub-case list for ${capabilityId}`)
    }
    ledgerRows.set(capabilityId, subCases)
  }

  const subCases = ledgerRows.get(capabilityId)
  return subCases
}

function stagingSubCases(input, capabilityId) {
  const subCases = input.sub_cases
  if (!subCases || !Array.isArray(subCases.proven) || !Array.isArray(subCases.not_proven)) {
    throw new Error('staging proof receipt requires sub_cases.proven and sub_cases.not_proven arrays')
  }
  const proven = [...subCases.proven]
  const notProven = [...subCases.not_proven]
  const ledgerCases = ledgerSubCases(capabilityId)
  const ledgerCaseSet = new Set(ledgerCases)
  const claimedCases = [...proven, ...notProven]
  if (new Set(proven).size !== proven.length || new Set(notProven).size !== notProven.length) {
    throw new Error('staging proof receipt sub_cases must not contain duplicates')
  }
  if (proven.some((subCase) => notProven.includes(subCase))) {
    throw new Error('staging proof receipt sub_cases.proven and sub_cases.not_proven must be disjoint')
  }
  if (claimedCases.some((subCase) => !ledgerCaseSet.has(subCase))) {
    throw new Error(`staging proof receipt sub_cases must match the ledger row for ${capabilityId}`)
  }
  if (claimedCases.length !== ledgerCases.length || new Set(claimedCases).size !== ledgerCases.length) {
    throw new Error(`staging proof receipt sub_cases must cover every ledger sub-case for ${capabilityId}`)
  }
  return {
    proven,
    not_proven: notProven,
    row_complete: notProven.length === 0,
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
  if (input.evidence_tier === 'staging' && input.capability_ids.length !== 1) {
    throw new Error('staging proof receipt requires exactly one capability id')
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
  if (input.evidence_tier === 'staging') receipt.sub_cases = stagingSubCases(input, receipt.capability_ids[0])
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
