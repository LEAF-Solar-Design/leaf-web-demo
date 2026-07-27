import { defineConfig } from '@playwright/test'

export default defineConfig({
  testDir: './e2e/guest',
  workers: 1,
  timeout: 60_000,
  outputDir: '../artifacts/unified-surface-proof/guest/test-results',
  reporter: [['list'], ['html', {
    open: 'never',
    outputFolder: '../artifacts/unified-surface-proof/guest/report',
  }]],
  use: {
    baseURL: process.env.LEAF_E2E_BASE_URL || 'http://127.0.0.1:5275',
    browserName: 'chromium',
    headless: true,
    video: 'on',
    screenshot: 'only-on-failure',
    trace: 'retain-on-failure',
    viewport: { width: 1600, height: 1000 },
  },
})
