// Small validator unit test for the widened CAPABILITY_ID regex in
// proofReceipt.mjs. No ledger allowlist here on purpose: the ledger at
// plans/UNIFIED-SURFACE-E2E-EXECUTION.md is the authority on which IDs are
// real; this only pins the shape the regex accepts.
//
// Run with: node --test web/e2e/proofReceipt.test.mjs
import assert from 'node:assert/strict'
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { test } from 'node:test'
import { makeProofReceipt, writeProofReceipt } from './proofReceipt.mjs'

const baseInput = {
  evidence_tier: 'staging',
  route: '/try',
  runtime: 'test',
  result: { verdict: 'pass' },
  source_commit: 'd6f548b',
  sub_cases: { proven: ['healthy'], not_proven: [] },
}

test('accepts a plain two-letter/two-digit capability id', () => {
  const receipt = makeProofReceipt({ ...baseInput, capability_ids: ['CA-01'] })
  assert.deepEqual(receipt.capability_ids, ['CA-01'])
})

test('accepts a letter-suffixed ledger id like ID-04A and ID-04B', () => {
  const receipt = makeProofReceipt({ ...baseInput, capability_ids: ['ID-04A', 'ID-04B'] })
  assert.deepEqual(receipt.capability_ids, ['ID-04A', 'ID-04B'])
})

test('rejects a lowercase capability id', () => {
  assert.throws(
    () => makeProofReceipt({ ...baseInput, capability_ids: ['ca-01'] }),
    /invalid capability id/,
  )
})

test('rejects two or more trailing letters', () => {
  assert.throws(
    () => makeProofReceipt({ ...baseInput, capability_ids: ['CA-01ZZ'] }),
    /invalid capability id/,
  )
})

test('rejects garbage that merely contains a valid-looking id', () => {
  for (const garbage of ['CA-01-extra', ' CA-01', 'CA-01 ', 'CA-1', 'C-01', 'CA-001', 'ZZ_99']) {
    assert.throws(
      () => makeProofReceipt({ ...baseInput, capability_ids: [garbage] }),
      /invalid capability id/,
      `expected "${garbage}" to be rejected`,
    )
  }
})

// The regex is deliberately permissive about WHICH two-letter/two-digit stem
// plus optional single letter suffix it accepts (e.g. it does not reject
// "ZZ-99Z", which is not a real ledger row). This is intentional: the shape
// check lives here, but the ledger at plans/UNIFIED-SURFACE-E2E-EXECUTION.md
// is the sole authority on which IDs are real. No allowlist is added here so
// this file never goes stale against the ledger.
test('the regex checks shape only; the ledger, not this file, is the authority on real ids', () => {
  const receipt = makeProofReceipt({ ...baseInput, capability_ids: ['ZZ-99Z'] })
  assert.deepEqual(receipt.capability_ids, ['ZZ-99Z'])
})

test('rejects a staging receipt without sub-case accounting', () => {
  const { sub_cases, ...withoutSubCases } = baseInput
  assert.throws(
    () => makeProofReceipt({ ...withoutSubCases, capability_ids: ['HL-01'] }),
    /requires sub_cases/,
  )
})

test('rejects a malformed staging source commit', () => {
  assert.throws(
    () => makeProofReceipt({ ...baseInput, capability_ids: ['HL-01'], source_commit: 'local-worktree' }),
    /well-formed source_commit/,
  )
})

test('requires an explicit staging source commit', () => {
  const { source_commit, ...withoutSourceCommit } = baseInput
  assert.throws(
    () => makeProofReceipt({ ...withoutSourceCommit, capability_ids: ['HL-01'] }),
    /well-formed source_commit/,
  )
})

test('accepts the readiness sha256 source revision form', () => {
  const receipt = makeProofReceipt({
    ...baseInput,
    capability_ids: ['HL-01'],
    source_commit: `sha256:${'a'.repeat(64)}`,
  })
  assert.equal(receipt.source_commit, `sha256:${'a'.repeat(64)}`)
})

test('requires every staging artifact to exist before writing the receipt', () => {
  const receiptDir = mkdtempSync(join(tmpdir(), 'proof-receipt-'))
  try {
    const missingPath = join(receiptDir, 'missing.png')
    assert.throws(
      () => writeProofReceipt(join(receiptDir, 'receipt.json'), {
        ...baseInput,
        capability_ids: ['HL-01'],
        artifacts: [missingPath],
      }),
      /artifact does not exist/,
    )

    const artifactPath = join(receiptDir, 'evidence.png')
    writeFileSync(artifactPath, 'evidence')
    const receipt = writeProofReceipt(join(receiptDir, 'receipt.json'), {
      ...baseInput,
      capability_ids: ['HL-01'],
      artifacts: [artifactPath],
    })
    assert.equal(receipt.sub_cases.row_complete, true)
  } finally {
    rmSync(receiptDir, { recursive: true, force: true })
  }
})
