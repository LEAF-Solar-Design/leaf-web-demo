import { expect } from '@playwright/test'
import { mkdir, writeFile } from 'node:fs/promises'
import {
  allowedStagingOrigins, assertAllowedStagingHost, assertPageOnAllowedOrigin,
  assertResponseOnAllowedOrigin, assertStagingTokenIdentity, collectBearerLeaks,
} from '../staging/stagingConfig.mjs'
import { inspectContext, inspectJob, isUuid, projectBinding } from '../local/solarProjectWalk.mjs'
import {
  openProjectSolarTool, selectProjectSolarVersion, takeProjectSolarCheckout,
} from '../walk/fixtures.mjs'

const TOOL = 'solar-solve-proposal'
const code = value => typeof value === 'string' && /^[A-Za-z][A-Za-z0-9_]{0,127}$/.test(value) ? value : null

export function stagingNetworkPath(url) {
  const parsed = new URL(url)
  if (!/^\/api\/(?:projects|capabilities|tools|session|drawings|campaigns|jobs)(?:\/|$)/.test(parsed.pathname)) return null
  const keys = [...parsed.searchParams.keys()].map(key => encodeURIComponent(key))
  return parsed.pathname + (keys.length ? `?${keys.join('&')}` : '')
}

export async function stagingErrorCode(response) {
  if (response.status() < 400) return null
  try {
    const body = await response.json()
    return code(body?.error_code) ?? code(body?.error?.error_code)
  } catch { return null }
}

// Diagnostic observations must never affect the walk's security or UI verdict.
export function observeStagingNetwork(page, records, latest = {}) {
  const pending = new Set()
  const duration = request => {
    const timing = request.timing()
    if (!Number.isFinite(timing.startTime) || timing.startTime <= 0) return null
    const elapsed = timing.responseEnd >= 0 ? timing.responseEnd : Date.now() - timing.startTime
    return Number.isFinite(elapsed) && elapsed >= 0 && elapsed <= 600000 ? Math.round(elapsed) : null
  }
  const observe = (request, response) => {
    try {
      const path = stagingNetworkPath(request.url())
      if (!path) return
      const record = { method: request.method(), path, duration_ms: 0, error_code: null }
      if (response) record.status = response.status()
      else record.failure = String(request.failure()?.errorText ?? 'request failed').slice(0, 512)
      records.push(record)
      if (records.length > 200) records.splice(0, records.length - 200)
      const pathname = path.split('?')[0]
      if (/\/drawing-versions\/[^/]+\/context$/.test(pathname)) latest.context = record
      if (/^\/api\/capabilities\/?$/.test(pathname)) latest.capabilities = record
      let timer
      const observation = (async () => {
        if (response) {
          record.error_code = await stagingErrorCode(response)
          await response.finished()
        }
        record.duration_ms = duration(request)
      })().catch(() => {})
      // A broken response body must not hold up cleanup or turn a UI pass
      // into a timeout. All observations run concurrently within this bound.
      const work = Promise.race([observation, new Promise(resolve => { timer = setTimeout(resolve, 2000) })])
        .finally(() => clearTimeout(timer))
      pending.add(work)
      work.finally(() => pending.delete(work))
    } catch { /* Best effort only; never capture headers or body content. */ }
  }
  const onResponse = response => {
    try { observe(response.request(), response) } catch { /* Diagnostic only. */ }
  }
  const onFailed = request => observe(request, null)
  page.on('response', onResponse)
  page.on('requestfailed', onFailed)
  return async () => {
    page.off('response', onResponse)
    page.off('requestfailed', onFailed)
    while (pending.size) await Promise.all([...pending])
  }
}

export function stagingNetworkFailure(records, latest = {}) {
  const last = pattern => [...records].reverse().find(row => pattern.test(row.path.split('?')[0])) ?? null
  return `Staging network: ${JSON.stringify({
    context: latest.context ?? last(/\/drawing-versions\/[^/]+\/context$/),
    capabilities: latest.capabilities ?? last(/^\/api\/capabilities\/?$/),
  })}`
}

async function persistStagingEvidence(evidence, title) {
  const directory = new URL('../../artifacts/walk-staging/', import.meta.url)
  await mkdir(directory, { recursive: true })
  const name = title.replace(/[^A-Za-z0-9_-]/g, '_')
  await writeFile(new URL(`${name}.json`, directory), JSON.stringify(evidence, null, 2))
}

export async function stagingEvidenceStep(runtime, testInfo, action, persist = persistStagingEvidence) {
  const records = runtime.evidence.projectSolar.network = []
  const latest = runtime.evidence.projectSolar.networkLast = { context: null, capabilities: null }
  const finish = observeStagingNetwork(runtime.page, records, latest)
  let failure
  try {
    return await action()
  } catch (error) {
    failure = error
    throw error
  } finally {
    await finish()
    if (failure instanceof Error) failure.message += `\n${stagingNetworkFailure(records, latest)}`
    try {
      await persist(runtime.evidence, testInfo.title)
    } catch { /* Evidence IO must not replace the step's verdict. */ }
  }
}

export function refusalCode(body, fallback = 'run_refused') {
  return code(body?.error?.reason_code) ?? code(body?.detail?.error?.reason_code)
    ?? code(body?.detail?.reason_code) ?? code(body?.reason_code) ?? code(body?.reason) ?? fallback
}

// Persist only public readiness fields, never request params, headers or grants.
export function proposalCatalogState(body) {
  const rows = body?.families?.flatMap(family => family.capabilities ?? []) ?? body?.tools ?? []
  return rows.filter(row => row.name === TOOL).map(row => {
    const a = row.availability ?? {}
    return { name: TOOL, ...Object.fromEntries(['entitled', 'engine_ready', 'implemented', 'input_ready', 'runnable']
      .filter(key => typeof a[key] === 'boolean').map(key => [key, a[key]])),
    admission_status: a.admission?.status_code ?? null,
    reason_code: code(a.admission?.error?.reason_code),
    refusal_reasons: (a.refusal_reasons ?? []).map(code).filter(Boolean) }
  })
}

export function proposalFormValues(graph) {
  const frame = graph?.frames?.[0]
  if (!frame?.matrix?.length) return { frame: frame?.id ?? 'walk-staging-frame', request: { grid: {} } }
  // Wire shape and graph coordinates follow server/tests/test_w1_solve_commit.py
  // (case fixture) and server/tests/fixtures/w1_stringer_request_4x6.json.
  const panels = new Map(graph.panels.map(panel => [panel.id, panel]))
  const count = frame.matrix.flat().filter(cell => cell.panel_ref).length
  const length = Math.max(1, graph.settings?.panels_in_sequence ?? 1)
  return { frame: frame.id, request: { grid: {
    Dwgname: 'walk-staging', Modify: [],
    Sequences: [[length, count % length], [Math.floor(count / length), count % length ? 1 : 0]],
    Rows: frame.matrix.map(row => ({ Panels: row.map(cell => {
      const panel = panels.get(cell.panel_ref)
      return { Code: panel ? 1 : 0, Id: panel?.id ?? '', Seq: 0, InverterId: -1, StringInputNumber: 0,
        X: panel?.centre?.[0] ?? cell.x ?? 0, Y: panel?.centre?.[1] ?? cell.y ?? 0, Angle: panel?.angle ?? cell.angle ?? 0 }
    }) })),
  } } }
}

export function canonicalProjectVersion(workspace, projectId) {
  expect(workspace.project?.project_id).toBe(projectId)
  // Fixtures contain one drawing. Prefer its newest sequence, including a
  // published scratch head from an earlier invocation of this suite.
  const versions = workspace.drawing_versions ?? []
  expect(versions.length).toBeGreaterThan(0)
  expect(new Set(versions.map(row => row.drawing_id)).size).toBe(1)
  const version = [...versions].sort((a, b) => b.seq - a.seq)[0]
  expect(version.org_id).toBe(workspace.project.org_id)
  expect(version.project_id).toBe(projectId)
  return projectBinding({ organization_id: workspace.project.org_id, project_id: projectId,
    drawing_id: version.drawing_id, input_version_id: version.version_id })
}

export async function stagingAction(page, apiBase, method, path, action) {
  const origin = assertAllowedStagingHost(apiBase).origin
  assertPageOnAllowedOrigin(page)
  const wait = page.waitForResponse(response => {
    const url = new URL(response.url())
    return url.origin === origin && url.pathname === path && response.request().method() === method
  }, { timeout: 30_000 })
  const [response] = await Promise.all([wait, Promise.resolve().then(action)])
  assertResponseOnAllowedOrigin(response)
  return response
}

export async function stagingRead(page, apiBase, path, binding) {
  const origin = assertAllowedStagingHost(apiBase).origin
  assertPageOnAllowedOrigin(page)
  expect(path.startsWith('/api/') && !path.startsWith('//')).toBe(true)
  const result = await page.evaluate(async ({ origin, path, binding }) => {
    const response = await fetch(`${origin}${path}`, {
      method: 'GET', cache: 'no-store', redirect: 'error', signal: AbortSignal.timeout(15_000),
      headers: { Authorization: `Bearer ${localStorage.getItem('leaf.jwt')}`,
        'X-Org-Id': binding.organization_id, 'X-Project-Id': binding.project_id,
        'X-Tenant-Id': binding.organization_id },
    })
    return { url: response.url, status: response.status, body: await response.json() }
  }, { origin, path, binding })
  assertResponseOnAllowedOrigin({ url: () => result.url })
  expect(result.status).toBe(200)
  return result.body
}

export async function stagingContext(page, apiBase, binding, graphRequired = false) {
  return inspectContext(await stagingRead(page, apiBase,
    `/api/projects/${binding.project_id}/drawing-versions/${binding.input_version_id}/context?drawing_id=${binding.drawing_id}`,
    binding), binding, { graphRequired })
}

export async function stagingRun({ page, expect: assertions, apiBase, binding, before, tool, action, checkParams }) {
  const response = await stagingAction(page, apiBase, 'POST', '/api/run', action)
  const request = response.request(), body = request.postDataJSON(), headers = request.headers()
  assertions(body.tool).toBe(tool)
  assertions(body.dwg).toBe(binding.input_version_id)
  assertions(body.params.drawing_id).toBe(binding.drawing_id)
  assertions(body.params.expected_rev).toBe(before.graph?.rev ?? 0)
  assertions(headers['x-org-id']).toBe(binding.organization_id)
  assertions(headers['x-project-id']).toBe(binding.project_id)
  // Assert presence without putting private header values in a failed matcher.
  assertions(/^Bearer \S+$/.test(headers.authorization ?? '')).toBe(true)
  assertions(Boolean(headers['x-checkout-capability'])).toBe(true)
  assertions(Boolean(headers['idempotency-key'])).toBe(true)
  checkParams?.(body.params)
  assertions(response.status()).toBe(202)
  const accepted = await response.json()
  assertions(isUuid(accepted.job_id)).toBe(true)
  let output
  await assertions.poll(async () => {
    const job = await stagingRead(page, apiBase, `/api/jobs/${accepted.job_id}`, binding)
    output = inspectJob(job, binding, accepted.job_id, tool)
    return output !== null
  }, { timeout: 90_000, intervals: [250, 500, 1000], message: 'staging settings job must complete and publish' }).toBe(true)
  const next = projectBinding({ ...binding, input_version_id: output })
  const context = await stagingContext(page, apiBase, next, true)
  if (before.graph) assertions(context.graph.rev).toBe(before.graph.rev + 1)
  return { binding: next, context, step: { tool, job_id: accepted.job_id,
    input_version_id: binding.input_version_id, output_version_id: output, run_status: 202 } }
}

export function monitorStagingSolar(page, solar) {
  const leaks = collectBearerLeaks(page)
  const responseErrors = [], pending = new Set()
  const onResponse = response => {
    try {
      if (response.request().headers().authorization) assertResponseOnAllowedOrigin(response)
    } catch {
      responseErrors.push('bearer response origin check failed')
      return
    }
    const work = (async () => {
      let timer
      try {
        const path = new URL(response.url()).pathname
        if (['/api/capabilities', '/api/tools'].includes(path) && response.ok()) {
          const body = await Promise.race([
            Promise.resolve().then(() => response.json()),
            new Promise(resolve => { timer = setTimeout(resolve, 2000) }),
          ])
          const rows = proposalCatalogState(body)
          if (rows.length) solar.catalog = rows
        }
      } catch { /* Catalog observations are diagnostic only. */ }
      finally {
        clearTimeout(timer)
      }
    })()
    pending.add(work)
    work.finally(() => pending.delete(work))
  }
  const onRequest = request => {
    if (request.method() === 'POST' && new URL(request.url()).pathname === '/api/run') solar.traffic.runCount += 1
  }
  page.on('response', onResponse)
  page.on('request', onRequest)
  return async () => {
    page.off('response', onResponse)
    page.off('request', onRequest)
    while (pending.size) await Promise.all([...pending])
    if (page.url() !== 'about:blank') assertPageOnAllowedOrigin(page)
    expect(responseErrors).toEqual([])
    expect(leaks).toEqual([])
  }
}

export async function withStagingProject(page, baseURL, projectId, token, testInfo, action) {
  assertAllowedStagingHost(baseURL)
  assertStagingTokenIdentity(token, process.env)
  const runtime = { page, stack: { baseURL }, cleanup: [], evidence: { projectSolar: { steps: [] } },
    projectSolar: { apiBase: baseURL, traffic: { runCount: 0 }, catalog: [] } }
  const finish = monitorStagingSolar(page, runtime.projectSolar)
  return stagingEvidenceStep(runtime, testInfo, async () => {
  try {
    await page.addInitScript(({ token, allowedOrigins }) => {
      if (allowedOrigins.includes(location.origin.toLowerCase())) localStorage.setItem('leaf.jwt', token)
    }, { token, allowedOrigins: [...allowedStagingOrigins()] })
    const projectsLoaded = page.waitForResponse(response => new URL(response.url()).pathname === '/api/projects'
      && response.request().method() === 'GET' && response.ok())
    await page.goto('/app?surface=browser', { waitUntil: 'domcontentloaded' })
    assertPageOnAllowedOrigin(page)
    const projects = (await (await projectsLoaded).json()).projects
    const project = projects.find(row => row.project_id === projectId)
    expect(Boolean(project)).toBe(true)
    expect(projects.filter(row => row.name === project.name)).toHaveLength(1)
    const response = await stagingAction(page, baseURL, 'GET', `/api/projects/${projectId}`,
      () => page.getByRole('region', { name: 'Workspace projects', exact: true })
        .getByRole('button', { name: project.name, exact: true }).click())
    expect(response.status()).toBe(200)
    runtime.projectSolar.binding = canonicalProjectVersion(await response.json(), projectId)
    await selectProjectSolarVersion(runtime)
    runtime.projectSolar.context = await stagingContext(page, baseURL, runtime.projectSolar.binding)
    testInfo.annotations.push({ type: 'project-solar-binding', description: JSON.stringify(runtime.projectSolar.binding) })
    await action(runtime)
  } finally {
    try {
      // Run all cleanups even when a release assertion fails.
      const errors = []
      for (const cleanup of runtime.cleanup.reverse()) {
        try { await cleanup() } catch (error) { errors.push(error) }
      }
      if (errors.length) throw errors[0]
    } finally {
      await finish()
    }
  }
  })
}

export async function reviewStagingProposal(runtime, {
  assertions = expect, open = openProjectSolarTool, checkout = takeProjectSolarCheckout, read = stagingRead,
} = {}) {
  const { page, projectSolar: solar } = runtime
  const refused = reason => ({ reason, catalog: solar.catalog })
  const opened = await open(runtime, TOOL, assertions, { allowDisabled: true })
  if (!opened) return refused('run_solve_disabled')
  const form = page.getByRole('form', { name: 'Solve', exact: true })
  await assertions(form).toBeVisible()
  await checkout(runtime, assertions, stagingAction)
  const frame = form.getByLabel('Frame', { exact: true })
  await assertions(frame).toBeEnabled({ timeout: 10_000 }).catch(() => {})
  if (!await frame.isEnabled()) return refused('run_solve_disabled')
  // These are references, never cloud credentials. The server resolves the grant.
  // w1-staging is also used by server/tests/test_w1_cloud_solve_probe.py.
  const values = proposalFormValues(solar.context.graph)
  await form.getByLabel('Grant ref', { exact: true }).fill('w1-staging')
  await form.getByLabel('Request', { exact: true }).fill(JSON.stringify(values.request))
  await frame.fill(values.frame)
  await form.getByRole('combobox', { name: 'Phase', exact: true }).selectOption('0')
  const start = form.getByRole('button', { name: 'Run solve', exact: true })
  // Give a loading/locked form time to settle without turning refusal into failure.
  await assertions(start).toBeEnabled({ timeout: 10_000 }).catch(() => {})
  if (!await start.isEnabled()) return refused('run_solve_disabled')
  // Submission can be refused by the UI's preflight before POST /api/run.
  let runResponse = null
  const observe = response => {
    if (new URL(response.url()).pathname === '/api/run' && response.request().method() === 'POST') runResponse = response
  }
  page.on('response', observe)
  try {
    await start.click()
    await assertions.poll(async () => runResponse !== null ||
      /The server refused this Solar request\./.test(await form.getByRole('status').allTextContents().then(rows => rows.join(' '))),
    { timeout: 30_000, message: 'Solve must receive a run response or a refusal reason' }).toBe(true)
    if (!runResponse) {
      const text = (await form.getByRole('status').allTextContents()).join(' ')
      return refused(text.match(/\(([A-Za-z][A-Za-z0-9_]{0,127})\)/)?.[1] ?? 'run_refused')
    }
    assertResponseOnAllowedOrigin(runResponse)
    if (runResponse.status() !== 202) {
      const body = await runResponse.json().catch(() => null)
      return refused(refusalCode(body, `run_http_${runResponse.status()}`))
    }
    const accepted = await runResponse.json()
    assertions(isUuid(accepted.job_id)).toBe(true)
    let job
    await assertions.poll(async () => {
      job = await read(page, solar.apiBase, `/api/jobs/${accepted.job_id}`, solar.binding)
      assertions(job.job_id).toBe(accepted.job_id)
      assertions(job.org_id).toBe(solar.binding.organization_id)
      assertions(job.project_id).toBe(solar.binding.project_id)
      assertions(job.input_version_id).toBe(solar.binding.input_version_id)
      assertions(job.tool_name).toBe(TOOL)
      return ['complete', 'failed', 'refused', 'cancelled'].includes(job.status)
    }, { timeout: 90_000, intervals: [250, 500, 1000] }).toBe(true)
    if (job.status !== 'complete') return refused(refusalCode(job, `solve_job_${job.status}`))
    await assertions(page.getByRole('button', { name: 'Commit proposal', exact: true })).toBeVisible({ timeout: 30_000 })
    return { job_id: accepted.job_id, reviewVisible: true }
  } finally { page.off('response', observe) }
}
