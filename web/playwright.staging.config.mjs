import { defineConfig } from '@playwright/test'
import { resolveStagingBaseURL, stagingProofPath } from './e2e/staging/stagingConfig.mjs'

export default defineConfig({
  testDir: './e2e/staging',
  // Runs before any spec: refuses a non-allowlisted host and clears this
  // run's prior receipts/screenshots/report so stale evidence can't survive.
  globalSetup: './e2e/staging/globalSetup.mjs',
  outputDir: stagingProofPath('test-results'),
  reporter: [['list'], ['html', {
    open: 'never',
    outputFolder: stagingProofPath('report'),
  }]],
  use: {
    // Deliberately LEAF_E2E_STAGING_BASE_URL, not LEAF_E2E_PROD_BASE_URL --
    // see e2e/staging/stagingConfig.mjs for why reusing the prod variable is
    // unsafe here.
    baseURL: resolveStagingBaseURL(),
    browserName: 'chromium',
    headless: true,
    video: 'on',
    screenshot: 'only-on-failure',
    trace: 'retain-on-failure',
    viewport: { width: 1600, height: 1000 },
  },
})
