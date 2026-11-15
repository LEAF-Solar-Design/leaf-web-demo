import { defineConfig } from '@playwright/test'
import { gateBrowserResources } from './scripts/gate-browser-resources.mjs'

// iOS ship-surface browser proof (e2e/ios-ship-surface.spec.mjs).
//
// VITE_IOS_SURFACE is folded at build time (src/ios/flag.js), so no response
// mock can turn the surface on or off. Each flag state gets its own local Next
// instance and its own project; a project's grep keeps every test on the
// server whose flag it asserts. Suite isolation assigns a distinct pair within
// each worker block; manual runs retain the legacy flag-on offset of 50.
const resources = gateBrowserResources(process.env, 'ios')
const [offPort, onPort] = resources.ports
const origin = (port) => `http://127.0.0.1:${port}`

const server = (port, iosSurface, instance) => ({
  command: `npm run dev -- --hostname 127.0.0.1 --port ${port}`,
  url: `${origin(port)}/app`,
  reuseExistingServer: false,
  timeout: 120_000,
  env: {
    LEAF_NEXT_DEV_INSTANCE: instance,
    VITE_MOCK: '0',
    VITE_CAT_PROOF: '1',
    VITE_API_BASE: 'http://leaf-proof.invalid',
    VITE_TENANT_ID: 'cat-litmus-tenant',
    VITE_IOS_SURFACE: iosSurface,
  },
})

export default defineConfig({
  testDir: './e2e',
  testMatch: 'ios-ship-surface.spec.mjs',
  workers: 1,
  timeout: 90_000,
  outputDir: `${resources.reportRoot}/test-results`,
  reporter: [['list'], ['html', {
    open: 'never',
    outputFolder: `${resources.reportRoot}/report`,
  }]],
  use: {
    browserName: 'chromium',
    headless: true,
    video: 'retain-on-failure',
    screenshot: 'only-on-failure',
    trace: 'retain-on-failure',
    viewport: { width: 1600, height: 1000 },
  },
  projects: [
    {
      name: 'ios-flag-off',
      grep: /@flag-off/,
      metadata: { iosSurface: false },
      use: { baseURL: origin(offPort) },
    },
    {
      name: 'ios-flag-on',
      grep: /@flag-on/,
      metadata: { iosSurface: true },
      use: { baseURL: origin(onPort) },
    },
  ],
  webServer: [server(offPort, '0', resources.instances[0]), server(onPort, '1', resources.instances[1])],
})
