import { expect as baseExpect, test } from '@playwright/test'
import { REQUEST, catProofResponse, makeCatProofState } from '../catProofFixture.mjs'
import { scaled } from '../timeScale.mjs'

// Device CI workers differ about 4x in browser speed (2026-11-12: zachs and the two WSL workers on one
// laptop fail every fixed 20 s / 60 s deadline here while run-1 and run-2 pass). Budgets are ceilings,
// not waits: a passing test takes the same time at any scale. LEAF_E2E_TIME_SCALE overrides (1 to 10).
const budget = (ms) => Math.round(scaled(ms))

const expect = baseExpect.configure({ timeout: budget(20_000) })

// Synthetic fixture diagnostics, emitted only for the two previously failed cases.
const restoreProofDiagnostics = new WeakMap()
test.afterEach(async ({ page }, testInfo) => {
  if (testInfo.status === testInfo.expectedStatus) return
  if (!['the /app history drawer shows deltas and restores an old version as a new head',
    'an unreadable restored head keeps its warning through history refresh and does not refresh the viewer'].includes(testInfo.title)) return
  const read = restoreProofDiagnostics.get(page)
  if (!read) return
  const body = JSON.stringify({ title: testInfo.title, status: testInfo.status, ...read() })
  console.log(`RESTORE_DIAGNOSTIC ${body}`)
  await testInfo.attach('restore-fixture-diagnostic', { body, contentType: 'application/json' })
})

test.beforeEach(async ({}, testInfo) => { testInfo.setTimeout(budget(30_000)) })

// A DRAWING mutation, which is what "read-only preview" is about. Telemetry is
// a non-GET too, but it carries no drawing write and the surface emits it on
// interaction (including the preview click itself), so counting it makes the
// preview assertion flaky in a way that says nothing about read-only-ness.
// Everything else non-GET still counts, so a real write during a preview
// fails these rows exactly as before.
const TELEMETRY_PATHS = new Set(['/api/telemetry'])
const SAVE_DXF = [
  '0', 'SECTION', '2', 'ENTITIES',
  '0', 'LINE', '5', '10', '8', 'Panels',
  '10', '0', '20', '0', '30', '0', '11', '100', '21', '50', '31', '0',
  '0', 'ENDSEC', '0', 'EOF', '',
].join('\n')
function isDrawingMutation(method, path) {
  return method !== 'GET' && !TELEMETRY_PATHS.has(path)
}

function versionRows(head) {
  return [
    { v: 1, parent: null, tool: 'base', note: 'Original drawing', delta: null },
    { v: 2, parent: 1, tool: 'move-panel', note: 'Moved A', delta: { added: 0, modified: 1, deleted: 0 } },
    { v: 3, parent: 2, tool: 'delete-panel', note: 'Removed B', delta: { added: 0, modified: 0, deleted: 1 } },
    ...(head >= 4 ? [{
      v: 4, parent: 3, tool: 'restore', note: 'restore of version 1',
      delta: { added: 1, modified: 1, deleted: 0 },
    }] : []),
    ...(head >= 5 ? [{
      v: 5, parent: 4, tool: 'restore', note: 'restore of version 2',
      delta: { added: 0, modified: 1, deleted: 0 },
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
  coverSaves = false,
  beforeApprove,
} = {}) {
  const proofState = makeCatProofState()
  let head = 3
  let restoreCount = 0
  let intakeReadsAfterRestore = 0
  let deltasRequested = false
  const savedRows = versionRows(3)
  const saveRequests = []
  // Every drawing-mutating request the surface makes to the API. A read-only
  // preview must add NOTHING to this list; asserting on disabled buttons alone
  // would pass against a UI that greys a control and still fires the request.
  const mutating = []
  const diagnosticStarted = Date.now()
  const diagnosticRoutes = []
  restoreProofDiagnostics.set(page, () => ({
    head, restoreCount, intakeReadsAfterRestore, deltasRequested,
    mutating: [...mutating], routes: diagnosticRoutes.slice(-24),
  }))
  let releaseHistoryRefresh
  const historyRefreshGate = new Promise((resolve) => { releaseHistoryRefresh = resolve })

  await page.route('**/api/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const method = request.method()
    const path = url.pathname
    if (path.startsWith('/api/drawings/cat-panels/versions') || path === '/api/drawings/cat-panels/intake') {
      diagnosticRoutes.push({ method, path, elapsedMs: Date.now() - diagnosticStarted })
    }
    const multipart = request.headers()['content-type']?.startsWith('multipart/form-data')
    const body = request.postData() && !multipart ? request.postDataJSON() : {}
    if (isDrawingMutation(method, path)) mutating.push(`${method} ${path}`)
    let result

    if (coverSaves && path === '/api/drawings/cat-panels/dxf' && method === 'GET') {
      await route.fulfill({
        status: 200, contentType: 'application/dxf', body: SAVE_DXF,
        headers: {
          'access-control-allow-origin': '*',
          'access-control-expose-headers': 'x-leaf-version, x-leaf-head',
          'x-leaf-version': String(head), 'x-leaf-head': String(head),
        },
      })
      return
    }
    if (coverSaves && /^\/api\/drawings\/cat-panels\/versions\/(edited|plan)$/.test(path) && method === 'POST') {
      const kind = path.split('/').at(-1)
      saveRequests.push({ kind, multipart, body: request.postDataBuffer().toString('utf8') })
      const parent = head++
      savedRows.push({
        v: head, parent, tool: kind === 'edited' ? 'cad-edit-surface' : 'cad-edit-plan',
        note: 'Saved browser edit', delta: { added: 1, modified: 0, deleted: 0 },
      })
      result = { status: 201, body: {
        drawing_id: 'cat-panels', head, latest: head,
        new_version: { drawing_id: 'cat-panels', version: head, parent },
        ...(kind === 'plan' ? { commit: 'dwg-plan' } : {}),
      } }
    } else if (path === '/api/drawings/cat-panels/versions' && method === 'GET') {
      deltasRequested ||= url.searchParams.get('include_deltas') === '1'
      if (restoreCount && stallHistoryAfterRestore) await historyRefreshGate
      result = {
        status: 200,
        body: {
          drawing_id: 'cat-panels', head, latest: head, checkout: coverSaves ? proofState.checkout : null,
          versions: coverSaves ? savedRows : versionRows(head),
        },
      }
    } else if (/^\/api\/drawings\/cat-panels\/versions\/[123]\/restore$/.test(path) && method === 'POST') {
      const restoredFrom = Number(path.split('/').at(-2))
      restoreCount += 1
      if (coverSaves) {
        const parent = head++
        savedRows.push({ v: head, parent, tool: 'restore', note: `restore of version ${restoredFrom}` })
      } else head = 3 + restoreCount
      result = {
        status: 200,
        body: {
          error: null, restored_from: restoredFrom, head, latest: head,
          restored_head_readable: restoreCount > 1 || restoredHeadReadable,
          new_version: { drawing_id: 'cat-panels', version: head, parent: head - 1 },
        },
      }
    } else if (coverSaves && path === '/api/drawings/cat-panels/intake' && method === 'GET') {
      if (restoreCount) intakeReadsAfterRestore += 1
      const requested = url.searchParams.get('version')
      const version = requested && requested !== 'head' ? Number(requested) : head
      result = { status: 200, body: {
        drawing_id: 'cat-panels', intake: proofState.base, version, head, latest: head,
      } }
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
      if (coverSaves && path === '/api/jobs/cat-job-0002' && method === 'GET') {
        result.body.result.result.new_version = { drawing_id: 'cat-panels', version: 3, parent: 2 }
      }
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
  await expect(page.locator('.viewer-title')).toContainText('cat.dwg')
  // Each profile starts with its floating panels closed. Enter through the
  // same Studio control a user opens before giving the drawing a command.
  const studio = page.getByRole('navigation', { name: 'Workspace controls' }).getByRole('button', { name: 'Studio', exact: true })
  await studio.click()
  await expect(studio).toHaveAttribute('aria-expanded', 'true')
  // Seat a real versioned drawing through App's production controller path.
  // The initial session intake alone is intentionally unversioned.
  await page.getByLabel('Command bar', { exact: true }).fill(REQUEST)
  await page.getByRole('button', { name: 'Run' }).click()
  const approval = page.locator('.converse-confirm').filter({ hasText: 'arrange-panels-as-cat' })
  if (beforeApprove) await beforeApprove(approval.getByRole('button', { name: 'Approve' }))
  await approval.getByRole('button', { name: 'Approve' }).click()
  const attach = page.getByRole('button', { name: 'Attach' })
  await expect(attach).toBeVisible()
  await attach.click()
  await expect(page.getByRole('button', { name: 'History' })).toBeVisible()
  await page.getByRole('button', { name: 'History' }).click()
  await expect(page.getByRole('dialog', { name: 'Version history' })).toBeVisible()

  return {
    mutating: () => [...mutating],
    deltasRequested: () => deltasRequested,
    intakeReadsAfterRestore: () => intakeReadsAfterRestore,
    restoreCount: () => restoreCount,
    saveRequests,
    releaseHistoryRefresh,
  }
}

test('the CAD Approve button is uncovered at rest before clicking', async ({ page }) => {
  test.setTimeout(budget(60_000))
  await mountVersionSurface(page, {
    beforeApprove: async (approve) => {
      await expect(page.locator('.app[data-studio-shell="cockpit"][data-surface="cad"] .cad-overview')).toBeVisible()
      await expect(approve).toBeVisible()
      const button = await approve.elementHandle()
      expect(await page.evaluate((button) => {
        const rect = button.getBoundingClientRect()
        const hit = document.elementFromPoint(rect.x + rect.width / 2, rect.y + rect.height / 2)
        return hit === button || button.contains(hit)
      }, button)).toBe(true)
    },
  })
})

test('the /app history drawer shows deltas and restores an old version as a new head', async ({ page }) => {
  test.setTimeout(budget(60_000))
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
  await expect.poll(() => observed.intakeReadsAfterRestore()).toBeGreaterThan(0)
})

async function saveAndRestoreVersion(page, kind) {
  const observed = await mountVersionSurface(page, { coverSaves: true })
  const history = page.getByRole('dialog', { name: 'Version history' })
  await expect(history.getByTestId('vh-row-v3').locator('.vh-mark')).toHaveText('head')
  await history.getByRole('button', { name: 'Close version history' }).click()

  // Production control: EngineRibbonClusters.jsx, fileTools/save-version,
  // calls session.actions.save(). App.jsx engineSaveTarget.save chooses
  // saveEditedDrawingVersion for a hand import and saveDrawingVersionPlan
  // for a diff against the loaded head. There are no separate save buttons.
  const ribbon = page.getByTestId('drafting-ribbon')
  await page.getByRole('tab', { name: 'Insert', exact: true }).click()
  const save = ribbon.locator('[data-tool="save-version"]')
  if (await save.count() === 0) {
    test.fail(true, `Missing /app Save version control (data-tool="save-version") for the ${kind}-version save; App.jsx gates EngineRibbonClusters on ENV_CAD_EDIT.`)
    expect(await save.count(), 'the ribbon Save version control must exist').toBe(1)
    return
  }

  await expect(page.locator('.workspace-card[data-engine-document="cat-panels-v3.dxf"]')).toHaveCount(1, { timeout: budget(60_000) })
  if (kind === 'edited') {
    const open = ribbon.locator('[data-tool="import-dxf"]')
    await open.click()
    await page.getByLabel('DXF file', { exact: true }).setInputFiles({
      name: 'history-edit.dxf', mimeType: 'application/dxf', buffer: Buffer.from(SAVE_DXF),
    })
    await expect(page.locator('.workspace-card[data-engine-document="history-edit.dxf"]')).toHaveCount(1, { timeout: budget(60_000) })
    await open.click()
  }

  // Use the cockpit's real LINE operands and Run control, as in the
  // one-shell-mount proof. A hand import has no committed base for a plan.
  await page.getByRole('tab', { name: 'Draw', exact: true }).click()
  await expect(page.getByTestId('cad-edit-entity-count')).toHaveText('1')
  await ribbon.locator('[data-tool="draw:createLine"]').click()
  await page.getByLabel('ribbon x', { exact: true }).fill('0')
  await page.getByLabel('ribbon y', { exact: true }).fill('0')
  await page.getByLabel('ribbon x2', { exact: true }).fill('10')
  await page.getByLabel('ribbon y2', { exact: true }).fill('10')
  await page.getByTestId('cockpit-prompt-run').click()
  await expect(page.getByTestId('cad-edit-entity-count')).toHaveText('2', { timeout: budget(60_000) })
  await page.keyboard.press('Escape')
  await page.getByRole('tab', { name: 'Insert', exact: true }).click()
  await expect(save).toBeEnabled()
  const beforeSave = observed.mutating().length
  await save.click()
  await expect.poll(() => observed.saveRequests.length).toBe(1)
  const submitted = observed.saveRequests[0]
  expect(submitted.kind).toBe(kind)
  expect(submitted.multipart).toBe(true)
  expect(submitted.body).toMatch(/name="parent_version"\r\n\r\n3\r\n/)
  expect(submitted.body.includes('name="plan"')).toBe(kind === 'plan')
  expect(observed.mutating().slice(beforeSave)).toContain(`POST /api/drawings/cat-panels/versions/${kind}`)

  // A success message is not the persistence oracle: reopen server-backed
  // history and require exactly one new head with the actual kind label.
  await expect(save).toBeDisabled()
  await page.getByRole('button', { name: 'History', exact: true }).click()
  const saved = history.getByTestId('vh-row-v4')
  await expect(saved.locator('.vh-mark')).toHaveText('head')
  await expect(saved.locator('.vh-tool')).toHaveText(kind === 'edited' ? 'cad-edit-surface' : 'cad-edit-plan')
  await expect(history.locator('.vh-mark')).toHaveCount(1)
  await expect(history.getByTestId('vh-row-v3').locator('.vh-mark')).toHaveCount(0)

  const previous = history.getByTestId('vh-row-v3')
  await previous.getByRole('button', { name: 'Restore', exact: true }).click()
  await previous.getByRole('button', { name: 'Restore v3', exact: true }).click()
  await expect(history).toBeHidden()
  await page.getByRole('button', { name: 'History', exact: true }).click()
  // Restore appends a copy of v3, rather than moving the head pointer back.
  const restored = history.getByTestId('vh-row-v5')
  await expect(restored.locator('.vh-mark')).toHaveText('head')
  await expect(restored.locator('.vh-tool')).toHaveText('restore')
  await expect(restored).toContainText('restore of version 3')
  await expect(saved.locator('.vh-mark')).toHaveCount(0)
  await expect(history.locator('.vh-mark')).toHaveCount(1)
  expect(observed.restoreCount()).toBe(1)
  await expect.poll(() => observed.intakeReadsAfterRestore()).toBeGreaterThan(0)
  expect(observed.saveRequests).toHaveLength(1)
}

test('saving an edited version makes it head with its kind, then restores the previous version', async ({ page }) => {
  test.setTimeout(budget(180_000))
  await saveAndRestoreVersion(page, 'edited')
})

test('saving a plan version makes it head with its kind, then restores the previous version', async ({ page }) => {
  test.setTimeout(budget(180_000))
  await saveAndRestoreVersion(page, 'plan')
})

test('an unreadable restored head keeps its warning through history refresh and does not refresh the viewer', async ({ page }) => {
  test.setTimeout(budget(60_000))
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
  await expect(page.locator('[data-quick="quick-undo"]')).toBeDisabled()
  await expect(page.locator('[data-quick="quick-redo"]')).toBeDisabled()
  // Studio's open overlay already exposes the tool catalog.
  await page.getByRole('button', { name: /Drawing tools/ }).click()
  const writeTool = page.locator('.tool-card').filter({ hasText: 'arrange-panels-as-cat' })
  await writeTool.getByRole('button').first().click()
  await expect(writeTool.getByRole('button', { name: 'Review & run' })).toBeDisabled()

  // Closing and reopening the drawer cannot erase the controller-owned lock.
  await page.getByRole('button', { name: 'History' }).click()
  const reopened = page.getByRole('dialog', { name: 'Version history' })
  await expect(reopened.getByTestId('vh-head-warning')).toBeVisible()
  const recovery = reopened.getByTestId('vh-row-v2')
  await expect(recovery.getByRole('button', { name: 'Recover', exact: true })).toBeEnabled()
  await recovery.getByRole('button', { name: 'Recover', exact: true }).click()
  await recovery.getByRole('button', { name: 'Recover from v2' }).click()

  // Only the historical recovery bypasses the lock. It appends v5 with v4 as
  // parent, seats the coherent new head, and only then re-enables mutations.
  await expect(persistentLock).toBeHidden()
  expect(observed.intakeReadsAfterRestore()).toBe(1)
  expect(observed.restoreCount()).toBe(2)
  await expect(page.locator('[data-quick="quick-undo"]')).toBeEnabled()
})

test('an unreadable committed head locks writes before a stalled history refresh and survives a failed repair', async ({ page }) => {
  test.setTimeout(budget(60_000))
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
  await expect(page.locator('[data-quick="quick-undo"]')).toBeDisabled()
  await expect(page.locator('[data-quick="quick-redo"]')).toBeDisabled()

  // A failed intake retry cannot clear the lock. Only a later successful read
  // can seat the committed head and make mutations eligible again.
  await persistentLock.getByRole('button', { name: 'Retry loading' }).click()
  await expect(persistentLock).toBeVisible()
  await expect(page.locator('[data-quick="quick-undo"]')).toBeDisabled()
  await persistentLock.getByRole('button', { name: 'Retry loading' }).click()
  await expect(persistentLock).toBeHidden()
  expect(observed.intakeReadsAfterRestore()).toBe(2)

  observed.releaseHistoryRefresh()
})

test('a stale post-restore head response cannot clear the write lock', async ({ page }) => {
  test.setTimeout(budget(60_000))
  const observed = await mountVersionSurface(page, { staleFirstHeadAfterRestore: true })
  const history = page.getByRole('dialog', { name: 'Version history' })
  const v1 = history.getByTestId('vh-row-v1')

  await v1.getByRole('button', { name: 'Restore', exact: true }).click()
  await v1.getByRole('button', { name: 'Restore v1' }).click()

  const persistentLock = page.getByTestId('unreadable-head-lock')
  await expect(persistentLock).toHaveAttribute('data-head', '4')
  // In the studio shell the open drawer overlays the column's right edge, where the lock's Retry sits; close it first (as the two rows above do).
  await history.getByRole('button', { name: 'Close version history' }).click()
  await expect(persistentLock).not.toHaveAttribute('data-pending', 'true')
  await expect(persistentLock.getByRole('button', { name: 'Retry loading' })).toBeVisible()
  await expect(page.locator('[data-quick="quick-undo"]')).toBeDisabled()
  expect(observed.intakeReadsAfterRestore()).toBe(1)

  await persistentLock.getByRole('button', { name: 'Retry loading' }).click()
  await expect(persistentLock).toBeHidden()
  await expect(page.locator('[data-quick="quick-undo"]')).toBeEnabled()
  expect(observed.intakeReadsAfterRestore()).toBe(2)
})

test('a split head response (new head, old geometry) cannot clear the write lock', async ({ page }) => {
  // Round-3 pin: the server can resolve the intake at v3 and reload the
  // manifest AFTER a restore commits, reporting {intake v3, head 4}. The
  // head field proves nothing about what was seated; the lock must hold
  // until a COHERENT response (version === head) arrives.
  test.setTimeout(budget(60_000))
  const observed = await mountVersionSurface(page, { splitReadFirstHeadAfterRestore: true })
  const history = page.getByRole('dialog', { name: 'Version history' })
  const v1 = history.getByTestId('vh-row-v1')

  await v1.getByRole('button', { name: 'Restore', exact: true }).click()
  await v1.getByRole('button', { name: 'Restore v1' }).click()

  const persistentLock = page.getByTestId('unreadable-head-lock')
  await expect(persistentLock).toHaveAttribute('data-head', '4')
  // In the studio shell the open drawer overlays the column's right edge, where the lock's Retry sits; close it first (as the two rows above do).
  await history.getByRole('button', { name: 'Close version history' }).click()
  await expect(persistentLock).not.toHaveAttribute('data-pending', 'true')
  await expect(persistentLock.getByRole('button', { name: 'Retry loading' })).toBeVisible()
  await expect(page.locator('[data-quick="quick-undo"]')).toBeDisabled()
  expect(observed.intakeReadsAfterRestore()).toBe(1)

  await persistentLock.getByRole('button', { name: 'Retry loading' }).click()
  await expect(persistentLock).toBeHidden()
  await expect(page.locator('[data-quick="quick-undo"]')).toBeEnabled()
  expect(observed.intakeReadsAfterRestore()).toBe(2)
})

test('an undo that landed after the restore releases the lock instead of wedging it', async ({ page }) => {
  // Round-3 pin: head legitimately DECREASES when an in-flight undo lands
  // after the restore. The response {version 3, head 3, latest 4} is
  // coherent AND post-restore (latest watermark), so the lock must release
  // — a >= head rule would wedge with redo disabled forever.
  test.setTimeout(budget(60_000))
  const observed = await mountVersionSurface(page, { failFirstRepairRead: true, undoLandedOnSecondRead: true })
  const history = page.getByRole('dialog', { name: 'Version history' })
  const v1 = history.getByTestId('vh-row-v1')

  await v1.getByRole('button', { name: 'Restore', exact: true }).click()
  await v1.getByRole('button', { name: 'Restore v1' }).click()

  const persistentLock = page.getByTestId('unreadable-head-lock')
  await expect(persistentLock).toHaveAttribute('data-head', '4')
  // In the studio shell the open drawer overlays the column's right edge, where the lock's Retry sits; close it first (as the two rows above do).
  await history.getByRole('button', { name: 'Close version history' }).click()
  await expect(persistentLock).not.toHaveAttribute('data-pending', 'true')
  await expect(persistentLock.getByRole('button', { name: 'Retry loading' })).toBeVisible()
  await expect(page.locator('[data-quick="quick-undo"]')).toBeDisabled()
  expect(observed.intakeReadsAfterRestore()).toBe(1)

  await persistentLock.getByRole('button', { name: 'Retry loading' }).click()
  await expect(persistentLock).toBeHidden()
  // head 3 / latest 4: redo is the operation that makes sense here.
  await expect(page.locator('[data-quick="quick-redo"]')).toBeEnabled()
  expect(observed.intakeReadsAfterRestore()).toBe(2)
})

// STANDARDIZATION SLICE 6a re-pin. This row asserted the ABSENCE of
// `include_deltas` on /try, which encoded the drift the slice removes: /app
// rendered delta chips and /try did not, from the same controller and the same
// endpoint. Both shells now render the ONE VersionList primitive, so /try asks
// for deltas exactly as the drawer does. The assertion is INVERTED on purpose,
// not deleted: the query is still pinned, and a regression that stopped
// requesting deltas would fail here.
test('/try requests deltas like /app and still hides recovery controls while its head is readable', async ({ page }) => {
  const state = makeCatProofState()
  const versionQueries = []
  const mutating = []
  await page.route('**/api/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    if (url.pathname.includes('/versions')) versionQueries.push(url.search)
    if (isDrawingMutation(request.method(), url.pathname)) mutating.push(`${request.method()} ${url.pathname}`)
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

  await page.goto('/try?proof=1')
  await expect(page.getByTestId('operator-phase')).toContainText(/ready/i)
  await expect(page.getByRole('button', { name: /^Restore/ })).toHaveCount(0)
  expect(versionQueries.length).toBeGreaterThan(0)

  // Opening the tab is what runs loadHistory, and THAT is the call the slice
  // changed: the controller has always asked for deltas, /try's adapter used
  // to drop the flag on the floor.
  await page.getByRole('tab', { name: /^Versions/ }).click()
  // v2, NOT v1: the proof fixture's head is v1, and previewing the head is the
  // controller's "back to head" branch, which would clear the preview it is
  // supposed to open.
  const olderRow = page.getByTestId('try-version-v2')
  await expect(olderRow).toBeVisible()
  expect(versionQueries.some((query) => query.includes('include_deltas=1'))).toBe(true)

  // READ-ONLY WHILE PREVIEWING, on /try. PR #409 dropped this assertion and
  // #410 only half-closed the gap: a tool RUN was refused while previewing,
  // but Undo/Redo stayed live, where /app has disabled them since it shipped.
  const beforePreview = mutating.length
  await olderRow.getByRole('button').first().click()
  await expect(page.getByTestId('try-preview-write-lock')).toBeVisible()
  // The request log is the load-bearing assertion: a disabled button proves
  // nothing about a surface that greys a control and still fires the write.
  // (/try's Undo/Redo chips live in the Execution tab and are unmounted here;
  // that they carry `previewLocked` is pinned at source in
  // src/site/toolCastPreviewLock.test.js, with a positive control, because in
  // this fixture head is v1 and `canUndo` is false anyway — an e2e assertion
  // on them would pass vacuously.)
  expect(mutating.slice(beforePreview)).toEqual([])

  await page.getByRole('button', { name: 'Back to head' }).click()
  await expect(page.getByTestId('try-preview-write-lock')).toHaveCount(0)
})


// READ-ONLY WHILE PREVIEWING, on /app. The drawer has disabled its write
// controls during a preview since it shipped; nothing pinned it after PR #409
// removed the assertion, so it could have regressed silently.
test('the /app drawer is read-only while previewing an older version and fires no mutating request', async ({ page }) => {
  test.setTimeout(budget(60_000))
  const observed = await mountVersionSurface(page)
  const history = page.getByRole('dialog', { name: 'Version history' })

  // The studio shell (the only shell since W7) hides the viewer toolbar's own
  // Undo and Redo (data-cockpit="ribbon"); the quick-access band serves
  // version undo and redo under the toolbar's exact gates (App.jsx quick-undo).
  const undo = page.locator('[data-quick="quick-undo"]')
  const redo = page.locator('[data-quick="quick-redo"]')
  await expect(undo).toBeEnabled()

  const beforePreview = observed.mutating().length
  await history.getByTestId('vh-row-v1').getByRole('button', { name: /^v1/ }).click()
  await expect(history.getByText(/read-only preview/)).toBeVisible()
  await expect(undo).toBeDisabled()
  await expect(redo).toBeDisabled()
  expect(observed.mutating().slice(beforePreview)).toEqual([])

  await history.getByRole('button', { name: 'Back to head' }).click()
  await expect(history.getByText(/read-only preview/)).toHaveCount(0)
  await expect(undo).toBeEnabled()
  expect(observed.mutating().slice(beforePreview)).toEqual([])
})
