import { defineConfig } from '@playwright/test'

export default defineConfig({
  testDir: './e2e',
  testMatch: 'b01-narrow-preview.spec.mjs',
  workers: 1,
  outputDir: '../artifacts/b01-narrow-preview',
  reporter: 'list',
  use: { browserName: 'chromium', headless: true, screenshot: 'only-on-failure' },
})
