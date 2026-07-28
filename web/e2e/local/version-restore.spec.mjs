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

async function mountVersionSurface(page, { restoredHeadReadable = true } = {}) {
  const proofState = makeCatProofState()
  let head = 3
  let restoreCount = 0
  let intakeReadsAfterRestore = 0
  let deltasRequested = false

  await page.route('**/api/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const method = request.method()
    const path = url.pathname
    const body = request.postData() ? request.postDataJSON() : {}
    let result

    if (path === '/api/drawings/cat-panels/versions' && method === 'GET') {
      deltasRequested ||= url.searchParams.get('include_deltas') === '1'
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
      result = {
        status: 200,
        body: {
          drawing_id: 'cat-panels', intake: proofState.base,
          version: head, head, latest: head,
        },
      }
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
})
