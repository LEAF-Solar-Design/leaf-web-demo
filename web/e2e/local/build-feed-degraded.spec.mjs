import { expect, test } from '@playwright/test'
import { requireLocalReady } from './requireReady.mjs'
import { setRail } from './railFlag.mjs'

// W6-E01: the job monitor says when its builds feed is stale or paused by
// sign-in, offers Retry or Resume, counts unreadable records, and never
// discards the last good cards.
//
// GET /api/builds is intercepted with ONE route whose answer this row switches
// between: a valid fleet-lane record (the wire shape lib/buildQueue.js
// parseBuildRecord accepts, the same shape the managed stack's route returns),
// a 503, a 401, and the valid record beside one malformed record. The console
// polls every 5000 ms; every transition is bounded at 20 s.
//
// Every row calls requireLocalReady first: under the managed runner
// (LEAF_E2E_MANAGED=1) a dead stack HARD-FAILS instead of skipping.
const API_BASE = process.env.LEAF_E2E_API_BASE || 'http://127.0.0.1:8230'
const TRANSITION_MS = 20_000
const TITLE = 'E01 build feed probe'

const VALID_RECORD = Object.freeze({
  id: 'e01-feed-probe',
  lane: 'fleet',
  state: 'queued',
  title: TITLE,
  requested_by: null,
  started: null,
  elapsed_ms: null,
  estimate_ms: null,
  cost_usd: null,
  receipts: [],
  terminal: { verified: false, promoted: false },
  actions: [],
  status: { word: 'queued', tint: 'mut', detail: null },
})
// Fails parseBuildRecord on its lane, so the hook drops and counts it.
const MALFORMED_RECORD = Object.freeze({ id: 'e01-feed-bad', lane: 'nowhere' })

const listBody = (builds) => JSON.stringify({ builds, warnings: [], sources: {} })

test('E01 build feed: a failed refresh keeps the cards, says so, and recovers', async ({ page, request }) => {
  test.setTimeout(180_000)
  await requireLocalReady(request, test, API_BASE)

  let mode = 'ok'
  await page.route('**/api/builds*', (route) => {
    if (mode === '503') {
      return route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ error: { message: 'unavailable' } }) })
    }
    if (mode === '401') {
      return route.fulfill({ status: 401, contentType: 'application/json', body: JSON.stringify({ error: { message: 'sign in required' } }) })
    }
    const builds = mode === 'malformed' ? [VALID_RECORD, MALFORMED_RECORD] : [VALID_RECORD]
    return route.fulfill({ status: 200, contentType: 'application/json', body: listBody(builds) })
  })

  await setRail(page, '1')
  await page.goto('/app?surface=cad')
  await expect(page.locator('.app[data-surface="cad"]')).toHaveCount(1, { timeout: 30_000 })

  // CAD is a job-spine surface: the queued record lights the running-count
  // badge, and one click on it expands the rail where the feed notes live.
  const badge = page.getByTestId('builds-badge')
  await expect(badge).toBeVisible({ timeout: TRANSITION_MS })
  await badge.click()
  await expect(page.locator('aside.rail[data-spine]')).toHaveCount(0)
  const card = page.locator('.bq-card[data-lane="fleet"]').filter({ hasText: TITLE })
  await expect(card).toBeVisible({ timeout: TRANSITION_MS })
  await expect(page.locator('[data-feed]')).toHaveCount(0)
  await expect(page.locator('[data-feed-dropped]')).toHaveCount(0)

  // A failed refresh: the card stays and the rail says the list may be stale.
  mode = '503'
  const stale = page.locator('[data-feed="stale"]')
  await expect(stale).toBeVisible({ timeout: TRANSITION_MS })
  await expect(stale).toContainText('Build list may be out of date: the last refresh failed.')
  await expect(card).toBeVisible()

  // Retry against a healthy route clears the note.
  mode = 'ok'
  await stale.getByRole('button', { name: 'Retry' }).click()
  await expect(page.locator('[data-feed]')).toHaveCount(0, { timeout: TRANSITION_MS })
  await expect(card).toBeVisible()

  // A 401 pauses the feed until the drafter resumes it.
  mode = '401'
  const auth = page.locator('[data-feed="auth"]')
  await expect(auth).toBeVisible({ timeout: TRANSITION_MS })
  await expect(auth).toContainText('Build updates are paused until you sign in again.')
  await expect(card).toBeVisible()

  mode = 'ok'
  await auth.getByRole('button', { name: 'Resume' }).click()
  await expect(page.locator('[data-feed]')).toHaveCount(0, { timeout: TRANSITION_MS })
  await expect(card).toBeVisible()

  // One unreadable record beside the valid one is counted, never rendered.
  mode = 'malformed'
  const dropped = page.locator('[data-feed-dropped="1"]')
  await expect(dropped).toBeVisible({ timeout: TRANSITION_MS })
  await expect(dropped).toHaveText('1 build record could not be read and is not shown.')
  await expect(card).toBeVisible()
  await expect(page.locator('.bq-card[data-lane="fleet"]')).toHaveCount(1)
})
