import { defineConfig } from '@playwright/test'

export default defineConfig({
  testDir: './e2e/staging',
  outputDir: '../artifacts/unified-surface-proof/staging/test-results',
  reporter: [['list'], ['html', {
    open: 'never',
    outputFolder: '../artifacts/unified-surface-proof/staging/report',
  }]],
  use: {
    baseURL: process.env.LEAF_E2E_PROD_BASE_URL || 'https://platform-staging.leafdesign.ai',
    browserName: 'chromium',
    headless: true,
    video: 'on',
    screenshot: 'only-on-failure',
    trace: 'retain-on-failure',
    viewport: { width: 1600, height: 1000 },
  },
})
