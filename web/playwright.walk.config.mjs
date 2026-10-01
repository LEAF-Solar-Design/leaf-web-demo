import { defineConfig } from '@playwright/test'
import { defaultAdmission } from './walk/stack.mjs'

// Collection never boots a stack. Admission is still required before choosing
// workers, and startStack rechecks it immediately before each worker launch.
const admission = await defaultAdmission()
const status = String(admission?.status || admission?.decision || '').toLowerCase()
const cap = (value, label) => {
  if (value === undefined || value === '') return 2
  const number = Number(value)
  if (!Number.isInteger(number) || number < 0) throw new Error(`${label} must be a nonnegative integer`)
  return Math.min(2, number)
}
const slots = Math.min(cap(admission?.slots, 'admission slots'), cap(process.env.LEAF_WALK_SLOTS, 'LEAF_WALK_SLOTS'))
if (status === 'queued' || slots === 0) {
  process.stderr.write(`QUEUED: ${admission?.reason || 'Zero local walk slots available'}\n`)
  process.exit(75)
}
if (status !== 'admitted') throw new Error(`STOPPED: ${admission?.reason || 'Walk admission did not explicitly admit execution'}`)

export default defineConfig({
  testDir: './e2e/walk',
  testMatch: '**/*.spec.mjs',
  fullyParallel: true,
  workers: slots,
  retries: 0,
  forbidOnly: true,
  timeout: 120_000,
  expect: { timeout: 15_000 },
  globalSetup: './e2e/walk/fixtures.mjs',
  metadata: { walk: { admittedSlots: slots, admission } },
  outputDir: './artifacts/walk/test-results',
  reporter: [
    ['list'],
    ['./e2e/walk/fixtures.mjs'],
    ['json', { outputFile: './artifacts/walk/results.json' }],
  ],
  use: {
    browserName: 'chromium',
    headless: true,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  projects: [
    { name: 'desktop', grep: /@desktop(?:\s|$)/, use: { viewport: { width: 1600, height: 1000 } } },
    // Matches e2e/local/cockpit-viewports.spec.mjs's portrait phone.
    { name: 'phone', grep: /@phone(?:\s|$)/, use: { viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true } },
  ],
})
