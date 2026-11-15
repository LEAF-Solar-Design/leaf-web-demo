import { defineConfig } from '@playwright/test'
import { gateBrowserResources } from './scripts/gate-browser-resources.mjs'

const resources = gateBrowserResources()
const [port] = resources.ports
const baseURL = `http://127.0.0.1:${port}`

export default defineConfig({
  testDir: './e2e',
  testIgnore: ['**/project/**'],
  workers: 1,
  outputDir: `${resources.reportRoot}/test-results`,
  reporter: [['list'], ['html', {
    open: 'never',
    outputFolder: `${resources.reportRoot}/report`,
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
    command: `npm run dev -- --hostname 127.0.0.1 --port ${port}`,
    url: `${baseURL}/app`,
    reuseExistingServer: false,
    timeout: 120_000,
    env: {
      ...(resources.isolated ? { LEAF_NEXT_DEV_INSTANCE: resources.instances[0] } : {}),
      VITE_MOCK: '0',
      VITE_CAT_PROOF: '1',
      VITE_API_BASE: 'http://leaf-proof.invalid',
      VITE_INTEGRATIONS_API_ORIGIN: 'https://ops-proof.invalid',
      VITE_TENANT_ID: 'cat-litmus-tenant',
    },
  },
})
