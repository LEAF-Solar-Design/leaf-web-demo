import { defineConfig } from '@playwright/test'
import { validateProdConfig } from './e2e/prod/prodConfig.mjs'

const config = validateProdConfig()

export default defineConfig({
  testDir: './e2e/prod',
  testMatch: /(open-project|unified)-prod-readonly\.spec\.mjs$/,
  outputDir: '../artifacts/unified-surface-proof/prod/test-results',
  reporter: [['./e2e/prod/prodReporter.mjs']],
  retries: 0,
  repeatEach: 1,
  workers: 1,
  projects: [{ name: 'chromium', use: { browserName: 'chromium' } }],
  use: {
    baseURL: config.enabled ? config.origin : undefined,
    browserName: 'chromium',
    headless: true,
    video: 'off',
    screenshot: 'off',
    trace: 'off',
    serviceWorkers: 'block',
    viewport: { width: 1600, height: 1000 },
  },
})
