import { defineConfig } from '@playwright/test'
import { isAbsolute, resolve } from 'node:path'
import { metadataFromEnvironment, exactOrigin } from './e2e/project/projectIdentity.mjs'

// UNMEASURED runtime budgets. Freeze only after a managed stack measurement.
export const PROJECT_TEST_TIMEOUT_MS = 180_000
export const PROJECT_EXPECT_TIMEOUT_MS = 30_000
export const PROJECT_ACTION_TIMEOUT_MS = 30_000

export function projectConfig(env) {
  const metadata = metadataFromEnvironment(env)
  const baseURL = exactOrigin(env.LEAF_PROJECT_BASE_URL)
  if (baseURL !== metadata.web_origin) throw new Error('PP3B_ORIGIN')
  const outputDir = env.LEAF_PROJECT_OUTPUT_DIR
  const outputFile = env.LEAF_PROJECT_PROOF_OUTPUT
  if (!outputDir || !outputFile || !isAbsolute(outputDir) || !isAbsolute(outputFile) ||
      resolve(outputDir) === resolve(outputFile)) throw new Error('PP3B_OUTPUT')
  return defineConfig({
    testDir: './e2e',
    projects: [{ name: 'project', testMatch: '**/project/project-authority.spec.mjs' }],
    workers: 1, retries: 0, forbidOnly: true, fullyParallel: false,
    shard: null, passWithNoTests: false, quiet: true,
    timeout: PROJECT_TEST_TIMEOUT_MS,
    expect: { timeout: PROJECT_EXPECT_TIMEOUT_MS },
    outputDir, preserveOutput: 'never',
    // The first reporter also scrubs runner failures before list's final summary.
    reporter: [['./e2e/project/projectProofReporter.mjs', { metadata, outputFile }], ['list']],
    use: { baseURL, browserName: 'chromium', headless: true,
      actionTimeout: PROJECT_ACTION_TIMEOUT_MS,
      screenshot: 'off', video: 'off', trace: 'off' },
  })
}

export default projectConfig(process.env)
