import { spawn, spawnSync } from 'node:child_process'
import { randomUUID } from 'node:crypto'
import { existsSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { mkdir, mkdtemp, open, readFile, rename, rm, stat, writeFile } from 'node:fs/promises'
import { connect, createServer } from 'node:net'
import { freemem, tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { setTimeout as delay } from 'node:timers/promises'
import { fileURLToPath } from 'node:url'
import { prepareProductionBundle, startSameOriginProxy } from './sameOriginProxy.mjs'

const repo = resolve(dirname(fileURLToPath(import.meta.url)), '../..')
const python = process.env.LEAF_TEST_PYTHON || process.env.PYTHON || 'python'
const admissionScript = 'C:/Users/ehaug/.claude/scripts/studio-walk/admission.py'
const running = new Set()
const pendingSlots = new Set()
const roles = ['app', 'broker', 'harness', 'web', 'proxy']
const ramFloor = 6 * 1024 ** 3
const osNames = new Set(['PATH', 'SYSTEMROOT', 'TEMP', 'TMP', 'HOME', 'USERPROFILE', 'APPDATA', 'LOCALAPPDATA', 'PATHEXT', 'COMSPEC', 'LANG'])

export class QueuedError extends Error {
  constructor(message = 'Local stack admission is queued', admission) { super(message); this.name = 'QueuedError'; this.code = 'QUEUED'; this.admission = admission }
}
export class StoppedError extends Error {
  constructor(message = 'Local stack admission is stopped', admission) { super(message); this.name = 'StoppedError'; this.code = 'STOPPED'; this.admission = admission }
}

export async function portListening(port) {
  return new Promise((accept) => {
    const socket = connect({ host: '127.0.0.1', port })
    let settled = false
    const done = (value) => { if (settled) return; settled = true; socket.destroy(); accept(value) }
    socket.once('connect', () => done(true))
    socket.once('error', () => done(false))
    socket.setTimeout(500, () => done(false))
  })
}

export function pidAlive(pid) {
  try { process.kill(pid, 0); return true } catch (error) {
    if (error.code === 'ESRCH') return false
    if (error.code === 'EPERM') return true
    throw error
  }
}

async function until(check, timeoutMs, reason) {
  const end = Date.now() + timeoutMs
  while (Date.now() < end) { if (await check()) return; await delay(100) }
  throw new Error(typeof reason === 'function' ? reason() : reason)
}

// Slot k owns [18000 + 100*k, 18099 + 100*k]: app +10, broker +20,
// harness +30, production web +40, public same-origin proxy +0. No drift.
export async function allocatePorts(slot) {
  if (!Number.isInteger(slot) || slot < 0 || slot > 474) throw new RangeError('slot must be an integer from 0 through 474')
  const base = 18000 + 100 * slot
  const ports = { app: base + 10, broker: base + 20, harness: base + 30, web: base + 40, proxy: base }
  const reservations = []
  try {
    for (const role of roles) {
      const server = createServer()
      await new Promise((accept, reject) => {
        server.once('error', (error) => reject(new Error(`${role} port ${ports[role]} is unavailable: ${error.message}`)))
        server.listen({ host: '127.0.0.1', port: ports[role], exclusive: true }, accept)
      })
      reservations.push(server)
    }
    return ports
  } finally {
    await Promise.all(reservations.map((server) => new Promise((accept) => server.close(accept))))
  }
}

function commandArgs(raw) {
  if (raw.trim().startsWith('[')) {
    const argv = JSON.parse(raw)
    if (!Array.isArray(argv) || !argv.length || argv.some((item) => typeof item !== 'string' || !item)) throw new StoppedError('LEAF_WALK_ADMISSION_CMD must be a nonempty argv array')
    return argv
  }
  // A quoted argv string is also supported, without executing a shell.
  const tokens = raw.match(/"[^"]*"|'[^']*'|[^\s"']+/g) || []
  if (!tokens.length || tokens.join(' ').replace(/\s+/g, ' ') !== raw.trim().replace(/\s+/g, ' ')) throw new StoppedError('Malformed LEAF_WALK_ADMISSION_CMD; use a JSON argv array')
  return tokens.map((token) => /^['"]/.test(token) ? token.slice(1, -1) : token)
}

async function executeAdmission(argv) {
  const child = spawn(argv[0], argv.slice(1), { windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] })
  let output = ''
  let errorOutput = ''
  child.stdout.on('data', (data) => { output = (output + data).slice(-16000) })
  child.stderr.on('data', (data) => { errorOutput = (errorOutput + data).slice(-4000) })
  const timer = setTimeout(() => child.kill('SIGKILL'), 30000)
  try {
    const code = await new Promise((accept, reject) => { child.once('error', reject); child.once('close', accept) })
    let answer = {}
    if (output.trim()) {
      try { answer = JSON.parse(output) } catch { throw new StoppedError('Admission command did not return JSON') }
    }
    if (code === 75) return { ...answer, status: 'queued' }
    if (code === 76) return { ...answer, status: 'stopped' }
    if (code !== 0) throw new StoppedError(`Admission command failed (${code}): ${errorOutput}`)
    return { ...answer, status: 'admitted' }
  } finally { clearTimeout(timer) }
}

export async function defaultAdmission() {
  if (process.env.LEAF_WALK_ADMISSION_CMD) return executeAdmission(commandArgs(process.env.LEAF_WALK_ADMISSION_CMD))
  if (existsSync(admissionScript)) return executeAdmission([python, '-B', admissionScript, '--kind', 'walk_local', '--json'])
  return freemem() >= ramFloor ? { status: 'admitted', slots: 2, source: 'local' } : { status: 'queued', slots: 0, reason: 'Less than 6 GiB free RAM' }
}

function slotCap(value, label) {
  if (value === undefined || value === '') return 2
  const number = Number(value)
  if (!Number.isInteger(number) || number < 0) throw new StoppedError(`${label} must be a nonnegative integer`)
  return Math.min(2, number)
}

// Machine-wide leases keep the conservative fallback honest across Node workers.
// A reused PID fails closed (queues); it never authorizes stealing a live lease.
async function acquireLease(cap, slot) {
  for (let index = 0; index < cap; index++) {
    const path = join(tmpdir(), `leaf-walk-local-slot-${index}.lock`)
    const token = randomUUID()
    for (let attempt = 0; attempt < 2; attempt++) {
      let file
      try {
        file = await open(path, 'wx', 0o600)
        await file.writeFile(JSON.stringify({ pid: process.pid, token, slot }))
        await file.close()
        return { path, token }
      } catch (error) {
        if (file) { await file.close().catch(() => {}); await rm(path, { force: true }); throw error }
        if (error.code !== 'EEXIST') throw error
        let owner
        try { owner = JSON.parse(await readFile(path, 'utf8')) } catch { break }
        if (!Number.isInteger(owner.pid) || owner.pid <= 0 || pidAlive(owner.pid)) break
        await rm(path, { force: true })
      }
    }
  }
  throw new QueuedError(`All ${cap} local stack slots are occupied`)
}

function releaseLeaseSync(lease) {
  if (!lease) return
  try { if (JSON.parse(readFileSync(lease.path, 'utf8')).token === lease.token) rmSync(lease.path) } catch (error) {
    if (error.code !== 'ENOENT') throw error
  }
}

async function privateEnvironment(root, ports, databaseURL, harnessDatabaseURL) {
  const directories = {
    LEAF_STORE_DIR: 'drawings', LEAF_GUEST_STORE_DIR: 'guest-drawings', LEAF_UPLOADS_DIR: 'uploads',
    LEAF_GRANTS_DIR: 'grants', LEAF_TENANTS_DIR: 'tenants', LEAF_WORKSPACE_BASE: 'workspaces', LEAF_TENANT_GIT_DIR: 'tenant-git',
    LEAF_TENANT_MCP_DIR: 'tenant-mcp', LEAF_SESSIONS_DIR: 'harness-sessions', LEAF_AGENT_STATE_DIR: 'agent',
    LEAF_AGENT_APPROVALS_DIR: 'approvals', LEAF_PLATFORM_CUSTOMIZE_STATE_DIR: 'customize', LEAF_BUILD_RECEIPTS_DIR: 'build-receipts',
  }
  const files = {
    SESSIONS_DB: 'sessions.db', JOBS_DB: 'jobs.db', PENDING_REAPS_PATH: 'pending-reaps.jsonl',
    BROKER_LEDGER: 'broker-ledger.jsonl', BROKER_TENANTS: 'broker-tenants.json', BROKER_ACTIVE_WORKITEMS_PATH: 'broker-active.jsonl',
    LEAF_CUSTOMIZATION_DB: 'customization.db', LEAF_AGENT_GRANTS_FILE: 'agent-grants.json', LEAF_AGENT_RATE_FILE: 'agent-rate.json',
    LEAF_AGENT_KILL_FILE: 'agent-kill.json', LEAF_AGENT_TENANTS_FILE: 'agent-tenants.json', LEAF_AGENT_LEDGER: 'agent-ledger.jsonl',
    LEAF_AGENT_AUDIT: 'agent-audit.jsonl', LEAF_CLOUD_GRANTS_FILE: 'cloud-grants.json', LEAF_ENTITLEMENTS_FILE: 'entitlements.json',
    LEAF_ROLES_FILE: 'roles.json', LEAF_TENANTS_FILE: 'tenants.json', LEAF_SITE_CACHE_FILE: 'site-cache.json', LEAF_GRANT_FILE: 'no-legacy-grant.token',
    LEAF_USAGE_CAPS_FILE: 'usage-caps.json',
  }
  const env = Object.fromEntries(Object.entries(process.env).filter(([key]) => osNames.has(key.toUpperCase())))
  Object.assign(env, {
    PYTHONDONTWRITEBYTECODE: '1', PYTHONUTF8: '1', APS_LIVE: '0', LEAF_AUTH_LIVE: '0', LEAF_AGENT_MOCK: '1',
    LEAF_RUNTIME_ENV: 'local', LEAF_TELEMETRY_DISABLED: '1', LEAF_CUSTOMIZATION_STAGE_WORKER_DISABLED: '1',
    // Upload admission defaults closed. Enable it explicitly for this private
    // local store; the launcher's allowlist must carry the setting to the app.
    LEAF_UPLOAD_IMPORT_MUTATIONS_ENABLED: '1',
    LEAF_CAMPAIGN_RELEASE_WORKER_DISABLED: '1', LEAF_CUSTOMIZATION_R5_MODE: 'off', LEAF_CUSTOMIZATION_R6_MODE: 'off',
    LEAF_DAILY_AUTHOR_QUOTA: '1', LEAF_AUTHOR_QUOTA_STORE: 'memory', LEAF_AUTHOR_TEMPLATE_FALLBACK: '0',
    LEAF_GUEST_SECRET: randomUUID(), LEAF_OPS_SECRET: randomUUID(), TENANT_MCP_FAKE_OAUTH: '1',
    LEAF_APP_PUBLIC_BASE_URL: `http://127.0.0.1:${ports.proxy}`, LEAF_CORS_ORIGINS: `http://127.0.0.1:${ports.proxy},http://127.0.0.1:${ports.web}`,
    TMPDIR: root, TEMP: root, TMP: root,
  })
  for (const [name, relative] of Object.entries(directories)) {
    env[name] = join(root, relative)
    await mkdir(env[name], { recursive: true })
  }
  for (const [name, relative] of Object.entries(files)) env[name] = join(root, relative)
  await writeFile(env.LEAF_ENTITLEMENTS_FILE, await readFile(join(repo, 'server', 'entitlements.json')))
  // Database authority is opt-in, never inherited from the host. Callers must
  // provision separate databases/schemas when opting in for separate stacks.
  if (databaseURL) env.DATABASE_URL = databaseURL
  if (harnessDatabaseURL) env.LEAF_HARNESS_DATABASE_URL = harnessDatabaseURL
  return env
}

let measuring
async function processTable() {
  // Both stacks share a host snapshot. Never block the event loop here: it
  // also serves their proxies, and synchronous CIM probes can starve HTTP.
  if (measuring) return measuring
  measuring = readProcessTable()
  try { return await measuring } finally { measuring = undefined }
}

async function readProcessTable() {
  const windows = process.platform === 'win32'
  const child = windows
    ? spawn('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command', 'Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,WorkingSetSize | ConvertTo-Json -Compress'], { windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] })
    : spawn('ps', ['-e', '-o', 'pid=,ppid=,rss='], { stdio: ['ignore', 'pipe', 'pipe'] })
  let stdout = ''
  let stderr = ''
  let failure
  child.stdout.on('data', (data) => {
    stdout += data.toString()
    if (Buffer.byteLength(stdout) > 8 * 1024 * 1024) { failure = new Error('Process table exceeds 8 MiB'); child.kill('SIGKILL') }
  })
  child.stderr.on('data', (data) => { stderr = (stderr + data).slice(-4000) })
  const timer = setTimeout(() => { failure = new Error('Process table probe timed out'); child.kill('SIGKILL') }, 10000)
  try {
    const code = await new Promise((accept, reject) => { child.once('error', reject); child.once('close', accept) })
    if (failure || code !== 0) throw failure || new Error(`Cannot measure stack process tree: ${stderr || code}`)
  } finally { clearTimeout(timer) }
  if (windows) {
    const data = JSON.parse(stdout)
    return (Array.isArray(data) ? data : [data]).map((row) => ({ pid: Number(row.ProcessId), parent: Number(row.ParentProcessId), rss: Number(row.WorkingSetSize) }))
  }
  return stdout.trim().split('\n').filter(Boolean).map((line) => {
    const [pid, parent, rss] = line.trim().split(/\s+/).map(Number)
    return { pid, parent, rss: rss * 1024 }
  })
}

async function sample(state) {
  if (!state.child?.pid) return
  if (state.samplePromise) return state.samplePromise
  state.samplePromise = sampleTree(state)
  try { return await state.samplePromise } finally { state.samplePromise = undefined }
}

async function sampleTree(state) {
  const table = await processTable()
  const tree = new Set([state.child.pid, ...Object.values(state.receipt?.pids || {})])
  let changed = true
  while (changed) {
    changed = false
    for (const row of table) if (tree.has(row.parent) && !tree.has(row.pid)) { tree.add(row.pid); changed = true }
  }
  const rss = table.filter((row) => tree.has(row.pid)).reduce((sum, row) => sum + row.rss, 0)
  if (!Number.isFinite(rss)) throw new Error('Process tree returned invalid RSS')
  for (const pid of tree) state.pids.add(pid)
  for (const row of table) if (row.parent === state.child.pid) state.groups.add(row.pid)
  state.peakRssBytes = Math.max(state.peakRssBytes, rss)
  state.rssSamples++
}

function killSync(state, force = false) {
  if (!state.child?.pid) return
  if (process.platform === 'win32') {
    // The launcher's kill-on-close job also covers descendants whose parent died.
    const roots = [state.child.pid, ...Object.values(state.receipt?.pids || {})]
    for (const pid of roots) if (pidAlive(pid)) {
      const result = spawnSync('taskkill', ['/PID', String(pid), '/T', '/F'], { windowsHide: true, timeout: 15000, stdio: 'ignore' })
      if (result.error) throw result.error
    }
  } else {
    // start-leaf gives EACH service its own session; killing only the launcher
    // group is insufficient. Also signal every ready-receipt service group.
    for (const pid of [state.child.pid, ...state.groups, ...Object.values(state.receipt?.pids || {})]) {
      try { process.kill(-pid, force ? 'SIGKILL' : 'SIGTERM') } catch (error) { if (error.code !== 'ESRCH') throw error }
    }
    if (force) for (const pid of state.pids) {
      try { process.kill(pid, 'SIGKILL') } catch (error) { if (error.code !== 'ESRCH') throw error }
    }
  }
}

function metrics(state) {
  return { slot: state.slot, root: state.root, bootSeconds: state.bootSeconds, peakRssBytes: state.peakRssBytes, rssSamples: state.rssSamples, pids: [...state.pids], metricsPath: state.metricsPath }
}

process.once('exit', () => {
  for (const state of running) {
    try {
      killSync(state, true)
      if (state.root) { writeFileSync(state.metricsPath, JSON.stringify(metrics(state)) + '\n'); rmSync(state.root, { recursive: true, force: true }) }
      releaseLeaseSync(state.lease)
    } catch { /* Normal stop reports errors; process exit can only do best-effort synchronous cleanup. */ }
  }
})
async function stopAll(signal) {
  const results = await Promise.allSettled([...running].map((state) => state.stop()))
  for (const result of results) if (result.status === 'rejected') console.error(result.reason)
  process.exit(results.some((result) => result.status === 'rejected') ? 1 : signal === 'SIGINT' ? 130 : 143)
}
process.once('SIGINT', () => { void stopAll('SIGINT') })
process.once('SIGTERM', () => { void stopAll('SIGTERM') })
process.on('beforeExit', () => { for (const state of running) void state.stop().catch((error) => { console.error(error); process.exitCode = 1 }) })

export async function startStack({ slot, admission = defaultAdmission, slots, databaseURL, harnessDatabaseURL, timeoutMs = 180000 } = {}) {
  if (typeof admission !== 'function') throw new TypeError('admission must be a function')
  // No root, build, listener or stack child exists before this per-launch hook.
  const answer = await admission({ slot, kind: 'walk_local' })
  const status = typeof answer === 'string' ? answer.toLowerCase() : String(answer?.status || answer?.decision || (answer?.admitted === true ? 'admitted' : '')).toLowerCase()
  if (status === 'queued') throw new QueuedError(answer?.reason, answer)
  if (status !== 'admitted') throw new StoppedError(answer?.reason || 'Admission did not explicitly admit the stack', answer)
  const cap = Math.min(slotCap(slots, 'slots'), slotCap(process.env.LEAF_WALK_SLOTS, 'LEAF_WALK_SLOTS'), slotCap(answer?.slots, 'admission slots'))
  if (cap === 0 || freemem() < ramFloor) throw new QueuedError(cap === 0 ? 'Zero local stack slots available' : 'Less than 6 GiB free RAM', answer)
  if (pendingSlots.has(slot)) throw new QueuedError(`Stack slot ${slot} is already owned`)
  if (!Number.isFinite(timeoutMs) || timeoutMs <= 0) throw new RangeError('timeoutMs must be positive')
  pendingSlots.add(slot)
  const state = { slot, pids: new Set(), groups: new Set(), peakRssBytes: 0, rssSamples: 0, bootSeconds: 0, proxies: [], output: '', closed: false }
  let stopping
  state.stop = () => {
    if (!stopping) stopping = (async () => {
      const errors = []
      clearInterval(state.sampler)
      if (state.child?.pid && !state.closed) {
        try { await sample(state) } catch (error) { state.sampleError = error }
      }
      // A probe begun before child exit must finish before metrics and PID
      // cleanup are finalized; it must not mutate the record after stop().
      if (state.samplePromise) await state.samplePromise.catch((error) => { state.sampleError = error })
      const proxyResults = await Promise.allSettled(state.proxies.map((proxy) => proxy.stop()))
      for (const result of proxyResults) if (result.status === 'rejected') errors.push(result.reason)
      try { killSync(state) } catch (error) { errors.push(error) }
      try {
        await until(async () => (!state.child || state.closed) && [...state.pids].every((pid) => !pidAlive(pid)) && (await Promise.all(Object.values(state.ports || {}).map(portListening))).every((open) => !open), 15000, 'Stack teardown left a listener or child process')
      } catch {
        killSync(state, true)
        await until(async () => (!state.child || state.closed) && [...state.pids].every((pid) => !pidAlive(pid)) && (await Promise.all(Object.values(state.ports || {}).map(portListening))).every((open) => !open), 15000, 'Stack teardown failed after force-kill')
      }
      const record = metrics(state)
      if (state.root) {
        const temporary = join(state.root, 'metrics.json')
        try {
          await writeFile(temporary, JSON.stringify(record, null, 2) + '\n')
          await rename(temporary, state.metricsPath)
        } catch (error) { errors.push(error) }
        try { await rm(state.root, { recursive: true, force: true }) } catch (error) { errors.push(error) }
      }
      try { releaseLeaseSync(state.lease) } catch (error) { errors.push(error) }
      pendingSlots.delete(slot)
      running.delete(state)
      if (errors.length) throw new AggregateError(errors, 'Stack stopped with cleanup errors')
      return record
    })()
    return stopping
  }
  running.add(state)
  try {
    state.lease = await acquireLease(cap, slot)
    state.ports = await allocatePorts(slot)
    // Fail closed if a repo-local dotenv could silently reconnect to host data.
    if (!databaseURL && existsSync(join(repo, 'platform', '.env.local'))) {
      const local = await readFile(join(repo, 'platform', '.env.local'), 'utf8')
      if (/^\s*(?:export\s+)?DATABASE_URL\s*=/m.test(local)) throw new Error('platform/.env.local contains database authority; remove it from this isolated checkout or pass an explicitly private databaseURL')
    }
    if (!existsSync(join(repo, 'harness', 'dist', 'scripts', 'serve.js'))) throw new Error('Missing prerequisite harness/dist/scripts/serve.js; compile the harness before running the stack fixture')
    const bundleDir = await prepareProductionBundle()
    state.root = await mkdtemp(join(tmpdir(), `leaf-walk-stack-${slot}-`))
    state.metricsPath = join(dirname(state.root), `leaf-walk-stack-metrics-${slot}.json`)
    const env = await privateEnvironment(state.root, state.ports, databaseURL, harnessDatabaseURL)
    const readyPath = join(state.root, 'ready.json')
    const args = ['-B', '-u', join(repo, 'scripts', 'start-leaf.py'), '--no-web', '--with-harness', '--strict-ports', '--ready-file', readyPath,
      '--env-allowlist', Object.keys(env).filter((key) => !osNames.has(key.toUpperCase())).join(','),
      ...['app', 'broker', 'harness', 'web'].flatMap((role) => [`--${role}-port`, String(state.ports[role])])]
    // The cached build/preparation may have waited on another worker. An
    // earlier admission never licenses crossing the RAM floor at spawn time.
    if (freemem() < ramFloor) throw new QueuedError('Less than 6 GiB free RAM before stack launch', answer)
    const start = performance.now()
    state.child = spawn(python, args, { cwd: repo, env, detached: process.platform !== 'win32', windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] })
    if (state.child.pid) state.pids.add(state.child.pid)
    state.child.once('error', (error) => { state.error = error; state.closed = true })
    state.child.once('close', (code, signal) => { state.closed = true; state.result = { code, signal } })
    for (const stream of [state.child.stdout, state.child.stderr]) stream.on('data', (chunk) => { state.output = (state.output + chunk).slice(-20000) })
    state.sampler = setInterval(() => { void sample(state).catch((error) => { state.sampleError = error }) }, 2000)
    state.sampler.unref()
    await until(async () => {
      if (state.error) throw state.error
      if (state.closed) throw new Error(`Stack launcher exited ${JSON.stringify(state.result)}:\n${state.output}`)
      try { await stat(readyPath); return true } catch (error) { if (error.code !== 'ENOENT') throw error; return false }
    }, timeoutMs, () => `Stack did not publish readiness within ${timeoutMs}ms:\n${state.output}`)
    state.receipt = JSON.parse(await readFile(readyPath, 'utf8'))
    if (state.receipt.launcher_pid !== state.child.pid || ['app', 'broker', 'harness'].some((role) => state.receipt.ports[role] !== state.ports[role] || !Number.isInteger(state.receipt.pids[role]) || !pidAlive(state.receipt.pids[role]))) throw new Error('Readiness receipt does not identify the requested live stack')
    await sample(state)
    if (state.peakRssBytes <= 0) throw state.sampleError || new Error('No process-tree RSS measurement was obtained')
    for (const role of ['web', 'proxy']) state.proxies.push(await startSameOriginProxy({ port: state.ports[role], appPort: state.ports.app, bundleDir }))
    if (state.closed || (await Promise.all(Object.values(state.ports).map(portListening))).some((open) => !open)) throw new Error('Stack lost a listener before boot completed')
    state.bootSeconds = (performance.now() - start) / 1000
    return { baseURL: `http://127.0.0.1:${state.ports.proxy}`, ports: state.ports, pids: state.pids, root: state.root, env, metricsPath: state.metricsPath, output: () => state.output, stop: state.stop }
  } catch (error) {
    try { await state.stop() } catch (cleanupError) { throw new AggregateError([error, cleanupError], 'Stack boot failed and cleanup failed') }
    throw error
  }
}
