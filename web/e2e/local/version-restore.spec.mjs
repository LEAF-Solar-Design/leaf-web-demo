import { expect, test } from '@playwright/test'
import { REQUEST, catProofResponse, makeCatProofState } from '../catProofFixture.mjs'

function versionRows(head) {
  return [
    { v: 1, parent: null, tool: 'base', note: 'Original drawing', delta: null },
    { v: 2, parent: 1, tool: 'move-panel', note: 'Moved A', delta: { added: 0, modified: 1, deleted: 0 } },
    { v: 3, parent: 2, tool: 'delete-panel', note: 'Removed B', delta: { added: 0, modified: 0, deleted: 1 } },
    ...(head === 4 ? [{
      v: 4, parent: 3, tool: 'restore', note: 'restore of version 1',
      delta: { added: 1, modified: 1, deleted: 0 },
    }] : []),
  ]
}

async function mountVersionSurface(page, {
  restoredHeadReadable = true,
  stallHistoryAfterRestore = false,
  failFirstRepairRead = false,
  staleFirstHeadAfterRestore = false,
  splitReadFirstHeadAfterRestore = false,
  undoLandedOnSecondRead = false,
} = {}) {
  const proofState = makeCatProofState()
  let head = 3
  let restoreCount = 0
  let intakeReadsAfterRestore = 0
  let deltasRequested = false
  let releaseHistoryRefresh
  const historyRefreshGate = new Promise((resolve) => { releaseHistoryRefresh = resolve })

  await page.route('**/api/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const method = request.method()
    const path = url.pathname
    const body = request.postData() ? request.postDataJSON() : {}
    let result

    if (path === '/api/drawings/cat-panels/versions' && method === 'GET') {
      deltasRequested ||= url.searchParams.get('include_deltas') === '1'
      if (restoreCount && stallHistoryAfterRestore) await historyRefreshGate
      result = {
        status: 200,
        body: {
          drawing_id: 'cat-panels', head, latest: head, checkout: null,
          versions: versionRows(head),
        },
      }
    } else if (path === '/api/drawings/cat-panels/versions/1/restore' && method === 'POST') {
      restoreCount += 1
      head = 4
      result = {
        status: 200,
        body: {
          error: null, restored_from: 1, head: 4, latest: 4,
          restored_head_readable: restoredHeadReadable,
          new_version: { drawing_id: 'cat-panels', version: 4, parent: 3 },
        },
      }
    } else if (path === '/api/drawings/cat-panels/intake' && method === 'GET' && restoreCount) {
      intakeReadsAfterRestore += 1
      result = failFirstRepairRead && intakeReadsAfterRestore === 1
        ? { status: 503, body: { detail: 'intake cache is still unavailable' } }
        : (() => {
            if (staleFirstHeadAfterRestore && intakeReadsAfterRestore === 1) {
              // A truly STALE pre-restore response: generated before the
              // restore committed, so it reports the OLD latest too — a
              // pre-restore server cannot know about v4.
              return { status: 200, body: { drawing_id: 'cat-panels', intake: proofState.base, version: 3, head: 3, latest: 3 } }
            }
            if (splitReadFirstHeadAfterRestore && intakeReadsAfterRestore === 1) {
              // A SPLIT read: the intake was resolved at v3 but the manifest
              // reloaded after the restore committed — head/latest are new,
              // the seated geometry is not.
              return { status: 200, body: { drawing_id: 'cat-panels', intake: proofState.base, version: 3, head: 4, latest: 4 } }
            }
            if (undoLandedOnSecondRead && intakeReadsAfterRestore >= 2) {
              // An undo issued around the restore landed AFTER it: head is
              // legitimately BELOW the restored version, latest keeps it.
              return { status: 200, body: { drawing_id: 'cat-panels', intake: proofState.base, version: 3, head: 3, latest: 4 } }
            }
            return { status: 200, body: { drawing_id: 'cat-panels', intake: proofState.base, version: head, head, latest: head } }
          })()
    } else {
      result = catProofResponse({
        method, path, body, query: Object.fromEntries(url.searchParams),
      }, proofState)
    }

    await route.fulfill({
      status: result.status,
      contentType: result.body == null ? undefined : 'application/json',
      body: result.body == null ? '' : JSON.stringify(result.body),
      headers: {
        'access-control-allow-origin': '*',
        'access-control-allow-headers': '*',
      },
    })
  })

  await page.goto('/app?drawing=cat-panels')
  await expect(page.locator('.viewer-title')).toContainText('cat.dwg', { timeout: 15_000 })
  // Seat a real versioned drawing through App's production controller path.
  // The initial session intake alone is intentionally unversioned.
  await page.getByRole('combobox', { name: 'Command bar' }).fill(REQUEST)
  await page.getByRole('button', { name: 'Run' }).click()
  const approval = page.locator('.converse-confirm').filter({ hasText: 'arrange-panels-as-cat' })
  await approval.getByRole('button', { name: 'Approve' }).click()
  const attach = page.getByRole('button', { name: 'Attach' })
  await expect(attach).toBeVisible({ timeout: 15_000 })
  await attach.click()
  await expect(page.getByRole('button', { name: 'History' })).toBeVisible({ timeout: 15_000 })
  await page.getByRole('button', { name: 'History' }).click()
  await expect(page.getByRole('dialog', { name: 'Version history' })).toBeVisible()

  return {
    deltasRequested: () => deltasRequested,
    intakeReadsAfterRestore: () => intakeReadsAfterRestore,
    restoreCount: () => restoreCount,
    releaseHistoryRefresh,
  }
}

test('the /app history drawer shows deltas and restores an old version as a new head', async ({ page }) => {
  test.setTimeout(60_000)
  const observed = await mountVersionSurface(page)
  const history = page.getByRole('dialog', { name: 'Version history' })
  const v1 = history.getByTestId('vh-row-v1')

  expect(observed.deltasRequested()).toBe(true)
  await expect(v1.getByTestId('vh-delta')).toHaveCount(0)
  await expect(history.getByTestId('vh-row-v2').getByTestId('vh-delta')).toContainText('~1')
  await expect(history.getByTestId('vh-row-v3').getByTestId('vh-delta')).toContainText('-1')

  await v1.getByRole('button', { name: 'Restore', exact: true }).click()
  await v1.getByRole('button', { name: 'Restore v1' }).click()

  await expect(history).toBeHidden()
  await page.getByRole('button', { name: 'History' }).click()
  const reopened = page.getByRole('dialog', { name: 'Version history' })
  const v4 = reopened.getByTestId('vh-row-v4')
  await expect(v4).toBeVisible()
  await expect(v4).toContainText('head')
  await expect(reopened.getByTestId('vh-row-v1')).toBeVisible()
  expect(observed.restoreCount()).toBe(1)
  expect(observed.intakeReadsAfterRestore()).toBeGreaterThan(0)
})

test('an unreadable restored head keeps its warning through history refresh and does not refresh the viewer', async ({ page }) => {
  test.setTimeout(60_000)
  const observed = await mountVersionSurface(page, { restoredHeadReadable: false })
  const history = page.getByRole('dialog', { name: 'Version history' })
  const v1 = history.getByTestId('vh-row-v1')

  await v1.getByRole('button', { name: 'Restore', exact: true }).click()
  await v1.getByRole('button', { name: 'Restore v1' }).click()

  await expect(history.getByTestId('vh-row-v4')).toBeVisible()
  await expect(history.getByTestId('vh-head-warning')).toContainText('not readable yet')
  expect(observed.restoreCount()).toBe(1)
  expect(observed.intakeReadsAfterRestore()).toBe(0)

  await history.getByRole('button', { name: 'Close version history' }).click()
  const persistentLock = page.getByTestId('unreadable-head-lock')
  await expect(persistentLock).toContainText('Restored as v4')
  await expect(persistentLock).toHaveAttribute('data-head', '4')
  await expect(persistentLock).toHaveAttribute('data-latest', '4')
  await expect(page.getByRole('button', { name: 'Undo' })).toBeDisabled()
  await expect(page.getByRole('button', { name: 'Redo' })).toBeDisabled()
  await page.getByRole('button', { name: /Drawing tools/ }).click()
  const writeTool = page.locator('.tool-card').filter({ hasText: 'arrange-panels-as-cat' })
  await writeTool.getByRole('button').first().click()
  await expect(writeTool.getByRole('button', { name: 'Review & run' })).toBeDisabled()

  // Closing and reopening the drawer cannot erase the controller-owned lock.
  await page.getByRole('button', { name: 'History' }).click()
  const reopened = page.getByRole('dialog', { name: 'Version history' })
  await expect(reopened.getByTestId('vh-head-warning')).toBeVisible()
  await expect(reopened.getByTestId('vh-row-v1').getByRole('button', { name: 'Restore', exact: true })).toBeDisabled()
  await reopened.getByRole('button', { name: 'Close version history' }).click()

  // A successful head intake read repairs the state and only then unlocks edits.
  await persistentLock.getByRole('button', { name: 'Retry loading' }).click()
  await expect(persistentLock).toBeHidden()
  expect(observed.intakeReadsAfterRestore()).toBe(1)
  await expect(page.getByRole('button', { name: 'Undo' })).toBeEnabled()
})

test('an unreadable committed head locks writes before a stalled history refresh and survives a failed repair', async ({ page }) => {
  test.setTimeout(60_000)
  const observed = await mountVersionSurface(page, {
    restoredHeadReadable: false,
    stallHistoryAfterRestore: true,
    failFirstRepairRead: true,
  })
  const history = page.getByRole('dialog', { name: 'Version history' })
  const v1 = history.getByTestId('vh-row-v1')

  await v1.getByRole('button', { name: 'Restore', exact: true }).click()
  await v1.getByRole('button', { name: 'Restore v1' }).click()

  // The post-commit history GET is still pending, but the committed response
  // must already have moved the controller head and locked every write path.
  const persistentLock = page.getByTestId('unreadable-head-lock')
  await expect(persistentLock).toHaveAttribute('data-head', '4')
  await history.getByRole('button', { name: 'Close version history' }).click()
  await expect(page.getByRole('button', { name: 'Undo' })).toBeDisabled()
  await expect(page.getByRole('button', { name: 'Redo' })).toBeDisabled()

  // A failed intake retry cannot clear the lock. Only a later successful read
  // can seat the committed head and make mutations eligible again.
  await persistentLock.getByRole('button', { name: 'Retry loading' }).click()
  await expect(persistentLock).toBeVisible()
  await expect(page.getByRole('button', { name: 'Undo' })).toBeDisabled()
  await persistentLock.getByRole('button', { name: 'Retry loading' }).click()
  await expect(persistentLock).toBeHidden()
  expect(observed.intakeReadsAfterRestore()).toBe(2)

  observed.releaseHistoryRefresh()
})

test('a stale post-restore head response cannot clear the write lock', async ({ page }) => {
  test.setTimeout(60_000)
  const observed = await mountVersionSurface(page, { staleFirstHeadAfterRestore: true })
  const history = page.getByRole('dialog', { name: 'Version history' })
  const v1 = history.getByTestId('vh-row-v1')

  await v1.getByRole('button', { name: 'Restore', exact: true }).click()
  await v1.getByRole('button', { name: 'Restore v1' }).click()

  const persistentLock = page.getByTestId('unreadable-head-lock')
  await expect(persistentLock).toHaveAttribute('data-head', '4')
  await expect(persistentLock).not.toHaveAttribute('data-pending', 'true')
  await expect(persistentLock.getByRole('button', { name: 'Retry loading' })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Undo' })).toBeDisabled()
  expect(observed.intakeReadsAfterRestore()).toBe(1)

  await persistentLock.getByRole('button', { name: 'Retry loading' }).click()
  await expect(persistentLock).toBeHidden()
  await expect(page.getByRole('button', { name: 'Undo' })).toBeEnabled()
  expect(observed.intakeReadsAfterRestore()).toBe(2)
})

test('a split head response (new head, old geometry) cannot clear the write lock', async ({ page }) => {
  // Round-3 pin: the server can resolve the intake at v3 and reload the
  // manifest AFTER a restore commits, reporting {intake v3, head 4}. The
  // head field proves nothing about what was seated; the lock must hold
  // until a COHERENT response (version === head) arrives.
  test.setTimeout(60_000)
  const observed = await mountVersionSurface(page, { splitReadFirstHeadAfterRestore: true })
  const history = page.getByRole('dialog', { name: 'Version history' })
  const v1 = history.getByTestId('vh-row-v1')

  await v1.getByRole('button', { name: 'Restore', exact: true }).click()
  await v1.getByRole('button', { name: 'Restore v1' }).click()

  const persistentLock = page.getByTestId('unreadable-head-lock')
  await expect(persistentLock).toHaveAttribute('data-head', '4')
  await expect(page.getByRole('button', { name: 'Undo' })).toBeDisabled()
  expect(observed.intakeReadsAfterRestore()).toBe(1)

  await persistentLock.getByRole('button', { name: 'Retry loading' }).click()
  await expect(persistentLock).toBeHidden()
  await expect(page.getByRole('button', { name: 'Undo' })).toBeEnabled()
  expect(observed.intakeReadsAfterRestore()).toBe(2)
})

test('an undo that landed after the restore releases the lock instead of wedging it', async ({ page }) => {
  // Round-3 pin: head legitimately DECREASES when an in-flight undo lands
  // after the restore. The response {version 3, head 3, latest 4} is
  // coherent AND post-restore (latest watermark), so the lock must release
  // — a >= head rule would wedge with redo disabled forever.
  test.setTimeout(60_000)
  const observed = await mountVersionSurface(page, { failFirstRepairRead: true, undoLandedOnSecondRead: true })
  const history = page.getByRole('dialog', { name: 'Version history' })
  const v1 = history.getByTestId('vh-row-v1')

  await v1.getByRole('button', { name: 'Restore', exact: true }).click()
  await v1.getByRole('button', { name: 'Restore v1' }).click()

  const persistentLock = page.getByTestId('unreadable-head-lock')
  await expect(persistentLock).toHaveAttribute('data-head', '4')
  await expect(page.getByRole('button', { name: 'Undo' })).toBeDisabled()
  expect(observed.intakeReadsAfterRestore()).toBe(1)

  await persistentLock.getByRole('button', { name: 'Retry loading' }).click()
  await expect(persistentLock).toBeHidden()
  // head 3 / latest 4: redo is the operation that makes sense here.
  await expect(page.getByRole('button', { name: 'Redo' })).toBeEnabled()
  expect(observed.intakeReadsAfterRestore()).toBe(2)
})

test('/try neither requests deltas nor exposes restore controls', async ({ page }) => {
  const state = makeCatProofState()
  const versionQueries = []
  await page.route('**/api/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    if (url.pathname.includes('/versions')) versionQueries.push(url.search)
    const body = request.postData() ? request.postDataJSON() : {}
    const result = catProofResponse({
      method: request.method(), path: url.pathname, body,
      query: Object.fromEntries(url.searchParams),
    }, state)
    await route.fulfill({
      status: result.status,
      contentType: result.body == null ? undefined : 'application/json',
      body: result.body == null ? '' : JSON.stringify(result.body),
      headers: { 'access-control-allow-origin': '*', 'access-control-allow-headers': '*' },
    })
  })

  await page.goto('/try')
  await expect(page.getByTestId('operator-phase')).toContainText(/ready/i, { timeout: 15_000 })
  await expect(page.getByRole('button', { name: /^Restore/ })).toHaveCount(0)
  expect(versionQueries.length).toBeGreaterThan(0)
  expect(versionQueries.some((query) => query.includes('include_deltas'))).toBe(false)
})
