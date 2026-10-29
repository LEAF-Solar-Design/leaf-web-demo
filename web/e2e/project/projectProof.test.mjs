// @vitest-environment node
import { it, expect, vi } from 'vitest'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { CHECKPOINTS, REQUIRED_TEST_IDS, createProjectIdentity, disposeIdentity, installIdentity,
  removeBearer, validateMetadata } from './projectIdentity.mjs'
import Reporter, { EVENT_NAME, CANONICAL_NAME, MAX_RECEIPT_BYTES, BROWSER_REPORT_SCHEMA } from './projectProofReporter.mjs'
import { createProjectApiBundle, injectProjectApi, MAX_BUNDLE_BYTES, BUILD_TIMEOUT_MS } from './projectApiBundle.mjs'
import { environmentDefinitions } from '../../scripts/next-runtime.mjs'

const source = file => readFileSync(new URL(file, import.meta.url), 'utf8')
const metadata = () => ({ profile: 'project', run_id: 'pp3b-unit-0001', web_origin: 'http://127.0.0.1:5185',
  app_origin: 'http://127.0.0.1:8010', organization_id: 'org-1', binding_id: 'binding-1', worker_id: 'project:pp3b-unit-0001',
  public_key_thumbprint: 'public-thumbprint', required_test_ids: [...REQUIRED_TEST_IDS], source_binding: {
    commit: 'a'.repeat(40), dirty_tree_sha256: 'b'.repeat(64), tested_source_manifest_sha256: 'c'.repeat(64),
    files: [{ path: 'web/src/api.js', sha256: 'd'.repeat(64), size: 42 }], declared_untracked: [], expected_migrations: [] } })
const environment = () => ({ LEAF_PROJECT_REQUEST: JSON.stringify(metadata()), LEAF_PROJECT_BASE_URL: metadata().web_origin,
  LEAF_E2E_MANAGED: '1', LEAF_PROOF_PROJECT_RUN_ID: metadata().run_id,
  LEAF_PROJECT_OUTPUT_DIR: fileURLToPath(new URL('./fake-output', import.meta.url)),
  LEAF_PROJECT_PROOF_OUTPUT: fileURLToPath(new URL('./fake-result.json', import.meta.url)) })
async function config(env = environment()) {
  for (const [key, value] of Object.entries(env)) vi.stubEnv(key, value)
  try { vi.resetModules(); return (await import('../../playwright.project.config.mjs')).projectConfig(env) }
  finally { vi.unstubAllEnvs() }
}
const storage = () => {
  const map = new Map()
  return { getItem: key => map.get(key) ?? null, setItem: (key, value) => map.set(key, value),
    removeItem: key => map.delete(key), clear: () => map.clear() }
}
function document(origin = metadata().web_origin, topLevel = true) {
  const target = { location: { origin }, localStorage: storage(), sessionStorage: storage() }
  target.top = topLevel ? target : {}
  return target
}
function fakeBrowser() {
  const contexts = []
  const forbidden = () => { throw new Error('forbidden transport override') }
  return { contexts, newContext: vi.fn(async options => {
    const target = document()
    const scripts = []
    const page = { setExtraHTTPHeaders: forbidden, route: forbidden, target,
      addInitScript: vi.fn(async (fn, arg) => scripts.push([fn, arg])),
      evaluate: vi.fn(async (fn, arg) => fn(arg, target)),
      initialize: () => scripts.forEach(([fn, arg]) => fn(arg, target)) }
    const context = { options, page, setExtraHTTPHeaders: forbidden, route: forbidden,
      pages: () => [page], newPage: vi.fn(async () => page), clearCookies: vi.fn(async () => {}), close: vi.fn(async () => {}) }
    contexts.push(context)
    return context
  }) }
}
const token = 'header.payload.signature'
const canonical = () => ({ organization_id: 'org-1', project_id: 'project-1', drawing_id: 'drawing-1',
  input_version_id: 'version-1', output_version_id: 'version-2', job_id: 'job-1', binding_id: 'binding-1',
  worker_id: metadata().worker_id, checkout_fence: 1, before_settings_sha256: '1'.repeat(64),
  settings_sha256: '2'.repeat(64), reopened_settings_sha256: '2'.repeat(64) })
const attachment = (name, value) => ({ name, contentType: 'application/json', body: Buffer.from(JSON.stringify(value)) })
function fakeReporter() {
  const files = new Map()
  const outputFile = environment().LEAF_PROJECT_PROOF_OUTPUT
  const write = vi.fn((path, bytes, options) => {
    if (options.flag === 'wx' && files.has(path)) throw Object.assign(new Error('private existing-file canary'), { code: 'EEXIST' })
    files.set(path, bytes)
  })
  const reporter = new Reporter({ metadata: metadata(), outputFile, write })
  return { reporter, files, write, outputFile, json: () => JSON.parse(files.get(outputFile)) }
}
function drive(reporter, change = () => {}) {
  const tests = REQUIRED_TEST_IDS.map(title => ({ title, annotations: [] }))
  reporter.onBegin({ workers: 1, forbidOnly: true, shard: null, projects: [{ name: 'project', retries: 0 }] }, { allTests: () => tests })
  tests.forEach((test, index) => {
    reporter.onTestBegin(test)
    const keys = CHECKPOINTS.slice(index === 0 ? 0 : index === 1 ? 4 : 7, index === 0 ? 4 : index === 1 ? 7 : 12)
    const result = { status: 'passed', retry: 0, errors: [], attachments: keys.map(key => attachment(EVENT_NAME, { key, value: true })) }
    if (index === 2) result.attachments.push(attachment(CANONICAL_NAME, canonical()))
    change(test, result, index)
    reporter.onTestEnd(test, result)
  })
  return reporter.onEnd({ status: 'passed' })
}
const python = () => source('../../../scripts/proof_project.py')
function tuple(name) {
  const expression = python().match(new RegExp(`^${name} = \\(([\\s\\S]*?)\\)`, 'm'))?.[1]
  if (!expression) throw new Error('missing Python manifest')
  return [...expression.matchAll(/"([^"]+)"/g)].map(match => match[1])
}
function fields(name) {
  const block = python().match(new RegExp(`^class ${name}:\\n([\\s\\S]*?)(?=\\n\\n(?:@|class ))`, 'm'))?.[1]
  return [...block.matchAll(/^    ([a-z_][a-z_0-9]*):/gm)].map(match => match[1])
}
function acceptsBrowserResult(result, request) {
  // JS fixture mirrors the dataclass boundary: Python's JSON adapter must reconstruct tuples/dataclasses.
  expect(result.schema).toBe(BROWSER_REPORT_SCHEMA)
  expect(python()).toContain(`BROWSER_REPORT_SCHEMA = "${BROWSER_REPORT_SCHEMA}"`)
  expect(Object.keys(result).sort()).toEqual(['schema', ...fields('BrowserResult')].sort())
  expect(result.run_id).toBe(request.run_id)
  expect(result.source_binding).toEqual(request.source_binding)
  expect(Object.keys(result.source_binding).sort()).toEqual(fields('SourceProof').sort())
  expect(result.public_key_thumbprint).toBe(request.public_key_thumbprint)
  expect(result.exit_code).toBe(0)
  for (const key of ['selected_test_ids', 'started_test_ids', 'completed_test_ids']) {
    expect(result[key]).toHaveLength(3)
    expect([...result[key]].sort()).toEqual([...request.required_test_ids].sort())
  }
  expect(result.outcomes).toHaveLength(3)
  expect(result.outcomes.map(o => o.test_id).sort()).toEqual([...request.required_test_ids].sort())
  for (const outcome of result.outcomes) {
    expect(Object.keys(outcome).sort()).toEqual(fields('TestOutcome').sort())
    expect(outcome.status).toBe('passed'); expect(outcome.retry).toBe(0)
  }
  expect(result.annotation_types).toEqual([])
  expect(result.checkpoints).toEqual(Object.fromEntries(tuple('CHECKPOINTS').map(key => [key, true])))
  const c = result.canonical
  expect(Object.keys(c).sort()).toEqual(fields('CanonicalEvidence').sort())
  for (const key of ['organization_id', 'binding_id', 'worker_id']) expect(c[key]).toBe(request[key])
  for (const key of fields('CanonicalEvidence').slice(0, 8)) expect(c[key]).toMatch(/^[A-Za-z0-9:|_-]{1,128}$/)
  expect(Number.isSafeInteger(c.checkout_fence) && c.checkout_fence >= 1).toBe(true)
  expect(c.input_version_id).not.toBe(c.output_version_id)
  for (const key of fields('CanonicalEvidence').slice(9)) expect(c[key]).toMatch(/^[a-f0-9]{64}$/)
  expect(c.before_settings_sha256).not.toBe(c.settings_sha256)
  expect(c.settings_sha256).toBe(c.reopened_settings_sha256)
}

it('PP3B-01 config selects only the one spec and the named project', async () => {
  const value = await config()
  expect(value.testDir).toBe('./e2e')
  expect(value.projects).toEqual([{ name: 'project', testMatch: '**/project/project-authority.spec.mjs' }])
  expect(value.webServer).toBeUndefined()
  expect(value.shard).toBeNull()
  expect(value.passWithNoTests).toBe(false)
  expect(value.outputDir).toBe(environment().LEAF_PROJECT_OUTPUT_DIR)
  expect(value.reporter.map(r => r[0])).toEqual(['./e2e/project/projectProofReporter.mjs', 'list'])
  const defaultConfig = source('../../playwright.config.mjs')
  expect(defaultConfig).toContain("testIgnore: ['**/project/**']")
})

it('PP3B-02 config refuses a missing or non-loopback origin', async () => {
  for (const origin of ['', 'http://localhost:5185', 'https://127.0.0.1:5185', 'http://127.0.0.1:5185/',
    'http://127.0.0.1:5185/evil', 'http://127.0.0.1:5185?x=1', 'http://127.0.0.1:0', 'http://127.0.0.1:65536',
    'http://127.0.0.1:05185', 'http://user@127.0.0.1:5185', 'http://127.0.0.2:5185']) {
    await expect(config({ ...environment(), LEAF_PROJECT_BASE_URL: origin })).rejects.toThrow('PP3B_')
  }
  await expect(config({ ...environment(), LEAF_PROJECT_OUTPUT_DIR: '' })).rejects.toThrow('PP3B_OUTPUT')
  await expect(config({ ...environment(), LEAF_PROJECT_PROOF_OUTPUT: '' })).rejects.toThrow('PP3B_OUTPUT')
  await expect(config({ ...environment(), LEAF_E2E_MANAGED: '0' })).rejects.toThrow('PP3B_METADATA')
  await expect(config({ ...environment(), LEAF_PROOF_PROJECT_RUN_ID: 'wrong-run' })).rejects.toThrow('PP3B_METADATA')
  for (const patch of [{ profile: 'account' }, { run_id: '' }, { run_id: undefined, worker_id: 'project:undefined' }, { required_test_ids: [] }, { worker_id: 'wrong' },
    { source_binding: {} }, { public_key_thumbprint: 'bad value' }, { app_origin: 'http://localhost:8010' }]) {
    expect(() => validateMetadata({ ...metadata(), ...patch })).toThrow('PP3B_METADATA')
  }
})

it('PP3B-03 config has retries 0, workers 1, forbidOnly, screenshot/video/trace off', async () => {
  const value = await config()
  expect(value.retries).toBe(0); expect(value.workers).toBe(1); expect(value.forbidOnly).toBe(true)
  expect(value.use.screenshot).toBe('off'); expect(value.use.video).toBe('off'); expect(value.use.trace).toBe('off')
  expect(value.quiet).toBe(true); expect(value.preserveOutput).toBe('never')
})

it('PP3B-04 identity installs both keys before app init on the exact origin only', async () => {
  const browser = fakeBrowser()
  const identity = await createProjectIdentity(browser, metadata(), token)
  expect(identity.page.addInitScript).toHaveBeenCalledOnce()
  expect(identity.page.target.localStorage.getItem('leaf.jwt')).toBeNull()
  identity.page.initialize()
  expect(identity.page.target.localStorage.getItem('leaf.jwt')).toBe(token)
  expect(identity.page.target.localStorage.getItem('leaf.org_id')).toBe(metadata().organization_id)
  const args = { origin: metadata().web_origin, bearer: token, organizationId: 'org-1' }
  for (const target of [document('http://localhost:5185'), document(metadata().app_origin), document(metadata().web_origin, false)]) {
    installIdentity(args, target)
    expect(target.localStorage.getItem('leaf.jwt')).toBeNull()
    expect(target.localStorage.getItem('leaf.org_id')).toBeNull()
  }
  const second = await createProjectIdentity(browser, metadata(), token)
  expect(second.context).not.toBe(identity.context)
  expect(browser.newContext).toHaveBeenCalledTimes(2)
  await expect(createProjectIdentity(browser, metadata(), '')).rejects.toThrow('PP3B_METADATA')
})

it('PP3B-05 identity never calls setExtraHTTPHeaders or route', async () => {
  const browser = fakeBrowser()
  const identity = await createProjectIdentity(browser, metadata(), token)
  identity.page.initialize()
  await removeBearer(identity)
  await disposeIdentity(identity)
  expect(identity.context.close).toHaveBeenCalledOnce()
  expect(source('./projectIdentity.mjs')).not.toMatch(/\.\s*(?:setExtraHTTPHeaders|route)\s*\(/)
  expect(identity.context.options).toEqual({ baseURL: metadata().web_origin, serviceWorkers: 'block' })
})

it('PP3B-06 bearer removal is not restored by an init script', async () => {
  const identity = await createProjectIdentity(fakeBrowser(), metadata(), token)
  identity.page.initialize()
  await removeBearer(identity)
  identity.page.initialize(); identity.page.initialize()
  expect(identity.page.target.localStorage.getItem('leaf.jwt')).toBeNull()
  expect(identity.page.target.localStorage.getItem('leaf.org_id')).toBe('org-1')
})

it('PP3B-07 teardown clears identity and closes the context', async () => {
  const identity = await createProjectIdentity(fakeBrowser(), metadata(), token)
  identity.page.initialize()
  await disposeIdentity(identity)
  expect(identity.page.target.localStorage.getItem('leaf.jwt')).toBeNull()
  expect(identity.page.target.localStorage.getItem('leaf.org_id')).toBeNull()
  expect(identity.page.target.sessionStorage.getItem('leaf.pp3b.identity-installed')).toBeNull()
  expect(identity.context.clearCookies).toHaveBeenCalledOnce()
  expect(identity.context.close).toHaveBeenCalledOnce()
  const failed = await createProjectIdentity(fakeBrowser(), metadata(), token)
  failed.page.evaluate.mockRejectedValue(new Error('private failure'))
  await expect(disposeIdentity(failed)).rejects.toThrow('PP3B_TEARDOWN')
  expect(failed.context.close).toHaveBeenCalledOnce()
})

it('PP3B-08 reporter records the three id lists and one outcome per test', () => {
  const h = fakeReporter()
  expect(drive(h.reporter)).toEqual({ status: 'passed' })
  for (const key of ['selected_test_ids', 'started_test_ids', 'completed_test_ids']) expect(h.json()[key]).toEqual(REQUIRED_TEST_IDS)
  expect(h.json().outcomes).toEqual(REQUIRED_TEST_IDS.map(test_id => ({ test_id, status: 'passed', retry: 0 })))
  const zero = fakeReporter(); expect(zero.reporter.onEnd({ status: 'passed' })).toEqual({ status: 'failed' })
  const write = vi.fn()
  const list = new Reporter({ metadata: metadata(), outputFile: h.outputFile, write, _mode: 'list' })
  expect(list.onEnd({ status: 'passed' })).toEqual({ status: 'failed' })
  list.onBegin({ workers: 1, forbidOnly: true, shard: null, projects: [{ name: 'project', retries: 0 }] },
    { allTests: () => REQUIRED_TEST_IDS.map(title => ({ title, annotations: [] })) })
  expect(list.onEnd({ status: 'passed' })).toEqual({ status: 'passed' })
  expect(write).not.toHaveBeenCalled()
  for (const status of ['failed', 'timedOut', 'skipped', 'interrupted']) {
    const invalid = fakeReporter()
    expect(drive(invalid.reporter, (t, r, i) => { if (!i) r.status = status })).toEqual({ status: 'failed' })
  }
  const retry = fakeReporter()
  expect(drive(retry.reporter, (t, r) => { r.retry = 1 })).toEqual({ status: 'failed' })
  const duplicate = fakeReporter()
  duplicate.reporter.onTestBegin({ title: REQUIRED_TEST_IDS[0] })
  expect(drive(duplicate.reporter)).toEqual({ status: 'failed' })
})

it('PP3B-09 reporter records checkpoint events and keeps annotation types empty', () => {
  const h = fakeReporter(); drive(h.reporter)
  expect(h.json().checkpoints).toEqual(Object.fromEntries(CHECKPOINTS.map(key => [key, true])))
  expect(h.json().annotation_types).toEqual([])
  const missing = fakeReporter()
  expect(drive(missing.reporter, (t, r, i) => { if (!i) r.attachments = [] })).toEqual({ status: 'failed' })
  const annotated = fakeReporter()
  expect(drive(annotated.reporter, t => { t.annotations = [{ type: 'skip', description: 'unavailable' }] })).toEqual({ status: 'failed' })
  expect(annotated.json().annotation_types).toEqual(['skip'])
  const duplicate = fakeReporter()
  expect(drive(duplicate.reporter, (t, r, i) => { if (!i) r.attachments.push(r.attachments[0]) })).toEqual({ status: 'failed' })
})

it('PP3B-10 reporter writes no header, body, token or DSN value including split chunks', () => {
  const canaries = ['HEADER_SECRET_CANARY', 'BODY_SECRET_CANARY', 'BEARER_SECRET_CANARY',
    'postgresql://proof:DSN_SECRET_CANARY@private/db', 'lco1.' + 'e'.repeat(64)]
  const h = fakeReporter()
  const observed = []
  for (const canary of canaries) {
    const middle = Math.floor(canary.length / 2)
    h.reporter.onStdOut(Buffer.from(canary.slice(0, middle)))
    h.reporter.onStdOut(Buffer.from(canary.slice(middle)))
    h.reporter.onStdErr(canary.slice(0, middle)); h.reporter.onStdErr(canary.slice(middle))
  }
  drive(h.reporter, (test, result) => {
    observed.push({ test, result })
    result[Symbol('list buffered output')] = canaries
    result.headers = { authorization: canaries[0] }; result.body = canaries[1]
    result.errors = [{ message: canaries.join(' '), stack: canaries.join(' ') }]
    result.stdout = canaries; result.stderr = canaries
    result.attachments.push({ name: 'raw', body: Buffer.from(canaries.join(' ')) })
    test.annotations = [{ type: canaries[2], description: canaries[3] }]
  })
  const output = h.files.get(h.outputFile)
  for (const canary of canaries) expect(output).not.toContain(canary)
  expect(output).not.toMatch(/SECRET|postgresql|authorization|lco1\./)
  for (const { test, result } of observed) {
    expect(Object.getOwnPropertySymbols(result)).toEqual([])
    expect(JSON.stringify([test.annotations, result.errors, result.stdout, result.stderr, result.attachments])).not.toContain('SECRET')
  }
  expect(Buffer.byteLength(output)).toBeLessThanOrEqual(MAX_RECEIPT_BYTES)
  const error = { message: canaries[0], stack: canaries[1], snippet: canaries[2] }
  h.reporter.onError(error)
  expect(JSON.stringify(error)).not.toContain('SECRET')
  const large = metadata()
  large.source_binding.files = Array.from({ length: 20_000 }, (_, i) => ({
    path: `web/${'a'.repeat(480)}-${i}.js`, sha256: 'd'.repeat(64), size: 42,
  }))
  const write = vi.fn()
  const oversized = new Reporter({ metadata: large, outputFile: h.outputFile, write })
  expect(() => drive(oversized)).toThrow('PP3B_OUTPUT_LIMIT')
  expect(write).not.toHaveBeenCalled()
})

it('PP3B-11 reporter refuses an existing output file', () => {
  const h = fakeReporter(); h.files.set(h.outputFile, 'existing receipt')
  expect(() => drive(h.reporter)).toThrow('PP3B_OUTPUT_REFUSED')
  expect(h.files.get(h.outputFile)).toBe('existing receipt')
  expect(h.write.mock.calls[0][2]).toEqual({ flag: 'wx', mode: 0o600 })
})

it('PP3B-12 reporter output carries every field the Python BrowserResult contract reads', () => {
  const h = fakeReporter(); drive(h.reporter)
  acceptsBrowserResult(h.json(), metadata())
  expect(() => acceptsBrowserResult({ ...h.json(), run_id: 'wrong-run' }, metadata())).toThrow()
  expect(() => acceptsBrowserResult({ ...h.json(), source_binding: {} }, metadata())).toThrow()
})

it('PP3B-13 spec names exactly the three REQUIRED_TEST_IDS and every CHECKPOINTS key', () => {
  const spec = source('./project-authority.spec.mjs')
  expect([...spec.matchAll(/^test\('([^']+)'/gm)].map(match => match[1])).toEqual(tuple('REQUIRED_TEST_IDS'))
  expect([...spec.matchAll(/await checkpoint\(info, '([^']+)'\)/g)].map(match => match[1])).toEqual(tuple('CHECKPOINTS'))
  expect([...REQUIRED_TEST_IDS]).toEqual(tuple('REQUIRED_TEST_IDS'))
  expect([...CHECKPOINTS]).toEqual(tuple('CHECKPOINTS'))
  expect(spec).not.toMatch(/\.annotations|\.screenshot\s*\(|\.tracing\b|\.video\b/)
})

it('PP3B-14 spec never imports a mock transport or calls page.route or setExtraHTTPHeaders', () => {
  const spec = source('./project-authority.spec.mjs')
  const imports = [...spec.matchAll(/(?:from\s+|import\s*\(\s*)['"]([^'"]+)['"]/g)].map(match => match[1])
  expect(imports.length).toBeGreaterThan(0)
  expect(imports.every(path => ['@playwright/test', 'node:crypto', 'node:url', './projectIdentity.mjs',
    './projectProofReporter.mjs', './projectApiBundle.mjs'].includes(path))).toBe(true)
  expect(spec).not.toMatch(/import\s*\(\s*['"]\/src\//)
  const transport = spec + source('./projectApiBundle.mjs')
  expect(transport).not.toMatch(/\.\s*(?:route|setExtraHTTPHeaders)\s*\(/)
  expect(transport).not.toMatch(/fetchImpl\s*:|authHeaders\s*:|routeFromHAR|APIRequestContext|request\.(?:post|get|put|delete)\(/)
  expect(transport).not.toMatch(/(?:window\.|globalThis\.)?fetch\s*=/)
})

const fakeBundle = () => ({ outputFiles: [{ text: 'var __pp3bApi = {};' }] })

it('PP3B-15 bundle uses the running app environmentDefinitions with production mode', async () => {
  const env = { VITE_MOCK: '0', VITE_API_BASE: '/api', VITE_TENANT_ID: 'org-test',
    VITE_SOLAR_SETTINGS_FORM: '1', VITE_STARTUP_FETCH_TIMEOUT_MS: '1234',
    VITE_NOT_PUBLIC: 'private-canary', LEAF_E2E_TOKEN: 'token-canary' }
  const build = vi.fn(async () => fakeBundle())
  await createProjectApiBundle(env, build)()
  expect(build.mock.calls[0][0].define).toEqual(environmentDefinitions(env, false))
  expect(JSON.stringify(build.mock.calls[0][0].define)).not.toMatch(/private-canary|token-canary/)
})

it('PP3B-16 bundle entry is exactly web/src/api.js and browser IIFE options are fixed', async () => {
  const build = vi.fn(async () => fakeBundle())
  await createProjectApiBundle({}, build)()
  expect(build.mock.calls[0][0]).toEqual({
    entryPoints: [fileURLToPath(new URL('../../src/api.js', import.meta.url))],
    globalName: '__pp3bApi', bundle: true, write: false, platform: 'browser', format: 'iife',
    define: environmentDefinitions({}, false), logLevel: 'silent',
  })
})

it('PP3B-17 injected global is read only after managed-origin navigation and reinjection on reload', async () => {
  const spec = source('./project-authority.spec.mjs')
  const navigation = spec.match(/async function navigateProject\(page\) \{[\s\S]*?\n\}/)?.[0]
  expect(navigation).toBeTruthy()
  const events = []
  const target = {}
  let url = 'about:blank'
  const page = { url: () => url,
    goto: vi.fn(async next => { await Promise.resolve(); url = next; delete target.__pp3bApi; events.push('navigate') }),
    evaluate: (fn, arg) => fn(arg),
    addScriptTag: vi.fn(async ({ content }) => {
      expect(content).toBe('bundle')
      events.push('inject')
      target.__pp3bApi = {
        openProject: async () => { events.push('read'); return { project: { org_id: 'org-1', project_id: 'project-1' }, drawing_versions: [], jobs: [] } },
        listProjects: async () => [],
      }
    }) }
  const navigate = new Function('metadata', 'injectProjectApi', 'projectApiBundle',
    `${navigation}; return navigateProject`)(metadata(), injectProjectApi, async () => 'bundle')
  const snapshotSource = spec.match(/async function snapshot\(page, projectId\) \{[\s\S]*?\n\}/)?.[0]
  expect(snapshotSource).toBeTruthy()
  const snapshot = new Function('metadata', 'window', `${snapshotSource}; return snapshot`)(metadata(), target)
  await expect(snapshot(page, 'project-1')).rejects.toThrow()
  await expect(injectProjectApi(page, metadata().web_origin, 'bundle')).rejects.toThrow(/^PP3B_API_INJECT$/)
  expect(page.addScriptTag).not.toHaveBeenCalled()
  await navigate(page)
  expect(page.goto).toHaveBeenCalledWith(`${metadata().web_origin}/app?surface=browser`)
  expect(events).toEqual(['navigate', 'inject'])
  expect((await snapshot(page, 'project-1')).project_id).toBe('project-1')
  url = 'http://127.0.0.1:5186/app'
  await expect(injectProjectApi(page, metadata().web_origin, 'bundle')).rejects.toThrow(/^PP3B_API_INJECT$/)
  expect(events).toEqual(['navigate', 'inject', 'read'])
  expect(spec.match(/window\.__pp3bApi/g)).toHaveLength(4)
  expect(spec.match(/\.goto\(/g)).toHaveLength(1)
  expect(spec).toMatch(/async function openProjectUI\(page\) \{\s+await navigateProject\(page\)/)
  expect(spec).toMatch(/test\('project_browser_create_import',[\s\S]*?withIdentity\(browser, async \(\{ page \}\) => \{\s+await navigateProject\(page\)/)
  expect(spec).toMatch(/test\('project_browser_bearer_required',[\s\S]*?await openProjectUI\(page\)[\s\S]*?await snapshot\(page,/)
  expect(spec).toMatch(/await page\.reload\(\)\s+await injectProjectApi\(page, metadata.web_origin, await projectApiBundle\(\)\)\s+check\(await page.evaluate/)
  expect(spec).toMatch(/async verifier => \{\s+await openProjectUI\(verifier.page\)\s+check\(JSON.stringify\(await snapshot/)
  expect(spec).toMatch(/test\('project_settings_worker_readback',[\s\S]*?await openProjectUI\(page\)[\s\S]*?await readContext\(page,/)
  expect(spec).toMatch(/await page\.reload\(\)\s+await openProjectUI\(page\)/)
})

it('PP3B-18 bundle builds once per worker including concurrent callers and caches failures', async () => {
  const build = vi.fn(async () => fakeBundle())
  const bundle = createProjectApiBundle({}, build)
  const first = bundle()
  expect(bundle()).toBe(first)
  expect(await first).toBe(fakeBundle().outputFiles[0].text)
  expect(await bundle()).toBe(await first)
  expect(build).toHaveBeenCalledTimes(1)
  const fail = vi.fn(async () => { throw new Error('private-path') })
  const rejected = createProjectApiBundle({}, fail)
  await expect(rejected()).rejects.toThrow(/^PP3B_API_BUILD$/)
  await expect(rejected()).rejects.toThrow(/^PP3B_API_BUILD$/)
  expect(fail).toHaveBeenCalledTimes(1)
  const spec = source('./project-authority.spec.mjs')
  expect(spec.match(/createProjectApiBundle\(\)/g)).toHaveLength(1)
  expect(spec).toMatch(/^const projectApiBundle = createProjectApiBundle\(\)/m)
})

it('PP3B-19 build failures expose only a fixed PP3B code and suppress compiler diagnostics', async () => {
  for (const build of [vi.fn(() => { throw new Error('SOURCE /private/path SECRET') }),
    vi.fn(async () => { throw new Error('SOURCE /private/path SECRET') })]) {
    const error = await createProjectApiBundle({}, build)().catch(value => value)
    expect(error.message).toBe('PP3B_API_BUILD')
    expect(error.cause).toBeUndefined()
    expect(build.mock.calls[0][0].logLevel).toBe('silent')
  }
})

it('PP3B-20 bundle wait is bounded and times out with a fixed PP3B code', async () => {
  vi.useFakeTimers()
  try {
    expect(BUILD_TIMEOUT_MS).toBe(30_000)
    const build = vi.fn(() => new Promise(() => {}))
    const bundle = createProjectApiBundle({}, build)
    const result = bundle().catch(error => error.message)
    let settled = false
    result.then(() => { settled = true })
    await vi.advanceTimersByTimeAsync(BUILD_TIMEOUT_MS - 1)
    expect(settled).toBe(false)
    await vi.advanceTimersByTimeAsync(1)
    expect(settled).toBe(true)
    expect(await result).toBe('PP3B_API_BUILD')
    expect(vi.getTimerCount()).toBe(0)
    await expect(bundle()).rejects.toThrow(/^PP3B_API_BUILD$/)
    expect(build).toHaveBeenCalledTimes(1)
  } finally { vi.useRealTimers() }
})

it('PP3B-21 bundle output is nonempty single-file and bounded by UTF-8 byte size', async () => {
  expect(MAX_BUNDLE_BYTES).toBe(4 * 1024 * 1024)
  for (const result of [{}, { outputFiles: [] }, { outputFiles: [{ text: '' }] },
    { outputFiles: [{ text: 42 }] }, { outputFiles: [{ text: 'a' }, { text: 'b' }] },
    { outputFiles: [{ text: 'x'.repeat(MAX_BUNDLE_BYTES + 1) }] },
    { outputFiles: [{ text: '\u00e9'.repeat(MAX_BUNDLE_BYTES / 2 + 1) }] }]) {
    await expect(createProjectApiBundle({}, async () => result)()).rejects.toThrow(/^PP3B_API_BUILD$/)
  }
  const limit = 'x'.repeat(MAX_BUNDLE_BYTES)
  expect(await createProjectApiBundle({}, async () => ({ outputFiles: [{ text: limit }] }))()).toBe(limit)
})
