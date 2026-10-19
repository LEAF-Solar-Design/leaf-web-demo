import { defineConfig } from '@playwright/test'

export default defineConfig({
  testDir: '.',
  testMatch: '**/*.canary.mjs',
  outputDir: process.env.LEAF_PROD_CANARY_OUTPUT,
  reporter: [['../prod/prodReporter.mjs', { canary: true }]],
  retries: 0,
  repeatEach: 1,
  workers: 1,
  projects: [{ name: 'chromium', use: { browserName: 'chromium' } }],
  use: {
    headless: true, serviceWorkers: 'block', video: 'off', screenshot: 'off', trace: 'off',
    prodOffline: true,
    prodAuthenticated: true,
    prodConfig: Object.freeze({ enabled: true, required: true, origin: 'https://studio.leafdesign.ai', token: 'SYNTHETIC_CANARY_SECRET_42', expectedSha: 'a'.repeat(40), projectId: '11111111-1111-4111-8111-111111111111' }),
  },
})
