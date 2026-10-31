import { test, expect } from '@playwright/test'
import { createHash } from 'node:crypto'
import { fileURLToPath } from 'node:url'
import { createProjectIdentity, disposeIdentity, metadataFromEnvironment, removeBearer, publicId } from './projectIdentity.mjs'
import { EVENT_NAME, CANONICAL_NAME, SIZING_NAME, canonicalProjection, sizingRecord } from './projectProofReporter.mjs'
import { createProjectApiBundle, injectProjectApi } from './projectApiBundle.mjs'

// UNMEASURED runtime pins: budgets and UI reachability await the managed walk.
const RESPONSE_TIMEOUT_MS = 120_000
const metadata = metadataFromEnvironment(process.env)
const fixture = fileURLToPath(new URL('../fixtures/distinctive-panel.dxf', import.meta.url))
const projectName = `PP3B ${metadata.run_id}`
const projectApiBundle = createProjectApiBundle()
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

// This second module instance has its own caches and active-scope listeners.
// These fetch-backed calls use explicit ids, so the server sees the same requests.
async function navigateProject(page) {
  await page.goto(`${metadata.web_origin}/app?surface=browser`)
  await injectProjectApi(page, metadata.web_origin, await projectApiBundle())
}

async function openProjectPanel(page) {
  await page.getByRole('button', { name: 'Open project or material', exact: true }).click()
  return page.getByRole('region', { name: 'Project sidebar', exact: true })
}

async function openProjectUI(page) {
  await navigateProject(page)
  const sidebar = await openProjectPanel(page)
  await sidebar.getByRole('list', { name: 'Projects', exact: true }).getByRole('button', { name: projectName, exact: true }).click()
  await expect(page.getByRole('button', { name: `Projects: change project. Project ${projectName}`, exact: true })).toBeVisible()
}

async function openVersionUI(page, version) {
  await page.getByRole('navigation', { name: 'Workspace controls', exact: true }).getByRole('button', { name: 'Project', exact: true }).click()
  const sidebar = page.getByRole('region', { name: 'Project sidebar', exact: true })
  await sidebar.getByRole('combobox', { name: 'Canonical drawing version', exact: true }).selectOption(version)
  await sidebar.getByRole('button', { name: 'Close Project', exact: true }).click()
  await page.getByRole('combobox', { name: 'Workspace profile', exact: true }).selectOption({ label: 'Solar CAD' })
  await page.getByRole('button', { name: 'Continue workspace', exact: true }).click()
}

async function openSettingsForm(page) {
  const step = page.getByRole('navigation', { name: 'Solar design steps', exact: true }).getByRole('button', { name: /^Settings\b/ })
  if (!(await step.isVisible())) await page.getByRole('button', { name: 'Solar flow tools', exact: true }).click()
  await step.click()
  return page.getByRole('region', { name: 'Solar settings', exact: true })
}

async function snapshot(page, projectId) {
  return page.evaluate(async ({ projectId, organizationId }) => {
    const api = window.__pp3bApi
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
    const { createProjectSolarClient } = window.__pp3bApi
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
  // Matches leaf_cloud_client.canonical_bytes (json.dumps with sort_keys, compact separators and ensure_ascii) for
  // null, booleans, printable-ASCII strings and keys, safe integers and non-integral finite numbers written without an
  // exponent, nested through objects and arrays. Any other value could serialize differently in Python: fail closed.
  const ascii = value => /^[\x20-\x7e]*$/.test(value)
  const canonical = value => {
    if (value === null || typeof value === 'boolean') return JSON.stringify(value)
    if (typeof value === 'string') { check(ascii(value)); return JSON.stringify(value) }
    if (typeof value === 'number') {
      check(Number.isSafeInteger(value) || (Number.isFinite(value) && !Number.isInteger(value) && !/e/i.test(String(value))))
      return String(value)
    }
    if (Array.isArray(value)) return '[' + value.map(canonical).join(',') + ']'
    check(typeof value === 'object' && Object.getPrototypeOf(value) === Object.prototype)
    return '{' + Object.keys(value).sort().map(key => {
      check(ascii(key))
      return JSON.stringify(key) + ':' + canonical(value[key])
    }).join(',') + '}'
  }
  return createHash('sha256').update(canonical(settings)).digest('hex')
}

test('project_browser_create_import', async ({ browser }, info) => {
  await withIdentity(browser, async ({ page }) => {
    await navigateProject(page)
    const seen = observe(page)
    try {
      const sidebar = await openProjectPanel(page)
      await sidebar.getByRole('textbox', { name: 'Project name', exact: true }).fill(projectName)
      const created = response(page, 'POST', '/api/projects', data => ({ project_id: data.project.project_id, organization_id: data.project.org_id }))
      await sidebar.getByRole('button', { name: 'Create project', exact: true }).click()
      const project = await created
      check(publicId(project.project_id) && project.organization_id === metadata.organization_id)
      const stored = await snapshot(page, project.project_id)
      check(stored.project_id === project.project_id && stored.organization_id === metadata.organization_id)
      await checkpoint(info, 'ui_project_created')
      await expect(page.getByRole('button', { name: `Projects: change project. Project ${projectName}`, exact: true })).toBeVisible()
      // The board's drawing tile opens the live Material pane; upload completion imports through useMaterialIntake.
      await page.getByRole('navigation', { name: 'Workspace controls', exact: true }).getByRole('button', { name: 'Project', exact: true }).click()
      await page.getByRole('region', { name: 'Project sidebar', exact: true }).getByRole('region', { name: 'Drawing', exact: true }).getByRole('button').click()
      const attached = response(page, 'POST', `/api/projects/${project.project_id}/drawing-versions/import`, data => {
        const v = data.drawing_version
        return { project_id: v.project_id, drawing_id: v.drawing_id, input_version_id: v.version_id, organization_id: v.org_id }
      })
      const material = page.getByRole('region', { name: 'Material', exact: true })
      await expect(material.getByText(`Material attaches to ${projectName}`, { exact: true })).toBeVisible()
      await expect(material.getByRole('button', { name: 'Upload DWG or DXF', exact: true })).toBeEnabled()
      await material.getByLabel('Drawing file', { exact: true }).setInputFiles(fixture)
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
    await injectProjectApi(page, metadata.web_origin, await projectApiBundle())
    check(await page.evaluate(() => localStorage.getItem('leaf.jwt') === null))
    // Signed-out UI disables protected controls. Invoke their unchanged application functions;
    // UI inability alone would not prove that the server requires a bearer.
    const refused = await page.evaluate(async ({ projectId, organizationId }) => {
      const api = window.__pp3bApi
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
    const form = await openSettingsForm(page)
    await expect(form).toHaveAttribute('data-mode', 'initialize')
    await form.getByLabel('Project name', { exact: true }).fill(projectName)
    await form.getByLabel('Panel layer contains', { exact: true }).fill('PP3B panels')
    await form.getByLabel('Optimizer ratio', { exact: true }).fill('1.25')
    // The DXF fixture has no INSUNITS. Explicit manual metres are part of this input,
    // not a fabricated extraction result; the form binds the actual intake SHA itself.
    await form.getByRole('combobox', { name: 'Drawing units', exact: true }).selectOption('m')
    const submitted = response(page, 'POST', '/api/run', data => ({ job_id: data.job_id, status: data.status }))
    await form.getByRole('button', { name: 'Start Solar design', exact: true }).click()
    const accepted = await submitted
    check(publicId(accepted.job_id) && accepted.status === 'submitted')
    await checkpoint(info, 'settings_submitted')
    let published
    await expect.poll(async () => {
      published = await page.evaluate(async ({ binding, jobId }) => {
        const { createProjectSolarClient } = window.__pp3bApi
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
    await releaseCheckout(page)
    await page.reload()
    await openProjectUI(page)
    await openVersionUI(page, published.output_version_id)
    const reopenedForm = await openSettingsForm(page)
    await expect(reopenedForm.getByLabel('Panel layer contains', { exact: true })).toHaveValue('PP3B panels')
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

const sizingFixture = fileURLToPath(new URL('../fixtures/pp3b-sizing-panels.dxf', import.meta.url))
const sizingNames = {
  recovery: `PP3B ${metadata.run_id} sizing recovery`,
  submission: `PP3B ${metadata.run_id} sizing submission`,
  service: `PP3B ${metadata.run_id} sizing service`,
}
const isHash = value => typeof value === 'string' && /^[a-f0-9]{64}$/.test(value)
let recovered

async function releaseCheckout(page) {
  const release = page.getByRole('group', { name: 'Project checkout', exact: true }).getByRole('button', { name: 'Release', exact: true })
  await expect(release).toBeEnabled()
  await release.click()
  await expect(release).toBeDisabled()
}

async function takeCheckout(page, binding) {
  const lease = response(page, 'POST', `/api/projects/${binding.project_id}/drawings/${binding.drawing_id}/checkout`, data => ({
    organization_id: data.organization_id, project_id: data.project_id, drawing_id: data.drawing_id,
    acquired: data.acquired, fence: Number(data.checkout?.fence),
  }))
  const take = page.getByRole('group', { name: 'Project checkout', exact: true }).getByRole('button', { name: 'Take', exact: true })
  await expect(take).toBeEnabled()
  await take.click()
  const acquired = await lease
  check(acquired.acquired && acquired.organization_id === metadata.organization_id && acquired.project_id === binding.project_id &&
    acquired.drawing_id === binding.drawing_id && Number.isSafeInteger(acquired.fence) && acquired.fence > 0)
  return acquired.fence
}

async function reopenSizingProject(page, name, versionId) {
  await navigateProject(page)
  const sidebar = await openProjectPanel(page)
  await sidebar.getByRole('list', { name: 'Projects', exact: true }).getByRole('button', { name, exact: true }).click()
  await expect(page.getByRole('button', { name: `Projects: change project. Project ${name}`, exact: true })).toBeVisible()
  await openVersionUI(page, versionId)
}

// A full reopen loses the client's checkout capability (web/src/solar/solarProjectClient.js:123): release
// first, then take again.
async function moveToHead(page, name, binding, versionId) {
  await releaseCheckout(page)
  await reopenSizingProject(page, name, versionId)
  return takeCheckout(page, binding)
}

async function readGraphJob(page, binding, jobId, tool) {
  return page.evaluate(async ({ binding, jobId, tool }) => {
    const client = window.__pp3bApi.createProjectSolarClient()
    try {
      client.setScope(binding, 'pp3b-sizing-readback')
      const read = await client.readJob(jobId, { tool })
      if (!read.ok) return null
      const job = read.data.job ?? read.data
      return { job_id: job.job_id, organization_id: job.org_id, project_id: job.project_id,
        input_version_id: job.input_version_id, status: job.status,
        output_version_id: job.result?.output_version_id ?? null,
        sizing_exchange: job.result?.graph_commit?.sizing_exchange ? {
          job_id: job.result.graph_commit.sizing_exchange.job_id,
          evidence_sha256: job.result.graph_commit.sizing_exchange.evidence_sha256,
        } : null, reason_code: job.error?.reason_code ?? null, retryable: job.error?.retryable ?? null }
    } finally { client.dispose() }
  }, { binding: { kind: 'project', organization_id: metadata.organization_id, ...binding }, jobId, tool })
}

async function readSizingJob(page, binding, jobId) {
  return readGraphJob(page, binding, jobId, 'solar-size-strings')
}

async function publishedGraphJob(page, binding, jobId, tool, timeout = RESPONSE_TIMEOUT_MS) {
  let job
  await expect.poll(async () => {
    job = await readGraphJob(page, binding, jobId, tool)
    return job?.status
  }, { timeout }).toBe('complete')
  check(job.job_id === jobId && job.organization_id === metadata.organization_id && job.project_id === binding.project_id &&
    job.input_version_id === binding.input_version_id && publicId(job.output_version_id) && job.output_version_id !== binding.input_version_id)
  return job
}

async function readSizingValues(page, binding, versionId) {
  return page.evaluate(async binding => {
    const client = window.__pp3bApi.createProjectSolarClient()
    try {
      client.setScope(binding, 'pp3b-sizing-values')
      const read = await client.readContext()
      if (!read.ok) throw new Error('PP3B_READBACK')
      const { context, intake } = read.data
      if (context.organization_id !== binding.organization_id || context.project_id !== binding.project_id ||
          context.drawing_id !== binding.drawing_id || context.input_version_id !== binding.input_version_id) throw new Error('PP3B_READBACK')
      const graph = intake.solar_design_graph
      const cold = graph.settings.voc_cold
      const exchange = graph.settings.extra?.string_sizing?.project_exchange
      return { panels_in_sequence: graph.settings.panels_in_sequence,
        voc_cold: cold ? { passes: cold.passes, per_module: cold.per_module, string_voltage: cold.string_voltage,
          max_dc_voltage: cold.max_dc_voltage } : null,
        project_exchange: exchange ? { job_id: exchange.job_id, evidence_sha256: exchange.evidence_sha256 } : null,
        panel_count: graph.panels.length,
        frames: graph.frames.map(frame => ({ id: frame.id, module_power_watts: frame.module_power_watts })) }
    } finally { client.dispose() }
  }, { kind: 'project', organization_id: metadata.organization_id, project_id: binding.project_id,
    drawing_id: binding.drawing_id, input_version_id: versionId })
}

async function openGraphForm(page, label, regionName) {
  if (label === 'Panel groups') {
    // ProjectSolarWorkspace's inspection buttons use the catalog row's name when it has no label.
    const entry = page.getByRole('button', { name: 'solar-panel-groups', exact: true })
    if (!(await entry.isVisible())) await page.getByRole('button', { name: 'Solar flow tools', exact: true }).click()
    await expect(entry).toBeEnabled()
    await entry.click()
    return page.getByRole('region', { name: regionName, exact: true })
  }
  const step = page.getByRole('navigation', { name: 'Solar design steps', exact: true }).getByRole('button', { name: new RegExp(`^${label}\\b`) })
  if (!(await step.isVisible())) await page.getByRole('button', { name: 'Solar flow tools', exact: true }).click()
  if (await step.isVisible()) await step.click()
  else await page.getByRole('button', { name: label, exact: true }).click()
  return page.getByRole('region', { name: regionName, exact: true })
}

async function prepareSizingProject(page, name) {
  await navigateProject(page)
  const sidebar = await openProjectPanel(page)
  await sidebar.getByRole('textbox', { name: 'Project name', exact: true }).fill(name)
  const created = response(page, 'POST', '/api/projects', data => ({ project_id: data.project.project_id, organization_id: data.project.org_id }))
  await sidebar.getByRole('button', { name: 'Create project', exact: true }).click()
  const project = await created
  check(publicId(project.project_id) && project.organization_id === metadata.organization_id)
  await expect(page.getByRole('button', { name: `Projects: change project. Project ${name}`, exact: true })).toBeVisible()
  await page.getByRole('navigation', { name: 'Workspace controls', exact: true }).getByRole('button', { name: 'Project', exact: true }).click()
  await page.getByRole('region', { name: 'Project sidebar', exact: true }).getByRole('region', { name: 'Drawing', exact: true }).getByRole('button').click()
  const attached = response(page, 'POST', `/api/projects/${project.project_id}/drawing-versions/import`, data => ({
    project_id: data.drawing_version.project_id, drawing_id: data.drawing_version.drawing_id,
    input_version_id: data.drawing_version.version_id, organization_id: data.drawing_version.org_id,
  }))
  const material = page.getByRole('region', { name: 'Material', exact: true })
  await expect(material.getByText(`Material attaches to ${name}`, { exact: true })).toBeVisible()
  await expect(material.getByRole('button', { name: 'Upload DWG or DXF', exact: true })).toBeEnabled()
  await material.getByLabel('Drawing file', { exact: true }).setInputFiles(sizingFixture)
  const binding = await attached
  check(binding.organization_id === metadata.organization_id && binding.project_id === project.project_id &&
    publicId(binding.drawing_id) && publicId(binding.input_version_id))
  await reopenSizingProject(page, name, binding.input_version_id)
  const fence = await takeCheckout(page, binding)
  const settings = await openSettingsForm(page)
  await expect(settings).toHaveAttribute('data-mode', 'initialize')
  await settings.getByLabel('Project name', { exact: true }).fill(name)
  await settings.getByLabel('ZIP code', { exact: true }).fill('44224')
  await settings.getByLabel('Panel layer contains', { exact: true }).fill('Panels')
  await settings.getByLabel('Optimizer ratio', { exact: true }).fill('1.25')
  await settings.getByRole('combobox', { name: 'Drawing units', exact: true }).selectOption('m')
  const initialized = response(page, 'POST', '/api/run', data => ({ job_id: data.job_id }))
  await settings.getByRole('button', { name: 'Start Solar design', exact: true }).click()
  const seed = await initialized
  check(publicId(seed.job_id))
  const published = await publishedGraphJob(page, binding, seed.job_id, 'solar-settings')
  await moveToHead(page, name, binding, published.output_version_id)
  const panelsBinding = { ...binding, input_version_id: published.output_version_id }
  const panels = await openGraphForm(page, 'Panel import', 'Import panels from drawing')
  const panelsSubmitted = response(page, 'POST', '/api/run', data => ({ job_id: data.job_id }))
  await expect(panels.getByRole('button', { name: 'Review & run', exact: true })).toBeEnabled()
  await panels.getByRole('button', { name: 'Review & run', exact: true }).click()
  const panelsAccepted = await panelsSubmitted
  check(publicId(panelsAccepted.job_id))
  const panelJob = await publishedGraphJob(page, panelsBinding, panelsAccepted.job_id, 'solar-panels-from-drawing')
  const values = await readSizingValues(page, binding, panelJob.output_version_id)
  check(values.panel_count === 4 && values.frames.length === 0)
  await moveToHead(page, name, binding, panelJob.output_version_id)
  return { project_id: binding.project_id, drawing_id: binding.drawing_id,
    imported_version_id: binding.input_version_id, settings_output_version_id: published.output_version_id,
    panels_job_id: panelJob.job_id, panels_output_version_id: panelJob.output_version_id, fence }
}

async function openSizingForm(page, binding) {
  check(publicId(binding.project_id) && publicId(binding.input_version_id))
  const form = await openGraphForm(page, 'Manual sizing', / parameters$/)
  await expect(form.getByRole('combobox', { name: 'Scope', exact: true })).toBeEnabled()
  return form
}

async function submitGlobalSizing(page, form) {
  await form.getByRole('combobox', { name: 'Scope', exact: true }).selectOption('global')
  await form.getByRole('textbox', { name: 'Grant reference', exact: true }).fill('project-replay-fixture')
  const module = form.getByRole('combobox', { name: 'Module', exact: true })
  await module.fill('JA_Solar_JAM72D40-595/MB')
  const options = form.getByRole('listbox', { name: 'Sizer modules', exact: true })
  await expect(options).toBeVisible()
  await options.getByRole('option', { name: /^JA_Solar_JAM72D40-595\/MB\b/ }).click()
  await expect(module).toHaveValue('JA_Solar_JAM72D40-595/MB')
  await expect(options).toBeHidden()
  await form.getByLabel('Inverter', { exact: true }).fill('Sungrow SG-HX SG250HX')
  await form.getByRole('combobox', { name: 'Bifacial', exact: true }).selectOption('false')
  await form.getByLabel('Bifacial coefficient', { exact: true }).fill('.7')
  await form.getByRole('combobox', { name: 'Racking', exact: true }).selectOption('fixed_tilt')
  await form.getByLabel('Surface tilt', { exact: true }).fill('5')
  await form.getByLabel('Surface azimuth', { exact: true }).fill('180')
  await form.getByLabel('Albedo', { exact: true }).fill('.25')
  await form.getByLabel('Maximum voltage', { exact: true }).fill('1500')
  await form.getByLabel('Thermal model', { exact: true }).fill('close mount glass glass')
  await form.getByRole('combobox', { name: 'Open circuit rise', exact: true }).selectOption('false')
  await form.getByRole('checkbox', { name: 'Use module parameters', exact: true }).uncheck()
  await expect(form.getByRole('button', { name: 'Review & run', exact: true })).toBeEnabled()
  const submitted = response(page, 'POST', '/api/run', data => ({ job_id: data.job_id, status: data.status }))
  await form.getByRole('button', { name: 'Review & run', exact: true }).click()
  const accepted = await submitted
  check(publicId(accepted.job_id) && accepted.status === 'submitted')
  return accepted
}

function sizingInput(binding) {
  return { project_id: binding.project_id, drawing_id: binding.drawing_id, input_version_id: binding.panels_output_version_id }
}

async function attachSizing(info, record) {
  await info.attach(SIZING_NAME, { body: Buffer.from(JSON.stringify(sizingRecord(record, record.scenario, metadata))), contentType: 'application/json' })
}

function successfulSizing(binding, job, values, scenario) {
  check(job.sizing_exchange?.job_id === job.job_id && isHash(job.sizing_exchange.evidence_sha256) &&
    values.panels_in_sequence === 27 && values.voc_cold?.passes === true && values.panel_count === 4 &&
    JSON.stringify(values.project_exchange) === JSON.stringify(job.sizing_exchange))
  return { scenario, organization_id: metadata.organization_id, ...sizingInput(binding), job_id: job.job_id,
    output_version_id: job.output_version_id, exchange_job_id: job.sizing_exchange.job_id,
    evidence_sha256: job.sizing_exchange.evidence_sha256, panels_in_sequence: values.panels_in_sequence }
}

test('project_sizing_recovery_reopen', async ({ browser }, info) => {
  await withIdentity(browser, async ({ page }) => {
    const binding = await prepareSizingProject(page, sizingNames.recovery)
    await checkpoint(info, 'recovery_settings_published')
    await checkpoint(info, 'recovery_panels_imported')
    const form = await openSizingForm(page, sizingInput(binding))
    const accepted = await submitGlobalSizing(page, form)
    await checkpoint(info, 'recovery_sizing_submitted')
    const job = await publishedGraphJob(page, sizingInput(binding), accepted.job_id, 'solar-size-strings', 150_000)
    check(job.sizing_exchange?.job_id === job.job_id && isHash(job.sizing_exchange.evidence_sha256))
    await checkpoint(info, 'recovery_sizing_published')
    const values = await readSizingValues(page, binding, job.output_version_id)
    const record = successfulSizing(binding, job, values, 'capture-recovery')
    check(values.frames.length === 0)
    await checkpoint(info, 'recovery_values_read')
    await releaseCheckout(page)
    await page.reload()
    await reopenSizingProject(page, sizingNames.recovery, job.output_version_id)
    await takeCheckout(page, binding)
    const reopened = await openSizingForm(page, { ...sizingInput(binding), input_version_id: job.output_version_id })
    await expect(reopened.getByText('Saved global string length: 27', { exact: true })).toBeVisible()
    expect(await readSizingValues(page, binding, job.output_version_id)).toEqual(values)
    await releaseCheckout(page)
    await checkpoint(info, 'recovery_exact_reopen')
    recovered = { binding, job, values }
    await attachSizing(info, record)
  })
})

test('project_sizing_read_custody', async ({ browser }, info) => {
  await withIdentity(browser, async ({ page }) => {
    check(!!recovered)
    const { binding, job, values } = recovered
    await reopenSizingProject(page, sizingNames.recovery, job.output_version_id)
    await takeCheckout(page, binding)
    const form = await openGraphForm(page, 'Panel groups', 'Panel groups')
    await form.getByLabel('Group 1 name', { exact: true }).fill('PP3B group 1')
    await form.getByLabel('Module width along row', { exact: true }).fill('1.134')
    await form.getByLabel('Module height across row', { exact: true }).fill('2.278')
    await form.getByLabel('Alignment tolerance', { exact: true }).fill('0.05')
    await expect(form.getByRole('button', { name: 'Review & run', exact: true })).toBeEnabled()
    const submitted = response(page, 'POST', '/api/run', data => ({ job_id: data.job_id }))
    await form.getByRole('button', { name: 'Review & run', exact: true }).click()
    const accepted = await submitted
    const groups = await publishedGraphJob(page, { ...sizingInput(binding), input_version_id: job.output_version_id }, accepted.job_id, 'solar-panel-groups')
    await checkpoint(info, 'custody_groups_published')
    const grouped = await readSizingValues(page, binding, groups.output_version_id)
    check(grouped.frames.length >= 1 && grouped.frames.every(frame => Number.isFinite(frame.module_power_watts) && frame.module_power_watts > 0))
    expect({ ...grouped, frames: [] }).toEqual(values)
    await checkpoint(info, 'custody_frame_power_checked')
    const read = await page.evaluate(async binding => {
      const client = window.__pp3bApi.createProjectSolarClient()
      try {
        client.setScope(binding, 'pp3b-custody-read')
        const catalog = await client.catalog()
        if (!catalog.ok) throw new Error('PP3B_READBACK')
        const row = catalog.data.families.flatMap(family => family.capabilities).find(row => row.name === 'solar-string-data')
        if (!row) throw new Error('PP3B_READBACK')
        const read = await client.readSolar({ tool: 'solar-string-data', params: {}, catalogDigest: row.catalog_digest, current: false })
        if (!read.ok) throw new Error('PP3B_READBACK')
        const result = read.data.result
        return { tool: result.tool, input_version_id: result.input_version_id, request_sha256: result.request_sha256, output_sha256: result.output_sha256 }
      } finally { client.dispose() }
    }, { kind: 'project', organization_id: metadata.organization_id, project_id: binding.project_id,
      drawing_id: binding.drawing_id, input_version_id: groups.output_version_id })
    check(read.tool === 'solar-string-data' && read.input_version_id === groups.output_version_id && isHash(read.request_sha256) && isHash(read.output_sha256))
    await checkpoint(info, 'custody_read_passed')
    await releaseCheckout(page)
    await attachSizing(info, { scenario: 'read-custody', organization_id: metadata.organization_id,
      project_id: binding.project_id, drawing_id: binding.drawing_id, source_version_id: job.output_version_id,
      groups_job_id: groups.job_id, input_version_id: groups.output_version_id, frame_count: grouped.frames.length,
      tool: read.tool, request_sha256: read.request_sha256, output_sha256: read.output_sha256 })
  })
})

function observeSizingSubmissions(page) {
  let firstBody, firstKey
  const state = { count: 0, same: true }
  const listener = request => {
    const url = new URL(request.url())
    if (![metadata.app_origin, metadata.web_origin].includes(url.origin) || url.pathname !== '/api/run' || request.method() !== 'POST') return
    const body = request.postData()
    if (JSON.parse(body)?.tool !== 'solar-size-strings') return
    const key = request.headers()['idempotency-key']
    check(typeof key === 'string' && key.length > 0)
    const bodyHash = createHash('sha256').update(body).digest('hex')
    const keyHash = createHash('sha256').update(key).digest('hex')
    if (state.count === 0) { firstBody = bodyHash; firstKey = keyHash }
    state.same &&= bodyHash === firstBody && keyHash === firstKey
    state.count += 1
  }
  page.on('request', listener)
  return { state, stop: () => page.off('request', listener) }
}

test('project_sizing_submission_unknown', async ({ browser }, info) => {
  await withIdentity(browser, async ({ page }) => {
    const binding = await prepareSizingProject(page, sizingNames.submission)
    await checkpoint(info, 'submission_settings_published')
    await checkpoint(info, 'submission_panels_imported')
    const form = await openSizingForm(page, sizingInput(binding))
    const seen = observeSizingSubmissions(page)
    try {
      const accepted = await submitGlobalSizing(page, form)
      check(seen.state.count === 2 && seen.state.same)
      await checkpoint(info, 'submission_retried_same_request')
      const job = await publishedGraphJob(page, sizingInput(binding), accepted.job_id, 'solar-size-strings', 150_000)
      const values = await readSizingValues(page, binding, job.output_version_id)
      const record = successfulSizing(binding, job, values, 'browser-response-discard')
      check(seen.state.count === 2 && seen.state.same)
      await checkpoint(info, 'submission_sizing_published')
      await releaseCheckout(page)
      await attachSizing(info, { ...record, submissions: 2, same_submission: true })
    } finally { seen.stop() }
  })
})

test('project_sizing_service_unknown', async ({ browser }, info) => {
  await withIdentity(browser, async ({ page }) => {
    const binding = await prepareSizingProject(page, sizingNames.service)
    await checkpoint(info, 'service_settings_published')
    await checkpoint(info, 'service_panels_imported')
    const before = (await snapshot(page, binding.project_id)).versions
    const form = await openSizingForm(page, sizingInput(binding))
    const accepted = await submitGlobalSizing(page, form)
    await checkpoint(info, 'service_sizing_submitted')
    await expect(page.getByText('The Solar run outcome is unknown. Check its job before submitting again.', { exact: true })).toBeVisible({ timeout: 150_000 })
    await checkpoint(info, 'service_outcome_unknown_shown')
    let job
    await expect.poll(async () => (job = await readSizingJob(page, sizingInput(binding), accepted.job_id))?.status,
      { timeout: 150_000 }).toBe('failed')
    check(job?.status === 'failed' && job.reason_code === 'SIZING_OUTCOME_UNKNOWN' && job.retryable === false && job.output_version_id === null)
    await checkpoint(info, 'service_job_terminal')
    await releaseCheckout(page)
    await page.reload()
    await reopenSizingProject(page, sizingNames.service, binding.panels_output_version_id)
    expect(await readSizingJob(page, sizingInput(binding), accepted.job_id)).toEqual(job)
    expect((await snapshot(page, binding.project_id)).versions).toEqual(before)
    await checkpoint(info, 'service_reobserved')
    await attachSizing(info, { scenario: 'service-response-drop', organization_id: metadata.organization_id,
      ...sizingInput(binding), job_id: job.job_id, status: job.status, reason_code: job.reason_code, retryable: job.retryable })
  })
})
