import { defineConfig } from '@playwright/test'
import { dirname } from 'node:path'
import { fileURLToPath } from 'node:url'
import { exactSpecPattern } from './e2e/regressions/expectRed.mjs'

const requestedWorkers = Number(process.env.LEAF_REGRESSION_WORKERS || process.env.LEAF_WALK_SLOTS || 1)
if (!Number.isInteger(requestedWorkers) || requestedWorkers < 0) {
  throw new Error('LEAF_REGRESSION_WORKERS/LEAF_WALK_SLOTS must be a nonnegative integer')
}
const selectedSpec = process.env.LEAF_REGRESSION_SPEC

export default defineConfig({
  testDir: selectedSpec ? dirname(selectedSpec) : './e2e/regressions',
  testMatch: selectedSpec ? new RegExp(exactSpecPattern(selectedSpec)) : '**/*.spec.mjs',
  // startStack still consults admission for every launch and owns the leases.
  workers: Math.max(1, Math.min(2, requestedWorkers)),
  fullyParallel: false,
  forbidOnly: true,
  retries: 0,
  timeout: 60_000,
  globalTimeout: 600_000,
  outputDir: './artifacts/regressions/test-results',
  reporter: [[fileURLToPath(new URL('./e2e/regressions/expectRed.mjs', import.meta.url))]],
  use: {
    browserName: 'chromium',
    headless: true,
    viewport: { width: 1600, height: 1000 },
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
})
