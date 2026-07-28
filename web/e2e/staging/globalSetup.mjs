import { rmSync } from 'node:fs'
import { assertAllowedStagingHost, resolveStagingBaseURL, STAGING_OUTPUT_ROOT } from './stagingConfig.mjs'

// Runs once before any staging spec. Two jobs, both mandatory:
// 1. Refuse to proceed unless the resolved base URL is an allowed staging
//    host. A stray production base URL must never produce receipts that
//    claim evidence_tier "staging".
// 2. Clear this run's receipt/screenshot/report output so a stale receipt
//    from a prior run (e.g. an authenticated row that this run skips) can
//    never be mistaken for current evidence.
export default async function globalSetup() {
  rmSync(STAGING_OUTPUT_ROOT, { recursive: true, force: true })
  const baseURL = resolveStagingBaseURL()
  assertAllowedStagingHost(baseURL)
}
