import { defineConfig } from '@playwright/test'

export default defineConfig({
  testDir: './e2e/standard',
  testMatch: '*.spec.mjs',
  workers: 1,
  outputDir: '../artifacts/standard-sweep/test-results',
  reporter: [['list']],
  use: { browserName: 'chromium', headless: true },
})
