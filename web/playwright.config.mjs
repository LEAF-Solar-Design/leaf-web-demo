import { defineConfig } from '@playwright/test'

const worker = process.env.LEAF_NATIVE_GATE_WORKER ?? '0'
if (!/^[0-7]$/.test(worker)) throw new Error('Invalid native gate worker')
const port = 5185 + 100 * Number(worker)
const baseURL = `http://127.0.0.1:${port}`

export default defineConfig({
  testDir: './e2e',
  workers: 1,
  outputDir: '../artifacts/cat-operator-proof/test-results',
  reporter: [['list'], ['html', {
    open: 'never',
    outputFolder: '../artifacts/cat-operator-proof/report',
  }]],
  use: {
    baseURL,
    browserName: 'chromium',
    headless: true,
    video: 'on',
    screenshot: 'only-on-failure',
    trace: 'retain-on-failure',
    viewport: { width: 1600, height: 1000 },
  },
  webServer: {
    command: `npm run dev -- --host 127.0.0.1 --port ${port} --strictPort`,
    url: `${baseURL}/app`,
    reuseExistingServer: false,
    timeout: 120_000,
    env: {
      VITE_MOCK: '0',
      VITE_CAT_PROOF: '1',
      VITE_API_BASE: 'http://leaf-proof.invalid',
      VITE_TENANT_ID: 'cat-litmus-tenant',
    },
  },
})
