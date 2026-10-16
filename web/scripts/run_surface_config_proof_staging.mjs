import { chromium } from '@playwright/test'
import { writeFile, rename, rm } from 'node:fs/promises'
import { randomUUID } from 'node:crypto'
import { resolve } from 'node:path'
import { pathToFileURL } from 'node:url'
import { allowedStagingOrigins, assertAllowedStagingHost } from '../e2e/staging/stagingConfig.mjs'
import { captureStagingIdentity } from '../e2e/staging/stagingIdentity.mjs'
import { runSurfaceConfigProof, surfaceConfigProofPage } from './verify_surface_config_live.mjs'

export function parseArgs(argv, env = {}) {
  const options = { baseUrl: 'https://platform-staging.leafdesign.ai', tenantId: undefined,
    expectedSourceSha: undefined, out: undefined, jwtStdin: false, probe: false,
    headed: false, timeoutMs: 90000, token: (env.LEAF_E2E_STAGING_JWT || '').trim() }
  const values = { '--base-url': 'baseUrl', '--tenant-id': 'tenantId',
    '--expected-sha': 'expectedSourceSha', '--out': 'out', '--timeout-ms': 'timeoutMs' }
  const flags = { '--jwt-stdin': 'jwtStdin', '--probe': 'probe', '--headed': 'headed' }
  for (let i = 0; i < argv.length; i++) {
    const flag = argv[i]
    if (Object.hasOwn(flags, flag)) options[flags[flag]] = true
    else if (Object.hasOwn(values, flag)) {
      if (!argv[i + 1] || argv[i + 1].startsWith('--')) throw new Error('Missing option value')
      options[values[flag]] = argv[++i]
    } else throw new Error('Unknown option')
  }
  if (!options.probe) {
    if (!options.tenantId?.trim()) throw new Error('--tenant-id is required')
    if (!options.expectedSourceSha) throw new Error('--expected-sha is required')
    if (!options.out?.trim()) throw new Error('--out is required')
  }
  if (options.expectedSourceSha !== undefined && !/^[a-f0-9]{40}$/.test(options.expectedSourceSha)) {
    throw new Error('--expected-sha must be 40 lowercase hex characters')
  }
  if (!/^\d+$/.test(String(options.timeoutMs)) || !Number.isSafeInteger(Number(options.timeoutMs))
    || Number(options.timeoutMs) <= 0) throw new Error('--timeout-ms must be a positive integer')
  options.timeoutMs = Number(options.timeoutMs)
  const url = assertAllowedStagingHost(options.baseUrl, env)
  if (url.username || url.password || url.search || url.hash || !['', '/'].includes(url.pathname)) {
    throw new Error('--base-url must be a staging origin')
  }
  options.baseUrl = url.origin
  return options
}

export function redact(text, token) {
  const value = String(text)
  return token ? value.split(token).join('<redacted>') : value
}

export function makeTransport(page) {
  return Object.fromEntries(['getSession', 'getSurfaceConfig', 'submitSurfaceConfig'].map((name) =>
    [name, (...args) => page.evaluate(([name, args]) =>
      window.__leafSurfaceConfigProof[name](...args), [name, args])]))
}

// Scrub strings before serialization too, including object keys and nested data.
function scrub(value, token) {
  if (typeof value === 'string') return redact(value, token)
  if (Array.isArray(value)) return value.map((item) => scrub(item, token))
  if (value !== null && typeof value === 'object') {
    return Object.fromEntries(Object.entries(value).map(([key, item]) =>
      [redact(key, token), scrub(item, token)]))
  }
  return value
}

export function buildReceipt(receipt, { ranAt, baseUrl, expectedSourceSha, token = '' }) {
  return scrub({ ...receipt, ranAt, baseUrl, expectedSourceSha,
    driver: 'run_surface_config_proof_staging.mjs' }, token)
}

async function writeAtomic(path, receipt) {
  const temporary = `${path}.${randomUUID()}.tmp`
  try {
    await writeFile(temporary, `${JSON.stringify(receipt, null, 2)}\n`, { flag: 'wx', mode: 0o600 })
    await rename(temporary, path)
  } finally {
    await rm(temporary, { force: true })
  }
}

async function main() {
  let token = (process.env.LEAF_E2E_STAGING_JWT || '').trim()
  try {
    const options = parseArgs(process.argv.slice(2), process.env)
    if (options.jwtStdin) {
      token = ''
      process.stdin.setEncoding('utf8')
      for await (const chunk of process.stdin) token += chunk
      token = token.trim()
    }
    if (!token) throw new Error('A staging JWT is required via stdin or LEAF_E2E_STAGING_JWT')
    let browser
    try {
      browser = await chromium.launch({ headless: !options.headed, timeout: options.timeoutMs })
      const context = await browser.newContext({ baseURL: options.baseUrl })
      const page = await context.newPage()
      page.setDefaultTimeout(options.timeoutMs)
      page.setDefaultNavigationTimeout(options.timeoutMs)
      await page.addInitScript(({ token, allowedOrigins }) => {
        if (allowedOrigins.includes(location.origin.toLowerCase())) {
          window.localStorage.setItem('leaf.jwt', token)
        }
      }, { token, allowedOrigins: [...allowedStagingOrigins()] })
      await page.goto(`${options.baseUrl}/try?surface=cad`)
      try {
        await page.waitForFunction(() => window.__leafSurfaceConfigProof?.version === 1,
          null, { timeout: options.timeoutMs })
      } catch (error) {
        if (error?.name !== 'TimeoutError') throw error
        console.error(JSON.stringify({ reason: 'bridge_absent' }))
        return 3
      }
      const transport = makeTransport(page)
      if (options.probe) {
        const session = await transport.getSession(false)
        if (!session?.tenant || !session.org || !session.tier) throw new Error('authentication_required')
        const deployment = await captureStagingIdentity(page.request)
        console.log(JSON.stringify(scrub({ tenant: session.tenant, tier: session.tier,
          surface: session.surface ?? null, sourceRevision: deployment.source_revision }, token)))
        return 0
      }
      const proofPage = surfaceConfigProofPage(page, { transport, dedicatedTenantId: options.tenantId })
      const receipt = await runSurfaceConfigProof({ page: proofPage,
        baseUrl: options.baseUrl, expectedSourceSha: options.expectedSourceSha })
      const output = buildReceipt(receipt, { ranAt: new Date().toISOString(),
        baseUrl: options.baseUrl, expectedSourceSha: options.expectedSourceSha, token })
      await writeAtomic(options.out, output)
      if (receipt.ok === true) return 0
      console.error(JSON.stringify(scrub({ ok: receipt.ok, failure: receipt.failure,
        mutation: receipt.mutation, restoration: receipt.restoration,
        restorationFailure: receipt.restorationFailure }, token)))
      return 1
    } finally {
      if (browser) await browser.close()
    }
  } catch (error) {
    console.error(redact(`${error?.name || 'Error'}: ${error?.message || 'Driver failed'}`, token))
    return 1
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  process.exitCode = await main()
}
