import { defineConfig } from '@playwright/test'

const PORT = process.env.LEAF_MOCK_PORT || '5711'

export default defineConfig({
  testDir: './e2e',
  workers: 1,
  timeout: 60_000,
  outputDir: '../artifacts/scratch-e2e-fix/test-results',
  reporter: [['list']],
  use: {
    baseURL: `http://127.0.0.1:${PORT}`,
    browserName: 'chromium',
    headless: true,
    video: 'off',
    screenshot: 'only-on-failure',
    trace: 'retain-on-failure',
    viewport: { width: 1600, height: 1000 },
  },
})
