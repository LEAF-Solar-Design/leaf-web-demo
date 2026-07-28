// Small validator unit test for the widened CAPABILITY_ID regex in
// proofReceipt.mjs. The non-staging tests below have no ledger allowlist: the
// regex only checks shape. At the staging tier, the ledger is the allowlist.
//
// Run with: node --test web/e2e/proofReceipt.test.mjs
import assert from 'node:assert/strict'
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { test } from 'node:test'
import { makeProofReceipt, writeProofReceipt } from './proofReceipt.mjs'

const nonStagingBaseInput = {
  evidence_tier: 'local-e2e',
  route: '/try',
  runtime: 'test',
  result: { verdict: 'pass' },
}

const stagingBaseInput = {
  evidence_tier: 'staging',
  route: '/try',
  runtime: 'test',
  result: { verdict: 'pass' },
  source_commit: 'd6f548b',
  sub_cases: { proven: ['healthy'], not_proven: ['degraded', 'retry', 'recovery'] },
}

test('accepts a plain two-letter/two-digit capability id', () => {
  const receipt = makeProofReceipt({ ...nonStagingBaseInput, capability_ids: ['CA-01'] })
  assert.deepEqual(receipt.capability_ids, ['CA-01'])
})

test('accepts a letter-suffixed ledger id like ID-04A and ID-04B', () => {
  const receipt = makeProofReceipt({ ...nonStagingBaseInput, capability_ids: ['ID-04A', 'ID-04B'] })
  assert.deepEqual(receipt.capability_ids, ['ID-04A', 'ID-04B'])
})

test('rejects a lowercase capability id', () => {
  assert.throws(
    () => makeProofReceipt({ ...nonStagingBaseInput, capability_ids: ['ca-01'] }),
    /invalid capability id/,
  )
})

test('rejects two or more trailing letters', () => {
  assert.throws(
    () => makeProofReceipt({ ...nonStagingBaseInput, capability_ids: ['CA-01ZZ'] }),
    /invalid capability id/,
  )
})

test('rejects garbage that merely contains a valid-looking id', () => {
  for (const garbage of ['CA-01-extra', ' CA-01', 'CA-01 ', 'CA-1', 'C-01', 'CA-001', 'ZZ_99']) {
    assert.throws(
      () => makeProofReceipt({ ...nonStagingBaseInput, capability_ids: [garbage] }),
      /invalid capability id/,
      `expected "${garbage}" to be rejected`,
    )
  }
})

test('the non-staging regex checks shape only', () => {
  const receipt = makeProofReceipt({ ...nonStagingBaseInput, capability_ids: ['ZZ-99Z'] })
  assert.deepEqual(receipt.capability_ids, ['ZZ-99Z'])
})

test('rejects a staging receipt without sub-case accounting', () => {
  const { sub_cases, ...withoutSubCases } = stagingBaseInput
  assert.throws(
    () => makeProofReceipt({ ...withoutSubCases, capability_ids: ['HL-01'] }),
    /requires sub_cases/,
  )
})

test('rejects a malformed staging source commit', () => {
  assert.throws(
    () => makeProofReceipt({ ...stagingBaseInput, capability_ids: ['HL-01'], source_commit: 'local-worktree' }),
    /well-formed source_commit/,
  )
})

test('requires an explicit staging source commit', () => {
  const { source_commit, ...withoutSourceCommit } = stagingBaseInput
  assert.throws(
    () => makeProofReceipt({ ...withoutSourceCommit, capability_ids: ['HL-01'] }),
    /well-formed source_commit/,
  )
})

test('accepts the readiness sha256 source revision form', () => {
  const receipt = makeProofReceipt({
    ...stagingBaseInput,
    capability_ids: ['HL-01'],
    source_commit: `sha256:${'a'.repeat(64)}`,
  })
  assert.equal(receipt.source_commit, `sha256:${'a'.repeat(64)}`)
})

test('rejects a short sha256 staging source revision', () => {
  assert.throws(
    () => makeProofReceipt({ ...stagingBaseInput, capability_ids: ['HL-01'], source_commit: 'sha256:abcdef0' }),
    /well-formed source_commit/,
  )
})

test('staging receipts use the ledger as the sub-case authority', () => {
  const receipt = makeProofReceipt({ ...stagingBaseInput, capability_ids: ['HL-01'] })
  assert.deepEqual(receipt.sub_cases, {
    proven: ['healthy'],
    not_proven: ['degraded', 'retry', 'recovery'],
    row_complete: false,
  })
})

test('rejects an invented staging sub-case', () => {
  assert.throws(
    () => makeProofReceipt({ ...stagingBaseInput, capability_ids: ['HL-01'], sub_cases: { proven: ['healthy', 'invented'], not_proven: ['degraded', 'retry', 'recovery'] } }),
    /must match the ledger row/,
  )
})

test('rejects an omitted staging sub-case', () => {
  assert.throws(
    () => makeProofReceipt({ ...stagingBaseInput, capability_ids: ['HL-01'], sub_cases: { proven: ['healthy'], not_proven: ['degraded', 'retry'] } }),
    /must cover every ledger sub-case/,
  )
})

test('rejects overlapping staging sub-cases', () => {
  assert.throws(
    () => makeProofReceipt({ ...stagingBaseInput, capability_ids: ['HL-01'], sub_cases: { proven: ['healthy', 'degraded'], not_proven: ['degraded', 'retry', 'recovery'] } }),
    /must be disjoint/,
  )
})

test('rejects duplicate staging sub-cases', () => {
  assert.throws(
    () => makeProofReceipt({ ...stagingBaseInput, capability_ids: ['HL-01'], sub_cases: { proven: ['healthy', 'healthy'], not_proven: ['degraded', 'retry', 'recovery'] } }),
    /must not contain duplicates/,
  )
})

test('rejects a staging receipt with multiple capability ids', () => {
  assert.throws(
    () => makeProofReceipt({ ...stagingBaseInput, capability_ids: ['HL-01', 'CA-01'] }),
    /requires exactly one capability id/,
  )
})

test('requires every staging artifact to exist before writing the receipt', () => {
  const receiptDir = mkdtempSync(join(tmpdir(), 'proof-receipt-'))
  try {
    const missingPath = join(receiptDir, 'missing.png')
    assert.throws(
      () => writeProofReceipt(join(receiptDir, 'receipt.json'), {
        ...stagingBaseInput,
        capability_ids: ['HL-01'],
        artifacts: [missingPath],
      }),
      /artifact does not exist/,
    )

    const artifactPath = join(receiptDir, 'evidence.png')
    writeFileSync(artifactPath, 'evidence')
    const receipt = writeProofReceipt(join(receiptDir, 'receipt.json'), {
      ...stagingBaseInput,
      capability_ids: ['HL-01'],
      artifacts: [artifactPath],
    })
    assert.equal(receipt.sub_cases.row_complete, false)
  } finally {
    rmSync(receiptDir, { recursive: true, force: true })
  }
})
