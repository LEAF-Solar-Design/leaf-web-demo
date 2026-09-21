// Small validator unit test for the widened CAPABILITY_ID regex in
// proofReceipt.mjs. The non-staging tests below have no ledger allowlist: the
// regex only checks shape. At the staging tier, the ledger is the allowlist.
//
// Run with: node --test web/e2e/proofReceipt.test.mjs
import assert from 'node:assert/strict'
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { test } from 'node:test'
import { makeProofReceipt, resolveProofArtifact, writeProofReceipt } from './proofReceipt.mjs'

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

function withArtifactPaths(run) {
  const root = mkdtempSync(join(tmpdir(), 'proof-artifact-'))
  try {
    const cwd = join(root, 'web')
    const receiptDir = join(root, 'receipts')
    mkdirSync(cwd)
    mkdirSync(receiptDir)
    run({ root, cwd, receiptDir, receiptPath: join(receiptDir, 'receipt.json') })
  } finally {
    rmSync(root, { recursive: true, force: true })
  }
}

function artifactInput(artifact) {
  return { ...nonStagingBaseInput, capability_ids: ['CA-01'], artifacts: [artifact] }
}

test("a receipt-dir-relative artifact resolves against the receipt's directory", () => {
  withArtifactPaths(({ cwd, receiptDir, receiptPath }) => {
    const artifact = 'beside-receipt.png'
    writeFileSync(join(receiptDir, artifact), 'evidence')
    writeProofReceipt(receiptPath, artifactInput(artifact), { cwd })
    assert.ok(existsSync(receiptPath))
    assert.ok(existsSync(resolveProofArtifact(artifact, receiptPath, { cwd })))
  })
})

test('a repo-root-relative artifact resolves against the parent of cwd', () => {
  withArtifactPaths(({ root, cwd, receiptPath }) => {
    mkdirSync(join(root, 'artifacts'))
    writeFileSync(join(root, 'artifacts', 'x.png'), 'evidence')
    writeProofReceipt(receiptPath, artifactInput('artifacts/x.png'), { cwd })
    assert.ok(existsSync(receiptPath))
    assert.ok(existsSync(resolveProofArtifact('artifacts/x.png', receiptPath, { cwd })))
  })
})

test('a cwd-relative artifact resolves against cwd', () => {
  withArtifactPaths(({ cwd, receiptPath }) => {
    writeFileSync(join(cwd, 'y.png'), 'evidence')
    writeProofReceipt(receiptPath, artifactInput('y.png'), { cwd })
    assert.ok(existsSync(receiptPath))
    assert.ok(existsSync(resolveProofArtifact('y.png', receiptPath, { cwd })))
  })
})

test('an absolute artifact resolves as given', () => {
  withArtifactPaths(({ root, cwd, receiptPath }) => {
    const artifact = join(root, 'absolute.png')
    writeFileSync(artifact, 'evidence')
    writeProofReceipt(receiptPath, artifactInput(artifact), { cwd })
    assert.ok(existsSync(receiptPath))
    assert.ok(existsSync(resolveProofArtifact(artifact, receiptPath, { cwd })))
  })
})

test('a Playwright video resolves by its prepared directory before the file exists', () => {
  withArtifactPaths(({ root, cwd, receiptPath }) => {
    const preparedDir = join(root, 'prepared-output')
    mkdirSync(preparedDir)
    const artifact = join(preparedDir, 'video.webm')
    assert.equal(existsSync(artifact), false)
    writeProofReceipt(receiptPath, artifactInput(artifact), { cwd })
    assert.ok(existsSync(receiptPath))
    assert.notEqual(resolveProofArtifact(artifact, receiptPath, { cwd }), null)
    assert.equal(existsSync(artifact), false)
  })
})

test('a Playwright video whose directory was never prepared is refused', () => {
  withArtifactPaths(({ root, cwd, receiptPath }) => {
    const artifact = join(root, 'absent-output', 'video.webm')
    assert.equal(resolveProofArtifact(artifact, receiptPath, { cwd }), null)
    assert.throws(
      () => writeProofReceipt(receiptPath, artifactInput(artifact), { cwd }),
      /local-e2e proof receipt artifact does not exist/,
    )
    assert.equal(existsSync(receiptPath), false)
  })
})

test('an artifact absent under every base is refused naming the tier and the bases', () => {
  withArtifactPaths(({ cwd, receiptDir, receiptPath }) => {
    const artifact = 'absent.png'
    assert.equal(resolveProofArtifact(artifact, receiptPath, { cwd }), null)
    assert.throws(
      () => writeProofReceipt(receiptPath, artifactInput(artifact), { cwd }),
      (error) => {
        assert.match(error.message, /local-e2e proof receipt artifact does not exist: absent\.png/)
        const tried = error.message.split('(tried: ')[1]
        assert.ok(tried)
        assert.equal(tried.slice(0, -1).split('; ').length, 3)
        for (const candidate of [join(receiptDir, artifact), join(cwd, '..', artifact), join(cwd, artifact)]) {
          assert.ok(tried.includes(candidate))
        }
        return true
      },
    )
    assert.equal(existsSync(receiptPath), false)
  })
})

test('the receipt stores the declared artifact string, never the resolved path', () => {
  withArtifactPaths(({ root, cwd, receiptPath }) => {
    mkdirSync(join(root, 'artifacts'))
    writeFileSync(join(root, 'artifacts', 'x.png'), 'evidence')
    const receipt = writeProofReceipt(receiptPath, artifactInput('artifacts/x.png'), { cwd })
    assert.deepEqual(receipt.artifacts, ['artifacts/x.png'])
    assert.deepEqual(JSON.parse(readFileSync(receiptPath, 'utf8')).artifacts, ['artifacts/x.png'])
  })
})

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

test('a granular behavior-pin row (4 cells) yields its single behavior as the sub-case set', () => {
  // MO-02 lives in the ledger's granular table, not the main capability
  // table; its sub-case set is exactly the one Behavior cell.
  const receipt = makeProofReceipt({
    ...stagingBaseInput,
    capability_ids: ['MO-02'],
    sub_cases: { proven: [], not_proven: ['No fill-mode snap under reduced motion'] },
  })
  assert.deepEqual(receipt.sub_cases.not_proven, ['No fill-mode snap under reduced motion'])
  assert.equal(receipt.sub_cases.row_complete, false)
})

test('an id with no single unambiguous ledger row fails closed', () => {
  // The round-4 reviewer reproduced first-row-wins with a duplicated row;
  // the parser now requires EXACTLY one match across both tables (zero
  // matches exercises the same rule).
  assert.throws(
    () => makeProofReceipt({
      ...stagingBaseInput,
      capability_ids: ['QQ-99'],
      sub_cases: { proven: [], not_proven: ['anything'] },
    }),
    /exactly one row/,
  )
})

test('a nonstandard port on an allowed hostname is a different origin', async () => {
  // Round-5 BLOCKING: ports are part of an origin. The guards must refuse
  // https://platform-staging.leafdesign.ai:4443 everywhere.
  const { assertAllowedStagingHost, assertResponseOnAllowedOrigin, StagingHostError } =
    await import('./staging/stagingConfig.mjs')
  assert.throws(
    () => assertAllowedStagingHost('https://platform-staging.leafdesign.ai:4443', {}),
    StagingHostError,
  )
  assert.throws(
    () => assertResponseOnAllowedOrigin({ url: () => 'https://platform-staging.leafdesign.ai:4443/steal' }, {}),
    StagingHostError,
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

test('a local-e2e receipt refuses a declared artifact that does not exist', () => {
  const receiptDir = mkdtempSync(join(tmpdir(), 'proof-receipt-'))
  try {
    const receiptPath = join(receiptDir, 'receipt.json')
    assert.throws(
      () => writeProofReceipt(receiptPath, {
        ...nonStagingBaseInput,
        capability_ids: ['CA-01'],
        artifacts: [join(receiptDir, 'missing.png')],
      }),
      /local-e2e proof receipt artifact does not exist/,
    )
    assert.equal(existsSync(receiptPath), false)
  } finally {
    rmSync(receiptDir, { recursive: true, force: true })
  }
})

test('a local-e2e receipt with existing artifacts writes', () => {
  const receiptDir = mkdtempSync(join(tmpdir(), 'proof-receipt-'))
  try {
    const artifactPath = join(receiptDir, 'evidence.png')
    const receiptPath = join(receiptDir, 'receipt.json')
    writeFileSync(artifactPath, 'evidence')
    const receipt = writeProofReceipt(receiptPath, {
      ...nonStagingBaseInput,
      capability_ids: ['CA-01'],
      artifacts: [artifactPath],
    })
    assert.deepEqual(receipt.artifacts, [artifactPath])
    assert.deepEqual(JSON.parse(readFileSync(receiptPath, 'utf8')).artifacts, [artifactPath])
  } finally {
    rmSync(receiptDir, { recursive: true, force: true })
  }
})

test('a contract receipt refuses a declared artifact that does not exist', () => {
  const receiptDir = mkdtempSync(join(tmpdir(), 'proof-receipt-'))
  try {
    const receiptPath = join(receiptDir, 'receipt.json')
    assert.throws(
      () => writeProofReceipt(receiptPath, {
        ...nonStagingBaseInput,
        evidence_tier: 'contract',
        capability_ids: ['CA-01'],
        artifacts: [join(receiptDir, 'missing.png')],
      }),
      /contract proof receipt artifact does not exist/,
    )
    assert.equal(existsSync(receiptPath), false)
  } finally {
    rmSync(receiptDir, { recursive: true, force: true })
  }
})

test('a receipt declaring no artifacts writes on every tier', () => {
  const receiptDir = mkdtempSync(join(tmpdir(), 'proof-receipt-'))
  try {
    for (const evidenceTier of ['contract', 'local-e2e', 'staging']) {
      const receiptPath = join(receiptDir, `${evidenceTier}.json`)
      const receipt = writeProofReceipt(receiptPath, {
        ...stagingBaseInput,
        evidence_tier: evidenceTier,
        capability_ids: ['HL-01'],
        artifacts: [],
      })
      assert.deepEqual(receipt.artifacts, [])
      assert.deepEqual(JSON.parse(readFileSync(receiptPath, 'utf8')).artifacts, [])
    }
  } finally {
    rmSync(receiptDir, { recursive: true, force: true })
  }
})

test('a non-string artifact is refused on every tier', () => {
  const receiptDir = mkdtempSync(join(tmpdir(), 'proof-receipt-'))
  try {
    for (const evidenceTier of ['contract', 'local-e2e', 'staging']) {
      const receiptPath = join(receiptDir, `${evidenceTier}.json`)
      assert.throws(
        () => writeProofReceipt(receiptPath, {
          ...stagingBaseInput,
          evidence_tier: evidenceTier,
          capability_ids: ['HL-01'],
          artifacts: [42],
        }),
        new RegExp(`${evidenceTier} proof receipt artifact does not exist`),
      )
      assert.equal(existsSync(receiptPath), false)
    }
  } finally {
    rmSync(receiptDir, { recursive: true, force: true })
  }
})
