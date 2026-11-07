import { defineConfig } from '@playwright/test'
import { fileURLToPath } from 'node:url'

export default defineConfig({
  testDir: './e2e/signed-in',
  testMatch: '**/*.spec.mjs',
  workers: 1,
  fullyParallel: false,
  forbidOnly: true,
  retries: 0,
  timeout: 120_000,
  globalTimeout: 900_000,
  globalSetup: './e2e/signed-in/globalSetup.mjs',
  outputDir: './artifacts/signed-in/test-results',
  reporter: [[fileURLToPath(new URL('./e2e/regressions/expectRed.mjs', import.meta.url))]],
  use: {
    browserName: 'chromium',
    headless: true,
    viewport: { width: 1600, height: 1000 },
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
})
