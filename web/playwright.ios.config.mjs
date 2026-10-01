import { defineConfig } from '@playwright/test'

// iOS ship-surface browser proof (e2e/ios-ship-surface.spec.mjs).
//
// VITE_IOS_SURFACE is folded at build time (src/ios/flag.js), so no response
// mock can turn the surface on or off. Each flag state gets its own local Vite
// instance and its own project; a project's grep keeps every test on the
// server whose flag it asserts. Ports derive from LEAF_NATIVE_GATE_WORKER like
// playwright.config.mjs, and the flag-on server sits 50 above so parallel
// gate workers (100 apart) never collide.
const worker = process.env.LEAF_NATIVE_GATE_WORKER ?? '0'
if (!/^[0-7]$/.test(worker)) throw new Error('Invalid native gate worker')
const offPort = 5185 + 100 * Number(worker)
const onPort = offPort + 50
const origin = (port) => `http://127.0.0.1:${port}`

const server = (port, iosSurface) => ({
  command: `npm run dev -- --host 127.0.0.1 --port ${port} --strictPort`,
  url: `${origin(port)}/app`,
  reuseExistingServer: false,
  timeout: 120_000,
  env: {
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
  outputDir: '../artifacts/ios-ship-surface/test-results',
  reporter: [['list'], ['html', {
    open: 'never',
    outputFolder: '../artifacts/ios-ship-surface/report',
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
  webServer: [server(offPort, '0'), server(onPort, '1')],
})
