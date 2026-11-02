import { defineConfig } from '@playwright/test'
import { randomUUID } from 'node:crypto'
import { resolve } from 'node:path'

const worker = process.env.LEAF_NATIVE_GATE_WORKER ?? '0'
if (!/^[0-7]$/.test(worker)) throw new Error('Invalid native gate worker')
const port = 5189 + 100 * Number(worker)
const apiPort = 8189 + 100 * Number(worker)
const baseURL = `http://127.0.0.1:${port}`
// Inherit the runner's per-attempt directory; direct invocations get their own UUID.
const directory = process.env.LEAF_PAGE_SMOKE_DIR ||= resolve(`../artifacts/page-service-smoke/w${worker}-${randomUUID()}`)
const quote = value => `'${value.replaceAll("'", "'\\''")}'`
const python = process.env.LEAF_PAGE_SMOKE_PYTHON || 'python3'

export default defineConfig({
  testDir: './e2e',
  testMatch: 'project-page.service.mjs',
  workers: 1,
  retries: 0,
  forbidOnly: true,
  timeout: 120_000,
  outputDir: `../artifacts/page-service-smoke/w${worker}/test-results`,
  reporter: [['list']],
  use: {
    baseURL, browserName: 'chromium', headless: true,
    viewport: { width: 1600, height: 1000 },
    video: 'off', screenshot: 'only-on-failure', trace: 'retain-on-failure',
    serviceWorkers: 'block',
  },
  webServer: [{
    command: `${quote(python)} ../scripts/page_editor_service_smoke.py --port ${apiPort} --ready-file ${quote(`${directory}/ready.json`)} --log-file ${quote(`${directory}/server.log`)}`,
    port: apiPort,
    timeout: 120_000,
    stdout: 'ignore', stderr: 'pipe', reuseExistingServer: false,
    gracefulShutdown: { signal: 'SIGTERM', timeout: 15_000 },
  }, {
    command: `npm run dev -- --hostname 127.0.0.1 --port ${port}`,
    url: `${baseURL}/app`,
    timeout: 120_000,
    stdout: 'ignore', stderr: 'pipe', reuseExistingServer: false,
    gracefulShutdown: { signal: 'SIGTERM', timeout: 15_000 },
    env: {
      VITE_MOCK: '0', VITE_CAT_PROOF: '1',
      VITE_API_BASE: 'http://leaf-proof.invalid',
      VITE_INTEGRATIONS_API_ORIGIN: 'https://ops-proof.invalid',
      VITE_TENANT_ID: 'cat-litmus-tenant',
    },
  }],
})
