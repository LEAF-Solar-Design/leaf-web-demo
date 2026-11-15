import { defineConfig } from '@playwright/test'
import { gateBrowserResources } from './scripts/gate-browser-resources.mjs'

// Browser proof for the mounted project lifecycle panel (ToolCast.jsx mounts
// ProjectLifecyclePanel behind ENV_LIFECYCLE_UI). The dev server is built with
// VITE_LIFECYCLE_UI=1, the flag deploy/Dockerfile.web ships on, and every API
// call is answered by page.route inside the spec, so nothing leaves 127.0.0.1.
const resources = gateBrowserResources(process.env, 'lifecycle')
const [port] = resources.ports
const baseURL = `http://127.0.0.1:${port}`

export default defineConfig({
  testDir: './e2e',
  testMatch: 'project-lifecycle-panel.spec.mjs',
  workers: 1,
  timeout: 180_000,
  outputDir: `${resources.reportRoot}/test-results`,
  reporter: [['list'], ['html', {
    open: 'never',
    outputFolder: `${resources.reportRoot}/report`,
  }]],
  use: {
    baseURL,
    browserName: 'chromium',
    headless: true,
    acceptDownloads: true,
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
      ...(resources.isolated ? { LEAF_NEXT_DEV_INSTANCE: resources.instances[0] } : {}),
      VITE_MOCK: '0',
      VITE_CAT_PROOF: '1',
      VITE_LIFECYCLE_UI: '1',
      VITE_API_BASE: 'http://leaf-proof.invalid',
      VITE_TENANT_ID: 'cat-litmus-tenant',
    },
  },
})
