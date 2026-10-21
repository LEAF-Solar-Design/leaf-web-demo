import { defineConfig } from '@playwright/test'
import { assertAllowedStagingHost, resolveStagingBaseURL } from './e2e/staging/stagingConfig.mjs'

const baseURL = resolveStagingBaseURL()
assertAllowedStagingHost(baseURL)

export default defineConfig({
  testDir: './e2e/walk-staging',
  outputDir: './artifacts/walk-staging/test-results',
  reporter: [['list'], ['json', { outputFile: './artifacts/walk-staging/results.json' }]],
  workers: 1,
  retries: 0,
  metadata: { stagingWalk: true },
  timeout: 180_000,
  use: {
    baseURL,
    browserName: 'chromium',
    headless: true,
    viewport: { width: 1600, height: 1000 },
    trace: 'off',
    video: 'off',
    screenshot: 'only-on-failure',
  },
})
