// Author-quota live proof (P-082): drive the per-tenant DAILY authoring cap on a
// STAGING deployment until the server itself refuses with the quota_exceeded 429,
// then let a real browser hit that same refusal and capture the rendered QuotaGate.
//
// web/e2e/author-quota-gate.spec.mjs proves the UI against a 429 the test supplies
// itself; this runner is the half that proves the deployed server enforces it.
//
// Contract, fail closed on every point:
// - Dry run is the default and makes ZERO network requests and launches nothing.
// - Execution needs ALL of: --execute, https web and api URLs whose hosts are on
//   LEAF_QUOTA_PROOF_ALLOWED_HOSTS, every such host a staging host (production is
//   refused by name and by shape), and an explicit --max-requests cap.
// - Every authoring request, the runner's own and the browser's, takes one unit
//   from a single request budget; the browser's request is aborted past the cap.
// - The run stops at the FIRST quota refusal. Any other non-2xx stops it at once.
// - The bearer token is read in-process from LEAF_QUOTA_PROOF_JWT by name only. It
//   never reaches argv, the plan, the receipt or a log; errors are scrubbed of it.
import { mkdirSync, writeFileSync } from 'node:fs'
import { randomBytes } from 'node:crypto'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

export const SCHEMA = 'leaf.author-quota-live-proof.v1'
export const TOKEN_ENV = 'LEAF_QUOTA_PROOF_JWT'
export const STAGE_PATH = '/api/author/stage'
// One refused API probe plus the browser's one request is the smallest useful run.
export const MIN_REQUESTS = 2
// Hard ceiling: each accepted request is real authoring spend.
export const MAX_REQUEST_CEILING = 25
export const PRODUCTION_HOSTS = Object.freeze([
  'leafdesign.ai',
  'www.leafdesign.ai',
  'app.leafdesign.ai',
  'api.leafdesign.ai',
  'platform.leafdesign.ai',
])
// The QuotaGate copy web/src/components/AuthorPanel.jsx renders for the 429.
export const EXPECTED_GATE_TEXT = Object.freeze([
  'daily limit on authoring new tools',
  'resets at 00:00 UTC',
])
const STAGE_TIMEOUT_MS = 60_000
const BROWSER_TIMEOUT_MS = 240_000
const MAX_BODY_BYTES = 64 * 1024
const MAX_TOKEN_LENGTH = 8192
const DEFAULT_REQUEST = 'count panels within 24in of the roof edge'
const STAGING_LABEL = /(^|[.-])staging([.-]|$)/i
const RUN_ID = /^[A-Za-z0-9._-]{1,64}$/
const REPO_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..')

export class ProofError extends Error {
  constructor(code, message) {
    super(message)
    this.name = 'ProofError'
    this.code = code
  }
}

export function parseArgs(argv) {
  const args = { execute: false, maxRequests: null, out: null, help: false }
  for (let i = 0; i < argv.length; i += 1) {
    const raw = argv[i]
    const eq = raw.indexOf('=')
    const flag = eq > 0 ? raw.slice(0, eq) : raw
    const inline = eq > 0 ? raw.slice(eq + 1) : null
    const value = () => {
      if (inline !== null) return inline
      i += 1
      if (i >= argv.length) throw new ProofError('usage', `${flag} needs a value`)
      return argv[i]
    }
    if (flag === '--execute' && inline === null) args.execute = true
    else if (flag === '--help' || flag === '-h') args.help = true
    else if (flag === '--max-requests') args.maxRequests = value()
    else if (flag === '--out') args.out = value()
    else throw new ProofError('usage', `unknown argument: ${flag}`)
  }
  return args
}

export function isProductionHost(host) {
  const h = String(host || '').toLowerCase()
  if (PRODUCTION_HOSTS.includes(h)) return true
  // Any leafdesign.ai host that does not name itself staging is production.
  if (h.endsWith('.leafdesign.ai') && !STAGING_LABEL.test(h)) return true
  return false
}

export function isStagingHost(host) {
  return STAGING_LABEL.test(String(host || '')) && !isProductionHost(host)
}

function parseOrigin(name, raw, refusals) {
  if (!raw) {
    refusals.push(`${name} is not set`)
    return null
  }
  let url
  try {
    url = new URL(raw)
  } catch {
    refusals.push(`${name} is not a URL`)
    return null
  }
  if (url.protocol !== 'https:') refusals.push(`${name} must be https`)
  if (url.username || url.password) refusals.push(`${name} must not carry credentials`)
  if (url.search || url.hash || (url.pathname && url.pathname !== '/')) {
    refusals.push(`${name} must be a bare origin`)
  }
  return url.origin
}

export function parseRequestCap(raw, refusals) {
  if (raw === null || raw === undefined || raw === '') {
    refusals.push('--max-requests is required for execution')
    return null
  }
  if (!/^\d{1,3}$/.test(String(raw))) {
    refusals.push('--max-requests must be a whole number')
    return null
  }
  const cap = Number(raw)
  if (cap < MIN_REQUESTS || cap > MAX_REQUEST_CEILING) {
    refusals.push(`--max-requests must be between ${MIN_REQUESTS} and ${MAX_REQUEST_CEILING}`)
    return null
  }
  return cap
}

// Never returns the value to a caller that could print it; only a presence bit
// leaves this module's config, and readToken() is the one in-process reader.
function tokenReader(env) {
  const raw = env[TOKEN_ENV]
  const ok = typeof raw === 'string' && raw.length > 0 && raw.length <= MAX_TOKEN_LENGTH && !/\s/.test(raw)
  return { present: ok, read: () => (ok ? raw : null), value: ok ? raw : null }
}

// Builds the config and the full list of reasons execution would be refused.
// Dry run reports the refusals; execution throws on any.
export function resolveConfig(env, args, randomId = () => randomBytes(6).toString('hex')) {
  const refusals = []
  const webOrigin = parseOrigin('LEAF_QUOTA_PROOF_WEB_URL', env.LEAF_QUOTA_PROOF_WEB_URL, refusals)
  const apiOrigin = parseOrigin('LEAF_QUOTA_PROOF_API_URL', env.LEAF_QUOTA_PROOF_API_URL, refusals)
  const allowedHosts = String(env.LEAF_QUOTA_PROOF_ALLOWED_HOSTS || '')
    .split(',')
    .map((h) => h.trim().toLowerCase())
    .filter(Boolean)
  if (!allowedHosts.length) refusals.push('LEAF_QUOTA_PROOF_ALLOWED_HOSTS is empty')
  for (const host of allowedHosts) {
    if (!isStagingHost(host)) refusals.push(`allowlisted host is not a staging host: ${host}`)
  }
  for (const [name, origin] of [['web', webOrigin], ['api', apiOrigin]]) {
    if (!origin) continue
    const host = new URL(origin).hostname.toLowerCase()
    if (isProductionHost(host)) refusals.push(`${name} host is production: ${host}`)
    else if (!isStagingHost(host)) refusals.push(`${name} host is not a staging host: ${host}`)
    if (!allowedHosts.includes(host)) refusals.push(`${name} host is not allowlisted: ${host}`)
  }
  const maxRequests = parseRequestCap(args.maxRequests, refusals)
  const token = tokenReader(env)
  if (!token.present) refusals.push(`${TOKEN_ENV} is not set`)
  const runId = env.LEAF_QUOTA_PROOF_RUN_ID || `quota-${randomId()}`
  if (!RUN_ID.test(runId)) refusals.push('LEAF_QUOTA_PROOF_RUN_ID must match [A-Za-z0-9._-]{1,64}')
  const tenantId = env.LEAF_QUOTA_PROOF_TENANT_ID || null
  if (tenantId !== null && !RUN_ID.test(tenantId)) refusals.push('LEAF_QUOTA_PROOF_TENANT_ID is malformed')
  const orgId = env.LEAF_QUOTA_PROOF_ORG_ID || null
  if (orgId !== null && !RUN_ID.test(orgId)) refusals.push('LEAF_QUOTA_PROOF_ORG_ID is malformed')
  const config = {
    runId,
    webOrigin,
    apiOrigin,
    allowedHosts,
    maxRequests,
    tenantId,
    orgId,
    description: `${DEFAULT_REQUEST} (${runId})`,
    out: args.out ? resolve(args.out) : join(REPO_ROOT, 'artifacts', 'author-quota-live-proof', runId),
    tokenPresent: token.present,
  }
  // Non-enumerable: JSON.stringify and object spreads never see it.
  Object.defineProperty(config, 'readToken', { value: token.read, enumerable: false })
  Object.defineProperty(config, 'secrets', { value: token.value ? [token.value] : [], enumerable: false })
  return { config, refusals }
}

export function buildPlan(config, refusals) {
  return {
    schema: SCHEMA,
    mode: 'dry-run',
    requests_made: 0,
    run_id: config.runId,
    web_origin: config.webOrigin,
    api_origin: config.apiOrigin,
    allowed_hosts: config.allowedHosts,
    max_requests: config.maxRequests,
    token_env: TOKEN_ENV,
    token_present: config.tokenPresent,
    execution_ready: refusals.length === 0,
    refusals,
    steps: [
      `POST ${STAGE_PATH} until the first quota_exceeded 429, keeping one request for the browser`,
      'open /try in a headless browser, Generate tool once, capture the QuotaGate and a screenshot',
      `write receipt.json and quota-gate.png under ${config.out}`,
    ],
  }
}

// One budget for every authoring request in the run, API probe or browser.
export function createRequestBudget(limit) {
  if (!Number.isInteger(limit) || limit < 1 || limit > MAX_REQUEST_CEILING) {
    throw new ProofError('configuration', 'request budget is out of range')
  }
  const log = []
  return {
    get used() { return log.length },
    get remaining() { return limit - log.length },
    log,
    take(source) {
      if (log.length >= limit) {
        throw new ProofError('request_cap', `request cap ${limit} reached before ${source} request`)
      }
      log.push(source)
      return log.length
    },
  }
}

function quotaFields(body) {
  if (!body || typeof body !== 'object') return null
  const nested = body.error && typeof body.error === 'object' ? body.error : {}
  const pick = (key) => (body[key] !== undefined ? body[key] : nested[key])
  return {
    error_code: pick('error_code'),
    quota_kind: pick('quota_kind'),
    tier: pick('tier'),
    limit: pick('limit'),
    used: pick('used'),
    retryable: pick('retryable'),
    message: pick('message'),
  }
}

// 'accepted' (spend happened), 'quota_refusal' (the proof), else 'unexpected'.
export function classifyStageResponse(response) {
  const status = response && response.status
  if (status === 200 || status === 202) return 'accepted'
  if (status === 429) {
    const f = quotaFields(response.body)
    if (f && f.error_code === 'quota_exceeded' && f.quota_kind === 'daily_author') return 'quota_refusal'
  }
  return 'unexpected'
}

export function summarizeQuotaRefusal(body) {
  const f = quotaFields(body)
  const count = (n) => Number.isInteger(n) && n >= 0
  if (!f || !count(f.limit) || !count(f.used)) {
    throw new ProofError('quota_envelope', 'the quota refusal did not carry integer limit and used counts')
  }
  return {
    error_code: f.error_code,
    quota_kind: f.quota_kind,
    tier: typeof f.tier === 'string' ? f.tier.slice(0, 64) : null,
    limit: f.limit,
    used: f.used,
    retryable: f.retryable === true,
    message: typeof f.message === 'string' ? f.message.slice(0, 300) : null,
  }
}

export function validateGateCapture(capture, refusal) {
  const problems = []
  if (!capture || typeof capture !== 'object') return ['the browser returned no capture']
  if (capture.stageStatus !== 429) problems.push(`the browser's authoring request returned HTTP ${capture.stageStatus}`)
  if (capture.stageAttempts !== 1) problems.push(`the browser sent ${capture.stageAttempts} authoring requests, expected 1`)
  if (!capture.gateVisible) problems.push('the QuotaGate status region is not visible')
  const text = String(capture.gateText || '')
  const counts = `(${refusal.used}/${refusal.limit})`
  for (const expected of [counts, ...EXPECTED_GATE_TEXT]) {
    if (!text.includes(expected)) problems.push(`the QuotaGate text is missing "${expected}"`)
  }
  if (capture.inlineErrorCount !== 0) problems.push('a red inline error rendered beside the gate')
  if (capture.authoredCount !== 0) problems.push('an authored tool rendered despite the refusal')
  return problems
}

async function withTimeout(promise, ms, code, label) {
  let timer
  try {
    return await Promise.race([
      promise,
      new Promise((_, reject) => {
        timer = setTimeout(() => reject(new ProofError(code, `${label} timed out after ${ms} ms`)), ms)
      }),
    ])
  } finally {
    clearTimeout(timer)
  }
}

export async function runExecution(config, { backend, browser, now = () => new Date() }) {
  if (!config.maxRequests) throw new ProofError('configuration', 'no request cap')
  const budget = createRequestBudget(config.maxRequests)
  const startedAt = now().toISOString()
  const attempts = []
  let refusal = null
  // Keep the last unit for the browser so the UI sees the server's own 429.
  while (budget.remaining > 1) {
    const n = budget.take('api')
    const response = await withTimeout(
      backend.stage({
        apiOrigin: config.apiOrigin,
        tenantId: config.tenantId,
        description: config.description,
        idempotencyKey: `${config.runId}-${n}`,
        readToken: config.readToken,
      }),
      STAGE_TIMEOUT_MS + 5_000,
      'stage_timeout',
      `authoring request ${n}`,
    )
    const outcome = classifyStageResponse(response)
    attempts.push({ attempt: n, status: response && response.status, outcome })
    if (outcome === 'quota_refusal') {
      refusal = summarizeQuotaRefusal(response.body)
      break
    }
    if (outcome !== 'accepted') {
      throw new ProofError('unexpected_response', `authoring request ${n} returned HTTP ${response && response.status}; stopped`)
    }
  }
  if (!refusal) {
    throw new ProofError(
      'cap_not_reached',
      `no quota refusal within ${budget.used} authoring requests; the tenant cap is above the request budget`,
    )
  }
  const screenshotPath = join(config.out, 'quota-gate.png')
  const capture = await withTimeout(
    browser.captureQuotaGate({
      webOrigin: config.webOrigin,
      apiOrigin: config.apiOrigin,
      allowedHosts: config.allowedHosts,
      orgId: config.orgId,
      description: config.description,
      readToken: config.readToken,
      takeRequest: () => budget.take('browser'),
      screenshotPath,
    }),
    BROWSER_TIMEOUT_MS,
    'browser_timeout',
    'QuotaGate capture',
  )
  const problems = validateGateCapture(capture, refusal)
  if (problems.length) throw new ProofError('ui_mismatch', problems.join('; '))
  return {
    schema: SCHEMA,
    mode: 'execute',
    ok: true,
    run_id: config.runId,
    web_origin: config.webOrigin,
    api_origin: config.apiOrigin,
    max_requests: config.maxRequests,
    requests_made: budget.used,
    request_sources: [...budget.log],
    attempts,
    stopped_at_attempt: attempts[attempts.length - 1].attempt,
    quota_refusal: refusal,
    quota_gate: {
      stage_status: capture.stageStatus,
      visible: true,
      text: String(capture.gateText).slice(0, 600),
      screenshot: capture.screenshot || null,
      // Aborted before leaving the browser; recorded, never followed.
      blocked_origins: Array.isArray(capture.blockedOrigins) ? capture.blockedOrigins.slice(0, 20) : [],
    },
    started_at: startedAt,
    stopped_at: now().toISOString(),
  }
}

async function readBoundedJson(response) {
  const text = await response.text()
  if (text.length > MAX_BODY_BYTES) throw new ProofError('response_size', 'authoring response exceeded 64 KiB')
  try {
    return text ? JSON.parse(text) : null
  } catch {
    return null
  }
}

// The real backend: one POST per call, no retries, no redirects, bounded time and body.
export function createFetchBackend({ fetchImpl = globalThis.fetch, timeoutMs = STAGE_TIMEOUT_MS } = {}) {
  return {
    async stage({ apiOrigin, tenantId, description, idempotencyKey, readToken }) {
      const token = readToken()
      if (!token) throw new ProofError('configuration', `${TOKEN_ENV} is not set`)
      let response
      try {
        response = await fetchImpl(`${apiOrigin}${STAGE_PATH}`, {
          method: 'POST',
          redirect: 'manual',
          signal: AbortSignal.timeout(timeoutMs),
          headers: {
            'Content-Type': 'application/json',
            Authorization: `Bearer ${token}`,
            ...(tenantId ? { 'X-Tenant-Id': tenantId } : {}),
          },
          body: JSON.stringify({ description, mode: 'build', idempotency_key: idempotencyKey }),
        })
      } catch (error) {
        throw new ProofError('network', `authoring request failed: ${error && error.name}`)
      }
      return { status: response.status, body: await readBoundedJson(response) }
    },
  }
}

// The real browser: headless Chromium, the platform token in localStorage the way
// web/src/api.js reads it, every request outside the allowlist aborted, and the
// one authoring request routed through the shared budget.
export function createPlaywrightBrowser({ chromiumImpl } = {}) {
  return {
    async captureQuotaGate(opts) {
      const chromium = chromiumImpl || (await import('@playwright/test')).chromium
      const browser = await chromium.launch({ headless: true })
      try {
        const context = await browser.newContext({ baseURL: opts.webOrigin, serviceWorkers: 'block' })
        await context.addInitScript(({ token, orgId }) => {
          window.localStorage.setItem('leaf.jwt', token)
          if (orgId) window.localStorage.setItem('leaf.org_id', orgId)
        }, { token: opts.readToken(), orgId: opts.orgId })
        const allowed = new Set(opts.allowedHosts)
        const blockedOrigins = []
        let stageAttempts = 0
        await context.route('**/*', async (route) => {
          const request = route.request()
          const url = new URL(request.url())
          if (!['http:', 'https:'].includes(url.protocol)) return route.continue()
          if (!allowed.has(url.hostname.toLowerCase())) {
            if (blockedOrigins.length < 20) blockedOrigins.push(url.origin)
            return route.abort('blockedbyclient')
          }
          if (url.origin === opts.apiOrigin && url.pathname === STAGE_PATH && request.method() === 'POST') {
            try {
              opts.takeRequest()
            } catch {
              return route.abort('blockedbyclient')
            }
            stageAttempts += 1
          }
          return route.continue()
        })
        const page = await context.newPage()
        await page.goto('/try', { waitUntil: 'domcontentloaded', timeout: 120_000 })
        await page.getByTestId('operator-phase')
          .filter({ hasText: 'Backend ready' })
          .waitFor({ state: 'visible', timeout: 120_000 })
        await page.getByRole('tab', { name: 'Author' }).click()
        await page.getByLabel('What should the tool do?').fill(opts.description)
        const stageResponse = page.waitForResponse(
          (r) => r.url() === `${opts.apiOrigin}${STAGE_PATH}` && r.request().method() === 'POST',
          { timeout: 60_000 },
        )
        await page.getByRole('button', { name: 'Generate tool', exact: true }).click()
        const stageStatus = (await stageResponse).status()
        const gate = page.locator('.author-gate[role="status"]')
        let gateVisible = false
        try {
          await gate.waitFor({ state: 'visible', timeout: 30_000 })
          gateVisible = true
        } catch {
          gateVisible = false
        }
        const gateText = gateVisible ? await gate.innerText() : ''
        const inlineErrorCount = await page.locator('.inline-error').count()
        const authoredCount = await page.locator('.authored').count()
        mkdirSync(dirname(opts.screenshotPath), { recursive: true })
        await page.screenshot({ path: opts.screenshotPath, fullPage: true })
        return {
          stageStatus,
          stageAttempts,
          gateVisible,
          gateText,
          inlineErrorCount,
          authoredCount,
          blockedOrigins,
          screenshot: opts.screenshotPath,
        }
      } finally {
        await browser.close()
      }
    },
  }
}

export function scrub(text, secrets) {
  let out = String(text)
  for (const secret of secrets || []) {
    if (secret) out = out.split(secret).join('[redacted]')
  }
  return out.slice(0, 1000)
}

const USAGE = 'Usage: node web/scripts/author_quota_live_proof.mjs [--execute --max-requests <n>] [--out <dir>]\n'
  + `Dry run (default) prints the plan and makes no requests. Token is read from ${TOKEN_ENV}.`

export async function main(argv = process.argv.slice(2), env = process.env, deps = {}) {
  const stdout = deps.stdout || ((line) => console.log(line))
  const stderr = deps.stderr || ((line) => console.error(line))
  let secrets = []
  try {
    const args = parseArgs(argv)
    if (args.help) {
      stdout(USAGE)
      return 0
    }
    const { config, refusals } = resolveConfig(env, args, deps.randomId)
    secrets = config.secrets
    if (!args.execute) {
      stdout(JSON.stringify(buildPlan(config, refusals)))
      return 0
    }
    if (refusals.length) throw new ProofError('configuration', refusals.join('; '))
    const receipt = await runExecution(config, {
      backend: deps.backend || createFetchBackend(),
      browser: deps.browser || createPlaywrightBrowser(),
      now: deps.now,
    })
    mkdirSync(config.out, { recursive: true })
    const receiptPath = join(config.out, 'receipt.json')
    const body = JSON.stringify(receipt, null, 2)
    if (secrets.some((secret) => body.includes(secret))) throw new ProofError('secret_leak', 'the receipt carried the token; not written')
    writeFileSync(receiptPath, `${body}\n`, { flag: 'wx', mode: 0o600 })
    stdout(JSON.stringify({ ok: true, mode: 'execute', requests_made: receipt.requests_made, receipt: receiptPath }))
    return 0
  } catch (error) {
    const code = error instanceof ProofError ? error.code : 'internal'
    stderr(JSON.stringify({ ok: false, code, detail: scrub(error && error.message, secrets) }))
    return code === 'usage' || code === 'configuration' ? 2 : 1
  }
}

if (import.meta.url === pathToFileURL(process.argv[1] || '').href) {
  process.exitCode = await main()
}
