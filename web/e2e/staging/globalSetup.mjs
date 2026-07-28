import { rmSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { assertAllowedStagingHost, resolveStagingBaseURL } from './stagingConfig.mjs'

const HERE = dirname(fileURLToPath(import.meta.url))
// web/e2e/staging/globalSetup.mjs -> staging/e2e/web up three times to the repo
// root, then into artifacts/. This MUST match the PROOF_DIR convention every
// staging spec uses (join(process.cwd(), '..', 'artifacts', ...), where
// process.cwd() is web/ because `npm run proof:staging` runs from web/) --
// getting this wrong means globalSetup silently clears an empty directory
// while the real, stale receipts survive untouched.
const STAGING_OUTPUT_ROOT = resolve(HERE, '..', '..', '..', 'artifacts', 'unified-surface-proof', 'staging')

// Runs once before any staging spec. Two jobs, both mandatory:
// 1. Refuse to proceed unless the resolved base URL is an allowed staging
//    host. A stray production base URL must never produce receipts that
//    claim evidence_tier "staging".
// 2. Clear this run's receipt/screenshot/report output so a stale receipt
//    from a prior run (e.g. an authenticated row that this run skips) can
//    never be mistaken for current evidence.
export default async function globalSetup() {
  const baseURL = resolveStagingBaseURL()
  assertAllowedStagingHost(baseURL)
  rmSync(STAGING_OUTPUT_ROOT, { recursive: true, force: true })
}
