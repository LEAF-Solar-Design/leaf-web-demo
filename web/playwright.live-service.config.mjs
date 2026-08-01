import { defineConfig } from '@playwright/test'

export default defineConfig({
  testDir: './e2e',
  testMatch: 'live-service-surface.spec.mjs',
  workers: 1,
  outputDir: '../artifacts/live-service-surface/test-results',
  reporter: [['list'], ['html', {
    open: 'never',
    outputFolder: '../artifacts/live-service-surface/report',
  }]],
  use: {
    baseURL: 'http://127.0.0.1:5187',
    browserName: 'chromium',
    headless: true,
    video: 'on',
    screenshot: 'only-on-failure',
    trace: 'retain-on-failure',
    viewport: { width: 1600, height: 1000 },
  },
  webServer: {
    command: 'npm run dev -- --host 127.0.0.1 --port 5187',
    url: 'http://127.0.0.1:5187/try',
    reuseExistingServer: false,
    timeout: 120_000,
    env: {
      VITE_MOCK: '0',
      VITE_CAT_PROOF: '0',
      VITE_API_BASE: 'http://leaf-proof.invalid',
      VITE_TENANT_ID: 'cat-litmus-tenant',
    },
  },
})
