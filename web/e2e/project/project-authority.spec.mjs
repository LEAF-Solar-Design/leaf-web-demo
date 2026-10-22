import { test, expect } from '@playwright/test'
import { createHash } from 'node:crypto'
import { fileURLToPath } from 'node:url'
import { createProjectIdentity, disposeIdentity, metadataFromEnvironment, removeBearer, publicId } from './projectIdentity.mjs'
import { EVENT_NAME, CANONICAL_NAME, canonicalProjection } from './projectProofReporter.mjs'

// UNMEASURED runtime pins: budgets and UI reachability await the managed walk.
const RESPONSE_TIMEOUT_MS = 120_000
const metadata = metadataFromEnvironment(process.env)
const fixture = fileURLToPath(new URL('../fixtures/distinctive-panel.dxf', import.meta.url))
const projectName = `PP3B ${metadata.run_id}`
let imported
test.describe.configure({ mode: 'serial' })

function check(condition) { if (!condition) throw new Error('PP3B_ASSERTION') }
async function checkpoint(info, key) {
  await info.attach(EVENT_NAME, { body: Buffer.from(JSON.stringify({ key, value: true })), contentType: 'application/json' })
}
async function withIdentity(browser, action) {
  let identity
  try {
    identity = await createProjectIdentity(browser, metadata, process.env.LEAF_E2E_TOKEN)
    await action(identity)
  } catch { throw new Error('PP3B_BROWSER_FAILURE') }
  finally {
    if (identity) {
      try { await disposeIdentity(identity) } catch { throw new Error('PP3B_TEARDOWN') }
    }
  }
}

function observe(page) {
  // Reduce immediately. Never retain Request objects, URLs, headers or request bodies.
  const state = { count: 0, allBearer: true, noActor: true }
  const listener = request => {
    const url = new URL(request.url())
    if (![metadata.web_origin, metadata.app_origin].includes(url.origin) || !url.pathname.startsWith('/api/')) return
    const headers = request.headers()
    state.count += 1
    state.allBearer &&= typeof headers.authorization === 'string' && headers.authorization.startsWith('Bearer ')
    state.noActor &&= !Object.hasOwn(headers, 'x-actor-binding-id')
  }
  page.on('request', listener)
  return { state, stop: () => page.off('request', listener) }
}

function response(page, method, path, project) {
  return page.waitForResponse(res => {
    const url = new URL(res.url())
    return [metadata.app_origin, metadata.web_origin].includes(url.origin) &&
      url.pathname === path && res.request().method() === method
  }, { timeout: RESPONSE_TIMEOUT_MS }).then(async res => {
    // Success is contract-fixed 2xx; the exact runtime status is UNMEASURED.
    check(res.status() >= 200 && res.status() < 300)
    return project(await res.json())
  })
}

async function openProjectUI(page) {
  await page.goto('/app?surface=browser')
  const switcher = page.locator('header.top .proj-switch')
  await switcher.locator('button.proj-chip').click()
  await switcher.getByRole('menuitem').filter({ hasText: projectName }).first().click()
  await expect(page.locator('[data-ground="browser"]')).toHaveAttribute('data-project-state', 'project')
}

async function openVersionUI(page, version) {
  await page.getByLabel('Canonical drawing version', { exact: true }).selectOption(version)
  await page.getByRole('tab', { name: 'Solar CAD', exact: true }).click()
}

async function snapshot(page, projectId) {
  return page.evaluate(async ({ projectId, organizationId }) => {
    const api = await import('/src/api.js')
    const workspace = await api.openProject(projectId, organizationId)
    const projects = await api.listProjects(organizationId)
    return {
      organization_id: workspace.project.org_id, project_id: workspace.project.project_id,
      projects: projects.map(p => p.project_id).sort(),
      versions: workspace.drawing_versions.map(v => ({ organization_id: v.org_id, project_id: v.project_id,
        drawing_id: v.drawing_id, version_id: v.version_id })).sort((a, b) => a.version_id.localeCompare(b.version_id)),
      jobs: workspace.jobs.map(j => j.job_id).sort(),
    }
  }, { projectId, organizationId: metadata.organization_id })
}

async function readContext(page, inputVersion) {
  // Use the actual application client. No fetch implementation or auth overrides.
  return page.evaluate(async binding => {
    const { createProjectSolarClient } = await import('/src/api.js')
    const client = createProjectSolarClient()
    try {
      client.setScope(binding, 'pp3b-readback')
      const result = await client.readContext()
      if (!result.ok) throw new Error('PP3B_READBACK')
      const { context, intake } = result.data
      return { context: { organization_id: context.organization_id, project_id: context.project_id,
        drawing_id: context.drawing_id, input_version_id: context.input_version_id,
        head_version_id: context.head_version_id, is_head: context.is_head, intake_sha256: context.intake_sha256 },
      settings: intake.solar_design_graph?.settings ?? null,
      units: intake.solar_design_graph?.project?.units ?? null }
    } finally { client.dispose() }
  }, { kind: 'project', organization_id: metadata.organization_id,
    project_id: imported.project_id, drawing_id: imported.drawing_id, input_version_id: inputVersion })
}

function settingsHash(settings) {
  // Matches leaf_cloud_client.canonical_bytes for this ASCII, non-exponent fixture.
  const canonical = settings === null ? 'null' : JSON.stringify(Object.fromEntries(Object.keys(settings).sort().map(key => {
    const value = settings[key]
    check(typeof value === 'boolean' || (typeof value === 'string' && /^[\x20-\x7e]*$/.test(value)) ||
      (typeof value === 'number' && (Number.isSafeInteger(value) || value === 1.25)))
    return [key, value]
  })))
  return createHash('sha256').update(canonical).digest('hex')
}

test('project_browser_create_import', async ({ browser }, info) => {
  await withIdentity(browser, async ({ page }) => {
    await page.goto('/app?surface=browser')
    const seen = observe(page)
    try {
      await page.locator('.ribbon-tool[data-tool="project:create"]').click()
      const switcher = page.locator('header.top .proj-switch')
      await switcher.getByLabel('New project').fill(projectName)
      const created = response(page, 'POST', '/api/projects', data => ({ project_id: data.project.project_id, organization_id: data.project.org_id }))
      await switcher.getByRole('button', { name: 'Create project', exact: true }).click()
      const project = await created
      check(publicId(project.project_id) && project.organization_id === metadata.organization_id)
      const stored = await snapshot(page, project.project_id)
      check(stored.project_id === project.project_id && stored.organization_id === metadata.organization_id)
      await checkpoint(info, 'ui_project_created')
      // The ribbon's upload action opens the live material pane; upload completion imports through useMaterialIntake.
      await page.locator('.ribbon-tool[data-tool="files:upload"]').click()
      const attached = response(page, 'POST', `/api/projects/${project.project_id}/drawing-versions/import`, data => {
        const v = data.drawing_version
        return { project_id: v.project_id, drawing_id: v.drawing_id, input_version_id: v.version_id, organization_id: v.org_id }
      })
      await page.locator('[data-pane="material-intake"]').getByLabel('Drawing file').setInputFiles(fixture)
      imported = await attached
      check(imported.project_id === project.project_id && imported.organization_id === metadata.organization_id &&
        publicId(imported.drawing_id) && publicId(imported.input_version_id))
      const after = await snapshot(page, project.project_id)
      check(after.versions.some(v => v.version_id === imported.input_version_id && v.drawing_id === imported.drawing_id &&
        v.project_id === project.project_id && v.organization_id === metadata.organization_id))
      await checkpoint(info, 'ui_dxf_imported')
      check(seen.state.count > 0 && seen.state.allBearer)
      await checkpoint(info, 'bearer_present')
      check(seen.state.noActor)
      await checkpoint(info, 'actor_header_absent')
    } finally { seen.stop() }
  })
})

test('project_browser_bearer_required', async ({ browser }, info) => {
  await withIdentity(browser, async identity => {
    check(!!imported)
    const { page } = identity
    await openProjectUI(page)
    const before = await snapshot(page, imported.project_id)
    await removeBearer(identity)
    // Removal survives navigation. Public org/project context remains available.
    await page.reload()
    check(await page.evaluate(() => localStorage.getItem('leaf.jwt') === null))
    // Signed-out UI disables protected controls. Invoke their unchanged application functions;
    // UI inability alone would not prove that the server requires a bearer.
    const refused = await page.evaluate(async ({ projectId, organizationId }) => {
      const api = await import('/src/api.js')
      const status = async action => { try { await action(); return 0 } catch (error) { return Number(error.status) || 0 } }
      return { read: await status(() => api.openProject(projectId, organizationId)),
        mutation: await status(() => api.createProject('PP3B refused creation', organizationId)),
        bearerAbsent: localStorage.getItem('leaf.jwt') === null,
        orgRetained: localStorage.getItem('leaf.org_id') === organizationId }
    }, { projectId: imported.project_id, organizationId: metadata.organization_id })
    // Contract-fixed authentication denial range; exact 401 versus 403 is an UNMEASURED runtime pin.
    check(refused.bearerAbsent && refused.orgRetained && [401, 403].includes(refused.read))
    await checkpoint(info, 'bearerless_read_refused')
    check([401, 403].includes(refused.mutation))
    await checkpoint(info, 'bearerless_mutation_refused')
    await withIdentity(browser, async verifier => {
      await openProjectUI(verifier.page)
      check(JSON.stringify(await snapshot(verifier.page, imported.project_id)) === JSON.stringify(before))
    })
    // Browser-visible canonical readback; record C independently verifies database relationships.
    await checkpoint(info, 'bearerless_no_mutation')
  })
})

test('project_settings_worker_readback', async ({ browser }, info) => {
  await withIdentity(browser, async ({ page }) => {
    check(!!imported)
    await openProjectUI(page)
    await openVersionUI(page, imported.input_version_id)
    const before = await readContext(page, imported.input_version_id)
    check(before.settings === null && before.context.is_head && /^[a-f0-9]{64}$/.test(before.context.intake_sha256))
    const lease = response(page, 'POST', `/api/projects/${imported.project_id}/drawings/${imported.drawing_id}/checkout`, data =>
      ({ organization_id: data.organization_id, project_id: data.project_id, drawing_id: data.drawing_id,
        acquired: data.acquired, fence: Number(data.checkout?.fence) }))
    await page.getByRole('group', { name: 'Project checkout' }).getByRole('button', { name: 'Take', exact: true }).click()
    const acquired = await lease
    check(acquired.acquired && acquired.organization_id === metadata.organization_id && acquired.project_id === imported.project_id &&
      acquired.drawing_id === imported.drawing_id && Number.isSafeInteger(acquired.fence) && acquired.fence > 0)
    await checkpoint(info, 'ui_checkout_acquired')
    await page.locator('#solar-step-solar-settings').click()
    const form = page.getByTestId('solar-settings-form')
    await expect(form).toHaveAttribute('data-mode', 'initialize')
    await form.getByLabel('Project name', { exact: true }).fill(projectName)
    await form.getByLabel('Panel layer contains', { exact: true }).fill('PP3B panels')
    await form.getByLabel('Optimizer ratio', { exact: true }).fill('1.25')
    // The DXF fixture has no INSUNITS. Explicit manual metres are part of this input,
    // not a fabricated extraction result; the form binds the actual intake SHA itself.
    await form.getByLabel('Drawing units', { exact: true }).selectOption('m')
    const submitted = response(page, 'POST', '/api/run', data => ({ job_id: data.job_id, status: data.status }))
    await form.getByRole('button', { name: 'Start Solar design', exact: true }).click()
    const accepted = await submitted
    check(publicId(accepted.job_id) && accepted.status === 'submitted')
    await checkpoint(info, 'settings_submitted')
    let published
    await expect.poll(async () => {
      published = await page.evaluate(async ({ binding, jobId }) => {
        const { createProjectSolarClient } = await import('/src/api.js')
        const client = createProjectSolarClient()
        try {
          client.setScope(binding, 'pp3b-readback')
          const read = await client.readJob(jobId, { tool: 'solar-settings' })
          if (!read.ok) return null
          const job = read.data.job ?? read.data
          return { job_id: job.job_id, organization_id: job.org_id, project_id: job.project_id,
            input_version_id: job.input_version_id, output_version_id: job.result?.output_version_id,
            drawing_id: job.execution_context?.drawing_id, binding_id: job.execution_context?.actor_binding_id,
            checkout_fence: Number(job.execution_context?.checkout_fence), status: job.status,
            worker_id: job.result?.execution_provenance?.worker_id,
            parent_intake_sha256: job.result?.graph_commit?.parent_intake_sha256 }
        } finally { client.dispose() }
      }, { binding: { kind: 'project', ...imported }, jobId: accepted.job_id })
      return published?.status === 'complete'
    }, { timeout: RESPONSE_TIMEOUT_MS }).toBe(true)
    check(published.job_id === accepted.job_id && published.organization_id === metadata.organization_id &&
      published.project_id === imported.project_id && published.drawing_id === imported.drawing_id &&
      published.input_version_id === imported.input_version_id && published.output_version_id !== imported.input_version_id &&
      published.worker_id === metadata.worker_id && published.binding_id === metadata.binding_id &&
      published.checkout_fence === acquired.fence && published.parent_intake_sha256 === before.context.intake_sha256)
    await checkpoint(info, 'worker_published')
    const stored = await readContext(page, published.output_version_id)
    check(stored.context.is_head && stored.context.input_version_id === published.output_version_id &&
      stored.settings.panel_layer_contains === 'PP3B panels' && stored.settings.optimizer_ratio === 1.25 &&
      stored.units.drawing_units === 'm' && stored.units.meters_per_unit === 1)
    await checkpoint(info, 'settings_readback')
    await page.reload()
    await openProjectUI(page)
    await openVersionUI(page, published.output_version_id)
    await page.locator('#solar-step-solar-settings').click()
    await expect(page.getByTestId('solar-settings-form').getByLabel('Panel layer contains', { exact: true })).toHaveValue('PP3B panels')
    const reopened = await readContext(page, published.output_version_id)
    const canonical = canonicalProjection({ organization_id: metadata.organization_id, project_id: imported.project_id,
      drawing_id: imported.drawing_id, input_version_id: imported.input_version_id, output_version_id: published.output_version_id,
      job_id: published.job_id, binding_id: published.binding_id, worker_id: published.worker_id,
      checkout_fence: published.checkout_fence, before_settings_sha256: settingsHash(before.settings),
      settings_sha256: settingsHash(stored.settings), reopened_settings_sha256: settingsHash(reopened.settings) }, metadata)
    await checkpoint(info, 'settings_reopened')
    await info.attach(CANONICAL_NAME, { body: Buffer.from(JSON.stringify(canonical)), contentType: 'application/json' })
  })
})
