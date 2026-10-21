import { expect, test } from '@playwright/test'
import { publishProjectSolar, switchProjectSolarForms } from '../walk/fixtures.mjs'
import { reviewStagingProposal, stagingAction, stagingRun, withStagingProject } from './projectSolarSteps.mjs'

const token = process.env.LEAF_E2E_STAGING_JWT || ''
const owner = process.env.LEAF_WALK_STAGING_OWNER_PROJECT_ID || ''
const scratch = process.env.LEAF_WALK_STAGING_SCRATCH_PROJECT_ID || ''

test.skip(!token, 'LEAF_E2E_STAGING_JWT is not set; signed-in staging walk requires a credential')
test.beforeEach(({}, testInfo) => {
  test.skip(testInfo.config.metadata?.stagingWalk !== true,
    'Signed-in staging walks require playwright.walk-staging.config.mjs')
})

function requireProject(projectId, variable, writing = false) {
  test.skip(!token, 'LEAF_E2E_STAGING_JWT is not set; signed-in staging walk requires a credential')
  test.skip(!projectId, `${variable} is not set; staging walk requires a pre-provisioned project`)
  // Refuse ambiguous write configuration before opening or taking a checkout.
  if (writing) {
    test.skip(!owner, 'LEAF_WALK_STAGING_OWNER_PROJECT_ID is not set; cannot confirm a separate scratch project')
    expect(scratch !== owner).toBe(true)
  }
}

test('project-solar-open', async ({ page, baseURL }, testInfo) => {
  requireProject(owner, 'LEAF_WALK_STAGING_OWNER_PROJECT_ID')
  await withStagingProject(page, baseURL, owner, token, testInfo, async () => {})
})

test('project-solar-form-switch', async ({ page, baseURL }, testInfo) => {
  requireProject(owner, 'LEAF_WALK_STAGING_OWNER_PROJECT_ID')
  await withStagingProject(page, baseURL, owner, token, testInfo, runtime => switchProjectSolarForms(runtime))
})

test('project-solar-published', async ({ page, baseURL }, testInfo) => {
  requireProject(scratch, 'LEAF_WALK_STAGING_SCRATCH_PROJECT_ID', true)
  await withStagingProject(page, baseURL, scratch, token, testInfo, async runtime => {
    await publishProjectSolar(runtime, expect, { action: stagingAction, run: stagingRun, allowExistingGraph: true })
    testInfo.annotations.push({ type: 'solar-settings-publication',
      description: JSON.stringify(runtime.evidence.projectSolar.publication) })
  })
})

test('project-solar-proposal-review', async ({ page, baseURL }, testInfo) => {
  requireProject(scratch, 'LEAF_WALK_STAGING_SCRATCH_PROJECT_ID', true)
  await withStagingProject(page, baseURL, scratch, token, testInfo, async runtime => {
    const result = await reviewStagingProposal(runtime)
    testInfo.annotations.push({ type: result.reason ? 'solve-refusal' : 'solve-proposal-review', description: JSON.stringify(result) })
    test.skip(Boolean(result.reason), result.reason)
  })
})
