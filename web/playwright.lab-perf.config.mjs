import { defineConfig } from '@playwright/test'
import { fileURLToPath } from 'node:url'

export default defineConfig({
  testDir: './e2e',
  testMatch: 'lab-canvas-perf.spec.mjs',
  timeout: 180000,
  workers: 1,
  retries: 0,
  projects: [{ name: 'chromium', use: { browserName: 'chromium' } }],
  use: {
    baseURL: 'http://127.0.0.1:5195',
    viewport: { width: 1920, height: 1080 },
    video: 'off',
    trace: 'off',
    screenshot: 'only-on-failure',
  },
  webServer: {
    command: 'npx vite build --config vite.lab-perf.config.mjs && npx vite preview --config vite.lab-perf.config.mjs --host 127.0.0.1 --port 5195 --strictPort',
    cwd: fileURLToPath(new URL('.', import.meta.url)),
    url: 'http://127.0.0.1:5195/e2e/lab-perf/harness.html',
    reuseExistingServer: false,
    timeout: 180000,
  },
})
