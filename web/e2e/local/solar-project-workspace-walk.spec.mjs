import { expect, test } from '@playwright/test'
import { writeFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { writeProofReceipt } from '../proofReceipt.mjs'
import { H_TITLE, H_FLAGS, EQUIPMENT, demand, isUuid, loopbackBase, executionPolicy, projectBinding,
  observeProjectTraffic, actionResponse, safeJson, readContext, runProjectTool,
  assertManualGraph, assertUnknownElectrical, assertRefusalEvidence, publicEvidence, sameJson, previewHasParams } from './solarProjectWalk.mjs'

const DRAWING = fileURLToPath(new URL('../fixtures/distinctive-panel.dxf', import.meta.url))

test(H_TITLE, async ({ page }, testInfo) => {
  test.setTimeout(600000)
  const policy = executionPolicy(process.env)
  if (!policy.enabled) {
    testInfo.annotations.push({ type: 'not-exercised', description: policy.reason })
    return
  }
  const apiBase = loopbackBase(process.env.LEAF_E2E_API_BASE || 'http://127.0.0.1:8230')
  loopbackBase(testInfo.project.use.baseURL)
  const traffic = observeProjectTraffic(page, apiBase)
  const startedAt = Date.now()
  const steps = [], refusals = [], artifacts = []
  let binding = null, current = null, fence = null, checkoutHeld = false
  let originalFailure = null
  const takeGroup = () => page.getByRole('group', { name: 'Project checkout', exact: true })
  const assertDrawing = async () => {
    await expect(page.locator('.viewer-title')).toBeVisible()
    await expect(page.locator('.viewer-title')).toContainText(binding.input_version_id, { timeout: 30000 })
    await expect(page.getByTestId('solar-flow-seat')).toBeVisible()
    await expect(page.getByTestId('solar-flow-rail')).toBeVisible()
    const canvas = page.locator('.studio-ground .viewer-canvas canvas')
    await expect(canvas).toHaveCount(1)
    await expect(canvas).toBeVisible()
    demand(await canvas.evaluate(node => node.width > 0 && node.height > 0), 'canonical drawing canvas empty')
  }
  const screenshot = async name => {
    const path = testInfo.outputPath(`${name}.png`)
    await page.screenshot({ path, fullPage: false })
    await testInfo.attach(name, { path, contentType: 'image/png' })
    artifacts.push(path)
  }
  const closeEditor = async () => {
    const editor = page.locator('.project-solar-workspace-editor')
    if (!await editor.isVisible()) return
    const close = editor.getByRole('button', { name: /^(Close|Cancel)$/ })
    await expect(close).toHaveCount(1)
    await close.click()
    await expect(editor).toHaveCount(0)
  }
  const openTool = async tool => {
    await closeEditor()
    await page.getByRole('tab', { name: 'Solar', exact: true }).click()
    const target = page.getByTestId('drafting-ribbon').locator(`button[data-tool="${tool}"]`)
    const rail = page.locator(`#solar-step-${tool}`)
    const more = page.getByRole('button', { name: 'More panels', exact: true })
    await expect.poll(async () => await target.isVisible() || await more.isVisible() || await rail.isVisible(),
      { timeout: 30000, message: 'R13-H Solar control must be rendered' }).toBe(true)
    if (!await target.isVisible() && await more.isVisible()) await more.click()
    if (await target.isVisible()) { await expect(target).toBeEnabled(); await target.click() }
    else { await expect(rail).toBeEnabled(); await rail.click() }
  }
  const run = async (tool, button, checkParams) => {
    await expect(button).toBeEnabled({ timeout: 30000 })
    const result = await runProjectTool({ page, expect, apiBase, traffic, binding, before: current,
      tool, action: () => button.click(), checkParams })
    binding = result.binding
    current = result.context
    steps.push(result.step)
    // The rendered drawing must follow the exact published UUID before the next tool opens.
    await assertDrawing()
    await traffic.drain()
    return current.graph
  }
  const releaseCheckout = async () => {
    if (!checkoutHeld || !binding) return
    demand(!page.isClosed(), 'checkout cleanup unavailable')
    const release = takeGroup().getByRole('button', { name: 'Release', exact: true })
    await expect(release).toBeEnabled({ timeout: 10000 })
    const response = await actionResponse(page, apiBase, 'DELETE',
      `/api/projects/${binding.project_id}/drawings/${binding.drawing_id}/checkout`, () => release.click())
    demand(response.status() === 200, 'checkout release refused')
    const body = await safeJson(response)
    demand(body.released === true && body.checkout === null && body.organization_id === binding.organization_id
      && body.project_id === binding.project_id && body.drawing_id === binding.drawing_id, 'checkout release binding mismatch')
    checkoutHeld = false
  }
  try {
    await page.goto('/app?surface=browser')
    demand(await page.evaluate(() => typeof localStorage.getItem('leaf.jwt') === 'string' && localStorage.getItem('leaf.jwt').length > 0), 'managed browser identity missing')
    const create = page.getByRole('region', { name: 'Create your workspace', exact: true })
    const projects = page.getByRole('region', { name: 'Workspace projects', exact: true })
    await expect.poll(async () => await create.isVisible() || await projects.isVisible(),
      { timeout: 30000, message: 'R13-H workspace entry must resolve' }).toBe(true)
    let organizationId
    const name = `R13-H ${Date.now()} ${testInfo.workerIndex}`
    if (await create.isVisible()) {
      await create.getByLabel('Workspace name', { exact: true }).fill(name)
      const response = await actionResponse(page, apiBase, 'POST', '/api/orgs',
        () => create.getByRole('button', { name: 'Create workspace', exact: true }).click())
      demand(response.status() === 200 || response.status() === 201, 'workspace creation refused')
      const body = await safeJson(response)
      demand(isUuid(body.org?.org_id), 'workspace identity missing')
      organizationId = body.org.org_id
    } else {
      await expect.poll(async () => { await traffic.drain(); return traffic.organizations.length },
        { timeout: 30000, message: 'R13-H authenticated workspace read required' }).toBeGreaterThan(0)
      organizationId = traffic.organizations.at(-1).organization_id
    }
    await projects.getByLabel('Project name', { exact: true }).fill(name)
    const created = await actionResponse(page, apiBase, 'POST', '/api/projects',
      () => projects.getByRole('button', { name: 'Create project', exact: true }).click())
    demand(created.status() === 201 || created.status() === 200, 'project creation refused')
    const project = (await safeJson(created)).project
    demand(isUuid(project?.project_id) && project.org_id === organizationId && project.name === name, 'created project binding mismatch')
    await expect(page.getByText(name, { exact: true }).first()).toBeVisible()
    const material = page.locator('[data-pane="material-intake"]')
    await expect(material.getByRole('button', { name: 'Upload DWG or DXF', exact: true })).toBeEnabled()
    // Both waits precede file selection: import can finish before the first awaited response is inspected.
    const importWait = page.waitForResponse(response => {
      const url = new URL(response.url())
      return url.origin === apiBase && url.pathname === `/api/projects/${project.project_id}/drawing-versions/import`
        && response.request().method() === 'POST'
    }, { timeout: 120000 })
    const importSettled = importWait.then(response => ({ response }), () => ({ response: null }))
    const uploadResponse = await actionResponse(page, apiBase, 'POST', '/api/drawings/upload',
      () => material.getByLabel('Drawing file', { exact: true }).setInputFiles(DRAWING), 120000)
    demand(uploadResponse.status() === 202, 'drawing upload not accepted')
    const upload = await safeJson(uploadResponse)
    demand(isUuid(upload.drawing_id), 'uploaded drawing identity missing')
    const imported = (await importSettled).response
    demand(imported !== null, 'canonical import response missing')
    demand(imported.status() === 201, 'canonical import not created')
    const importedBody = await safeJson(imported)
    const version = importedBody.drawing_version
    demand(importedBody.replayed === false && isUuid(version?.version_id) && version.drawing_id === upload.drawing_id
      && version.org_id === organizationId && version.project_id === project.project_id, 'imported version binding mismatch')
    const importRequest = imported.request(), importHeaders = importRequest.headers(), importInput = importRequest.postDataJSON()
    const authenticatedImport = typeof importHeaders.authorization === 'string' && /^Bearer \S+$/i.test(importHeaders.authorization)
    const actorHeaderAbsent = !Object.hasOwn(importHeaders, 'x-actor-binding-id')
    demand(authenticatedImport && actorHeaderAbsent && importHeaders['x-org-id'] === organizationId
      && typeof importHeaders['idempotency-key'] === 'string', 'import authority mismatch')
    await traffic.drain()
    const ready = traffic.readyUploads.filter(row => row.drawing_id === upload.drawing_id).at(-1)
    demand(ready && importInput.source?.drawing_id === upload.drawing_id && importInput.source.version === ready.version, 'import source version mismatch')
    await expect(material).toContainText('Attached', { timeout: 30000 })
    binding = projectBinding({ organization_id: organizationId, project_id: project.project_id,
      drawing_id: version.drawing_id, input_version_id: version.version_id })
    const importedVersionId = binding.input_version_id
    await page.getByRole('button', { name: 'Open versions', exact: true }).click()
    await page.locator('[data-pane="versions"]').getByRole('button', { name: new RegExp(`^v\\d+ .*${importedVersionId}$`) }).click()
    await page.getByRole('tab', { name: 'Solar CAD', exact: true }).click()
    await assertDrawing()
    current = await readContext(page, apiBase, binding, { graphRequired: false })
    demand(current.graph === null, 'imported drawing unexpectedly initialized')
    const taken = await actionResponse(page, apiBase, 'POST', `/api/projects/${binding.project_id}/drawings/${binding.drawing_id}/checkout`,
      () => takeGroup().getByRole('button', { name: 'Take', exact: true }).click())
    demand(taken.status() === 200, 'checkout acquisition refused')
    const lease = await safeJson(taken)
    demand(lease.acquired === true && lease.organization_id === organizationId && lease.project_id === binding.project_id
      && lease.drawing_id === binding.drawing_id && typeof lease.checkout?.fence === 'string' && /^[0-9]+$/.test(lease.checkout.fence), 'checkout binding mismatch')
    fence = lease.checkout.fence
    checkoutHeld = true
    await expect(takeGroup().getByRole('button', { name: 'Release', exact: true })).toBeEnabled()
    await screenshot('open-canonical-drawing')

    await openTool('solar-settings')
    const settings = page.getByTestId('solar-settings-form')
    await expect(settings).toHaveAttribute('data-mode', 'initialize')
    await settings.getByLabel('Drawing units', { exact: true }).selectOption('m')
    await settings.getByLabel('Panels in sequence', { exact: true }).fill('1')
    const initialized = await run('solar-settings', settings.getByRole('button', { name: 'Start Solar design', exact: true }), params => {
      demand(params.initialize?.units?.drawing_units === 'm' && params.changes?.panels_in_sequence === 1, 'settings initialization intent mismatch')
    })
    demand(initialized.project.units.drawing_units === 'm' && initialized.settings.panels_in_sequence === 1
      && initialized.settings.panel_layer_contains === 'Panel' && initialized.settings.use_l2_collectors === false, 'saved settings mismatch')
    await openTool('solar-settings')
    await expect(settings).toHaveAttribute('data-mode', 'edit')
    await expect(settings.getByLabel('Panels in sequence', { exact: true })).toHaveValue('1')

    await openTool('solar-panels-from-drawing')
    const panels = await run('solar-panels-from-drawing', page.getByTestId('project-solar-graph-form').getByRole('button', { name: 'Review & run', exact: true }))
    demand(panels.panels.length === 1 && typeof panels.panels[0].id === 'string', 'fixture panel count mismatch')
    const panelId = panels.panels[0].id
    await openTool('solar-size-strings')
    const sizing = page.getByTestId('project-solar-sizing-form')
    await sizing.getByRole('combobox', { name: 'Scope', exact: true }).selectOption('manual-global')
    await expect(sizing).toContainText('Saved global string length: 1')
    await expect(sizing).toContainText('Manual confirmation does not calculate voltage limits or module power.')
    const sized = await run('solar-size-strings', sizing.getByRole('button', { name: 'Review & run', exact: true }), params => {
      demand(params.mode === 'manual-global' && params.confirm === true, 'manual sizing intent mismatch')
    })
    assertManualGraph(sized)
    await openTool('solar-string-add')
    const stringForm = page.getByTestId('project-solar-step-boundary')
    await stringForm.getByRole('button', { name: `Add panel ${panelId}`, exact: true }).click()
    const strung = await run('solar-string-add', stringForm.getByRole('button', { name: 'Review & run', exact: true }), params => {
      demand(params.operation === 'add-string' && sameJson(params.ordered_panel_refs, [panelId]), 'string membership intent mismatch')
    })
    demand(strung.strings.length === 1 && sameJson(strung.strings[0].ordered_panel_refs, [panelId]), 'published string membership mismatch')
    const stringId = strung.strings[0].id
    assertManualGraph(strung)
    await openTool('solar-string-conductors')
    const conductors = page.getByTestId('project-solar-conductor-form')
    await conductors.getByRole('button', { name: 'Select all', exact: true }).click()
    const gaugeControl = conductors.getByRole('combobox', { name: 'Conductor', exact: true })
    const gauge = await gaugeControl.locator('option').evaluateAll(options => options.map(option => option.value).find(value => value.trim()) || null)
    demand(typeof gauge === 'string' && gauge.trim(), 'supported conductor missing')
    await gaugeControl.selectOption(gauge)
    const wired = await run('solar-string-conductors', conductors.getByRole('button', { name: 'Apply to selected strings', exact: true }), params => {
      demand(params.operation === 'set-conductors' && params.assignments?.length === 1
        && params.assignments[0].string_ref === stringId && params.assignments[0].wire_gauge === gauge, 'conductor intent mismatch')
    })
    demand(wired.strings.length === 1 && wired.strings[0].id === stringId && wired.strings[0].wire_gauge === gauge
      && sameJson(wired.strings[0].ordered_panel_refs, [panelId]), 'published conductor mismatch')
    await openTool('solar-assign-equipment')
    const equipmentForm = page.getByTestId('project-solar-equipment-form')
    const assignments = [{ string_ref: stringId, inverter_ref: EQUIPMENT.id, mppt_letter: 'A', input_number: 0 }]
    await equipmentForm.getByLabel('Equipment', { exact: true }).fill(JSON.stringify([EQUIPMENT]))
    await equipmentForm.getByLabel('Assignments', { exact: true }).fill(JSON.stringify(assignments))
    const equipped = await run('solar-assign-equipment', equipmentForm.getByRole('button', { name: 'Review & run', exact: true }), params => {
      demand(sameJson(params.equipment, [EQUIPMENT]) && sameJson(params.assignments, assignments), 'equipment intent mismatch')
    })
    demand(equipped.inverters.length === 1 && equipped.inverters[0].id === EQUIPMENT.id
      && equipped.strings.length === 1 && equipped.strings[0].id === stringId
      && equipped.strings[0].inverter_ref === EQUIPMENT.id && equipped.strings[0].wire_gauge === gauge, 'published equipment mismatch')
    assertUnknownElectrical(equipped.inverters[0])
    assertUnknownElectrical(equipped.strings[0])
    await screenshot('equipment-remains-electrically-invalid')

    for (const [tool, code] of [['solar-homeruns', 'EQUIPMENT_ASSIGNMENT_REQUIRED'], ['solar-schedule', 'ROUTING_TOPOLOGY_REQUIRED']]) {
      const before = await readContext(page, apiBase, binding)
      const previewStart = traffic.previews.length
      const runCountBefore = traffic.runCount
      await openTool(tool)
      const form = page.getByTestId('project-solar-graph-form')
      if (tool === 'solar-schedule') await form.getByLabel('Insertion point (x, y, optional z)', { exact: true }).fill('0,0,0')
      await expect(form.getByTestId('project-solar-graph-feedback')).toContainText(code, { timeout: 30000 })
      const button = form.getByRole('button', { name: 'Review & run', exact: true })
      await expect(button).toBeDisabled()
      await traffic.drain()
      const exact = traffic.previews.slice(previewStart).filter(row => row.tool === tool
        && Object.keys(binding).every(key => row.binding[key] === binding[key])
        && previewHasParams(row, { drawing_id: binding.drawing_id, expected_rev: before.graph.rev,
          ...(tool === 'solar-schedule' ? { insertion_point: [0, 0, 0] } : {}) }))
      demand(exact.length > 0 && exact.at(-1).ready === false && exact.at(-1).codes.length === 1 && exact.at(-1).codes[0] === code
        && exact.at(-1).admissionStatus.every(status => Number.isInteger(status) && status >= 400 && status < 500),
        'exact downstream refusal missing')
      // A bounded network window supplements disabled controls and canonical readback; it is not the sole oracle.
      await page.waitForTimeout(400)
      await expect(button).toBeDisabled()
      const after = await readContext(page, apiBase, binding)
      await traffic.drain()
      assertRefusalEvidence({ before, after, code, expectedCode: code, runCountBefore, runCountAfter: traffic.runCount })
      refusals.push({ tool, code, before_head: before.head, after_head: after.head,
        run_count_before: runCountBefore, run_count_after: traffic.runCount })
      await screenshot(`${tool}-refusal`)
    }
    await closeEditor()
    await releaseCheckout()
    await traffic.drain()
    const replayPath = testInfo.outputPath('r13-h-replay.json')
    const replay = { title: H_TITLE, route: '/app?surface=browser', source_commit: policy.sourceCommit,
      viewport: page.viewportSize(), browser_version: page.context().browser()?.version() || 'not available',
      measured_duration_ms: Date.now() - startedAt,
      runtime: 'already managed loopback local/test stack; PostgreSQL and verified browser identity supplied by its supervisor',
      required_flags: Object.fromEntries(H_FLAGS.map(flag => [flag, '1'])),
      prerequisites: ['LEAF_E2E_MANAGED=1', 'LEAF_AUTH_LIVE=1', 'local/test runtime', 'adapter-installed leaf.jwt',
        'supervisor-owned workers for all six successful tools', 'accepted lifetime and managed receipt integration'],
      integration_status: 'Managed H runner integration pending; this replay describes the authored consumer.',
      selection: { spec: 'e2e/local/solar-project-workspace-walk.spec.mjs', grep: H_TITLE },
      steps: ['Create/open workspace and create project', 'Upload distinctive-panel.dxf, open observed version in Solar CAD, take checkout',
        'Initialize metre settings with sequence length 1', 'Import panel, confirm manual sizing, add string, assign conductor and equipment',
        'Observe electrical uncertainty and downstream refusals', 'Release checkout'],
    }
    writeFileSync(replayPath, `${JSON.stringify(replay, null, 2)}\n`)
    await testInfo.attach('r13-h-replay', { path: replayPath, contentType: 'application/json' })
    artifacts.push(replayPath)
    const receiptPath = testInfo.outputPath('r13-h-receipt.json')
    writeProofReceipt(receiptPath, {
      capability_ids: ['ID-04'], evidence_tier: 'local-e2e', source_commit: policy.sourceCommit,
      route: '/app?surface=browser', runtime: replay.runtime, api_endpoints: [...traffic.endpoints], artifacts,
      assertions: ['Every successful mutation matched an exact admitted project intent and its captured complete job.',
        'Canonical output UUIDs advanced the same drawing and preserved the manual electrical boundary.',
        'Equipment published with unknown power and voltage; homeruns and schedules refused without a run or head change.',
        'Checkout was released through the rendered project control.'],
      result: publicEvidence({ binding, importedVersionId, fence, gauge, steps, refusals, authenticatedImport, actorHeaderAbsent }),
      limitations: ['No voltage or module-power calculation is established.', 'No live cloud-sizing custody is established.',
        'Routing and schedules were refused, not successfully produced.', 'No production authentication or deployment proof is established.',
        'The independent supervisor must certify test selection, lifecycle cleanup and the managed receipt.'],
    })
    await testInfo.attach('r13-h-receipt', { path: receiptPath, contentType: 'application/json' })
  } catch (failure) {
    originalFailure = failure
    throw failure
  } finally {
    try { await releaseCheckout() }
    catch { if (!originalFailure) throw new Error('R13-H checkout cleanup failed') }
    finally { traffic.dispose() }
  }
})
