import { defineConfig } from '@playwright/test'

export default defineConfig({
  testDir: './e2e',
  outputDir: '../artifacts/cat-operator-proof/test-results',
  reporter: [['list'], ['html', {
    open: 'never',
    outputFolder: '../artifacts/cat-operator-proof/report',
  }]],
  use: {
    baseURL: 'http://127.0.0.1:5185',
    browserName: 'chromium',
    headless: true,
    screenshot: 'only-on-failure',
    trace: 'retain-on-failure',
    viewport: { width: 1600, height: 1000 },
  },
  webServer: {
    command: 'npm run dev -- --host 127.0.0.1 --port 5185',
    url: 'http://127.0.0.1:5185/app',
    reuseExistingServer: false,
    timeout: 120_000,
    env: {
      VITE_MOCK: '0',
      VITE_API_BASE: 'http://leaf-proof.invalid',
      VITE_TENANT_ID: 'cat-litmus-tenant',
    },
  },
})
