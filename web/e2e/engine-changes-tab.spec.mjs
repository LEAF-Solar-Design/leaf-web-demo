import { expect, test } from '@playwright/test'
import { mkdir } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { REQUEST, catProofResponse, makeCatProofState } from './catProofFixture.mjs'

const evidenceDir = resolve(dirname(fileURLToPath(import.meta.url)), '../artifacts/engine-changes-tab')
const intersects = (a, b) => a.x < b.x + b.width && a.x + a.width > b.x
  && a.y < b.y + b.height && a.y + a.height > b.y
async function expectInside(inner, outer) {
  const box = await inner.boundingBox()
  expect(box).not.toBeNull()
  expect(box.x).toBeGreaterThanOrEqual(outer.x)
  expect(box.y).toBeGreaterThanOrEqual(outer.y)
  expect(box.x + box.width).toBeLessThanOrEqual(outer.x + outer.width + 1)
  expect(box.y + box.height).toBeLessThanOrEqual(outer.y + outer.height + 1)
}
async function expectClearMetadata(page, rows) {
  const overview = await page.locator('.cad-overview').boundingBox()
  expect(overview).not.toBeNull()
  for (const chip of await rows.locator('.engine-changes-state').all()) {
    const box = await chip.boundingBox()
    expect(box).not.toBeNull()
    expect(intersects(box, overview)).toBe(false)
  }
}
const makeCard = (card_id, title, created_at) => ({
  card_id, title, created_at, updated_at: created_at, state: 'accepted', feature_id: 'panel-selection', unread: true,
  summary: 'Panel selection lost keyboard focus after refresh. The accepted fix restores focus to the selected row.',
  change: { pr_number: 123, pr_url: 'https://github.com/LEAF-Solar-Design/leaf-web-demo/pull/123', head_sha: 'abcdef1234567890', files: ['web/src/selection.js'], diff_stat: { additions: 4, deletions: 2 } },
  evidence: { before_ref: '/artifacts/before.png', after_ref: '/artifacts/after.png', regression_spec: 'web/e2e/selection.spec.mjs', receipt_ids: ['regression-1'] },
  acceptance: { verdict: 'accepted', acceptor: 'Fable', accepted_at: created_at },
  deployment_identity: { release: 'studio-proof' }, hold_requested_at: null, hold_requested_by: null,
})

async function mockStudio(page, { forbidden = false } = {}) {
  const proof = makeCatProofState()
  const cards = [makeCard('change-1', 'Restore panel selection focus', '2026-10-01T12:00:00Z'),
    makeCard('change-2', 'Repair panel export', '2026-09-30T12:00:00Z')]
  const requests = { list: 0, read: [], holds: [], turns: [], turnBodies: [], sessions: 0 }
  await page.route('http://leaf-proof.invalid/api/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const method = request.method()
    const body = request.postData() ? request.postDataJSON() : {}
    let result
    if (method === 'GET' && url.pathname === '/api/engine-changes') {
      requests.list += 1
      result = forbidden ? { status: 403, body: {} } : { status: 200, body: { cards, unread_count: cards.filter((card) => card.unread).length, next_cursor: null } }
    } else if (url.pathname.startsWith('/api/engine-changes/') && method !== 'OPTIONS') {
      const [, id, action] = url.pathname.match(/^\/api\/engine-changes\/([^/]+)(?:\/([^/]+))?$/) || []
      const card = cards.find((item) => item.card_id === id)
      if (!card) result = { status: 404, body: {} }
      else {
        if (method === 'POST' && action === 'read') { card.unread = false; requests.read.push(id) }
        if (method === 'POST' && action === 'hold-request') {
          requests.holds.push(id)
          card.hold_requested_at ||= '2026-10-01T13:00:00Z'
          card.hold_requested_by ||= 'Proof admin'
        }
        result = { status: 200, body: card }
      }
    } else if (method === 'POST' && url.pathname === '/api/sessions') {
      requests.sessions += 1
      result = catProofResponse({ method, path: url.pathname, body }, proof)
    } else if (method === 'POST' && url.pathname === '/api/sessions/cat-session/messages' && body.text) {
      const initializing = body.text === REQUEST
      if (!initializing) { requests.turns.push(body.text); requests.turnBodies.push(body) }
      const turnId = initializing ? 'setup-turn' : `discussion-${requests.turns.length}`
      const event = (type, data, seq) => ({ v: 1, session_id: 'cat-session', turn_id: turnId, seq, type, data })
      proof.events.push(event('turn_started', {}, proof.events.length + 1),
        event('text_delta', { text: initializing ? 'The existing assistant session is ready.' : 'Let’s review the accepted engine change.' }, proof.events.length + 2),
        event('turn_complete', { stop_reason: 'end_turn' }, proof.events.length + 3))
      result = { status: 202, body: { turn_id: turnId, status: 'started' } }
    } else {
      result = catProofResponse({ method, path: url.pathname, body, query: Object.fromEntries(url.searchParams) }, proof)
    }
    await route.fulfill({
      status: result.status, contentType: result.body == null ? undefined : 'application/json',
      body: result.body == null ? '' : JSON.stringify(result.body),
      headers: { 'access-control-allow-origin': '*', 'access-control-allow-headers': '*' },
    })
  })
  await page.goto('/app')
  await expect(page.locator('.viewer-title')).toContainText('cat.dwg', { timeout: 45_000 })
  // Like cat-operator-proof.spec.mjs, dispatch through the command bar to
  // create the session and open agent mode. Discuss must then post into that
  // mounted assistant without creating another session.
  await page.getByLabel('Command bar', { exact: true }).fill(REQUEST)
  await page.getByRole('button', { name: 'Run', exact: true }).click()
  await expect(page.locator('.converse-card').getByRole('log')).toContainText('The existing assistant session is ready.')
  await expect(page.locator('.converse-card')).toBeVisible()
  return { requests, cards, panel: page.locator('.converse-card') }
}

test('an admin reads, discusses, and requests a hold on an engine change in the existing assistant', async ({ page }) => {
  const { requests, cards, panel } = await mockStudio(page)
  await panel.getByRole('textbox', { name: 'Reply to the assistant' }).evaluate((element) => {
    const clipboardData = new DataTransfer()
    clipboardData.items.add(new File([Uint8Array.from(atob('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aWuoAAAAASUVORK5CYII='), (char) => char.charCodeAt(0))], 'pending.png', { type: 'image/png' }))
    element.dispatchEvent(new ClipboardEvent('paste', { clipboardData, bubbles: true, cancelable: true }))
  })
  await expect(panel.getByRole('button', { name: 'Remove image attachment' })).toBeVisible()
  const tab = panel.getByRole('tab', { name: 'Engine changes, 2 unread' })
  await expect(tab).toBeVisible()
  await expect(tab.locator('.engine-changes-badge')).toHaveText('2')
  await tab.click()
  const rows = panel.locator('.engine-changes-row')
  await expect(rows).toHaveCount(2)
  await expect(rows.first()).toContainText(cards[0].title)
  await expectClearMetadata(page, rows)
  await mkdir(evidenceDir, { recursive: true })
  await page.screenshot({ path: resolve(evidenceDir, 'list.png') })

  await rows.first().click()
  await expect(panel.getByRole('heading', { name: 'What changed' })).toBeVisible()
  await expect(panel.getByRole('tab', { name: 'Engine changes, 1 unread' })).toBeVisible()
  await expect.poll(() => requests.read).toEqual(['change-1'])
  await expect(panel.getByText(cards[0].summary)).toBeVisible()
  const discuss = panel.getByRole('button', { name: 'Discuss', exact: true })
  await expectInside(discuss, await panel.boundingBox())
  await expectInside(discuss, { x: 0, y: 0, ...page.viewportSize() })
  const lastField = panel.locator('.engine-changes-facts dd').last()
  await lastField.scrollIntoViewIfNeeded()
  await expect(lastField).toHaveText('release: studio-proof')
  await expectInside(lastField, await panel.locator('.engine-changes-body').boundingBox())
  await expectInside(lastField, await panel.boundingBox())
  await panel.getByRole('button', { name: 'Back', exact: true }).scrollIntoViewIfNeeded()
  await page.screenshot({ path: resolve(evidenceDir, 'detail.png') })

  await panel.getByRole('button', { name: 'Discuss', exact: true }).click()
  await expect(panel.getByRole('tab', { name: 'Conversation', exact: true })).toHaveAttribute('aria-selected', 'true')
  await expect.poll(() => requests.turns.length).toBe(1)
  expect(requests.turns[0]).toContain(cards[0].title)
  expect(requests.turns[0]).toContain(cards[0].evidence.regression_spec)
  expect(requests.turnBodies[0]).not.toHaveProperty('images')
  await expect(panel.getByRole('button', { name: 'Remove image attachment' })).toBeVisible()
  await expect(panel.getByAltText('Pending image attachment')).toBeVisible()
  await expect(panel.getByAltText('User attached image')).toHaveCount(0)
  expect(requests.sessions).toBe(1)
  await expect(panel.getByRole('log')).toContainText(cards[0].title)

  await expect(panel.getByRole('log')).toContainText('Let’s review the accepted engine change.')
  await expect(panel.getByRole('button', { name: 'Send', exact: true })).toBeEnabled()
  // Discuss focuses Conversation. Use the tablist's keyboard navigation once
  // the streamed reply has settled, then verify activation before the hold.
  await panel.getByRole('tab', { name: 'Conversation', exact: true }).press('ArrowRight')
  await expect(panel.getByRole('tab', { name: 'Engine changes, 1 unread' })).toHaveAttribute('aria-selected', 'true')
  await expect(panel.getByRole('tabpanel', { name: 'Engine changes, 1 unread' })).toBeVisible()
  await panel.getByRole('button', { name: 'Request hold', exact: true }).click()
  await expect(panel.getByRole('group', { name: 'Confirm hold request' })).toBeVisible()
  expect(requests.holds).toHaveLength(0)
  await panel.getByRole('button', { name: 'Confirm request', exact: true }).click()
  await expect(panel.getByText(/Hold requested by Proof admin at/)).toBeVisible()
  expect(requests.holds).toEqual(['change-1'])
  await panel.getByRole('button', { name: 'Back', exact: true }).click()
  await expect(panel.getByRole('button', { name: /Restore panel selection focus/ })).toBeFocused()
  await rows.first().press('Enter')
  await expect(panel.getByText(/Hold requested by Proof admin at/)).toBeVisible()
  expect(requests.read).toHaveLength(1)
  expect(requests.holds).toHaveLength(1)
  await page.setViewportSize({ width: 390, height: 844 })
  await expect(panel.getByRole('heading', { name: cards[0].title })).toBeVisible()
  expect(await panel.locator('.engine-changes-body').evaluate((element) => element.scrollWidth <= element.clientWidth)).toBe(true)
  await panel.getByRole('button', { name: 'Back', exact: true }).press('Escape')
  await expect(rows.first()).toBeFocused()
  await expectClearMetadata(page, rows)
  await rows.first().click()
  await expectInside(discuss, await panel.boundingBox())
  await expectInside(discuss, { x: 0, y: 0, ...page.viewportSize() })
  await lastField.scrollIntoViewIfNeeded()
  await expectInside(lastField, await panel.locator('.engine-changes-body').boundingBox())
  await expectInside(lastField, await panel.boundingBox())
  // Refresh both evidence images after the complete walk at the desktop size.
  await page.setViewportSize({ width: 1600, height: 1000 })
  await panel.getByRole('button', { name: 'Back', exact: true }).click()
  await expectClearMetadata(page, rows)
  await page.screenshot({ path: resolve(evidenceDir, 'list.png') })
  await rows.first().click()
  await expect(discuss).toBeEnabled()
  await page.screenshot({ path: resolve(evidenceDir, 'detail.png') })
})

test('a forbidden list leaves the existing assistant conversation without a tablist', async ({ page }) => {
  const { requests, panel } = await mockStudio(page, { forbidden: true })
  await expect.poll(() => requests.list).toBeGreaterThan(0)
  await expect(panel.getByRole('textbox', { name: 'Reply to the assistant' })).toBeVisible()
  await expect(panel.getByRole('tablist')).toHaveCount(0)
  await expect(panel.getByRole('tab', { name: /Engine changes/ })).toHaveCount(0)
  await expect(panel.getByRole('log')).toBeVisible()
})
