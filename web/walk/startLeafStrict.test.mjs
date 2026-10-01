import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { mkdtemp, mkdir, readFile, rm, stat, writeFile } from 'node:fs/promises'
import { createServer, connect } from 'node:net'
import { createServer as createHttpServer } from 'node:http'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { setTimeout as delay } from 'node:timers/promises'
import { test } from 'node:test'

const repo = resolve(dirname(fileURLToPath(import.meta.url)), '../..')
const launcher = join(repo, 'scripts', 'start-leaf.py')
const python = process.env.LEAF_TEST_PYTHON || process.env.PYTHON || 'python'
const roles = ['broker', 'app', 'harness', 'web']

function launch(args, env = process.env, code) {
  const command = code ? ['-B', '-u', '-c', code, launcher, ...args] : ['-B', '-u', launcher, ...args]
  const child = spawn(python, command, { cwd: repo, env, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] })
  const run = { child, stdout: '', stderr: '', result: null }
  child.stdout.on('data', (data) => { run.stdout += data })
  child.stderr.on('data', (data) => { run.stderr += data })
  run.exited = new Promise((resolve, reject) => {
    child.once('error', (error) => {
      run.error = error
      reject(new Error(`Cannot run ${python}: ${error.message}`))
    })
    child.once('close', (code, signal) => {
      run.result = { code, signal }
      resolve(run.result)
    })
  })
  // Startup is polled separately; preserve spawn errors without an unhandled rejection.
  run.exited.catch(() => {})
  return run
}

function output(run) {
  return `exit=${JSON.stringify(run.result)}\nstdout:\n${run.stdout}\nstderr:\n${run.stderr}`
}

async function until(check, timeoutMs, reason) {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    if (await check()) return
    await delay(50)
  }
  throw new Error(typeof reason === 'function' ? reason() : reason)
}

async function stop(run) {
  if (run.error || run.result) return
  if (process.platform === 'win32') {
    // Killing the launcher closes its kill-on-close job, including npm descendants.
    const killer = spawn('taskkill', ['/PID', String(run.child.pid), '/T', '/F'], { windowsHide: true, stdio: 'ignore' })
    await new Promise((resolve, reject) => { killer.once('error', reject); killer.once('close', resolve) })
  } else {
    run.child.kill('SIGTERM')
  }
  await until(() => run.result !== null, 15000, () => `Launcher did not stop:\n${output(run)}`)
}

async function finish(run, timeoutMs = 20000) {
  try {
    await until(() => {
      if (run.error) throw run.error
      return run.result !== null
    }, timeoutMs, () => `Launcher did not exit:\n${output(run)}`)
    return await run.exited
  } finally {
    await stop(run)
  }
}

async function listen(server) {
  await new Promise((resolve, reject) => {
    server.once('error', reject)
    server.listen(0, '127.0.0.1', resolve)
  })
  return server.address().port
}

async function close(server) {
  await new Promise((resolve, reject) => server.close((error) => error ? reject(error) : resolve()))
}

async function freePorts() {
  // Reserve all ports together so this stack never requests a duplicate port.
  const servers = roles.map(() => createServer())
  try {
    const ports = await Promise.all(servers.map(listen))
    return Object.fromEntries(roles.map((role, index) => [role, ports[index]]))
  } finally {
    await Promise.all(servers.filter((server) => server.listening).map(close))
  }
}

function portArgs(ports) {
  return roles.flatMap((role) => [`--${role}-port`, String(ports[role])])
}

async function listening(port) {
  return await new Promise((resolve) => {
    const socket = connect({ host: '127.0.0.1', port })
    let settled = false
    const done = (value) => {
      if (settled) return
      settled = true
      socket.destroy()
      resolve(value)
    }
    socket.once('connect', () => done(true))
    socket.once('error', () => done(false))
    socket.setTimeout(500, () => done(false))
  })
}

function pidAlive(pid) {
  try { process.kill(pid, 0); return true } catch (error) {
    if (error.code === 'ESRCH') return false
    throw error
  }
}

async function assertStopped(ports, pids = []) {
  await until(async () => {
    const open = await Promise.all(Object.values(ports).map(listening))
    return open.every((value) => !value) && pids.every((pid) => !pidAlive(pid))
  }, 15000, `Child survived teardown: ports ${JSON.stringify(ports)}, pids ${JSON.stringify(pids)}`)
}

async function exists(path) {
  try { await stat(path); return true } catch (error) {
    if (error.code === 'ENOENT') return false
    throw error
  }
}

async function isolatedStack(root) {
  const directories = {
    LEAF_STORE_DIR: 'drawings', LEAF_GUEST_STORE_DIR: 'guest-drawings', LEAF_UPLOADS_DIR: 'uploads',
    LEAF_GRANTS_DIR: 'grants', LEAF_TENANTS_DIR: 'tenants', LEAF_TENANT_GIT_DIR: 'tenant-git',
    LEAF_TENANT_MCP_DIR: 'tenant-mcp', LEAF_AGENT_STATE_DIR: 'agent',
    LEAF_AGENT_APPROVALS_DIR: 'approvals', LEAF_PLATFORM_CUSTOMIZE_STATE_DIR: 'customize',
    LEAF_BUILD_RECEIPTS_DIR: 'build-receipts',
  }
  const files = {
    SESSIONS_DB: 'sessions.db', JOBS_DB: 'jobs.db', PENDING_REAPS_PATH: 'pending-reaps.json',
    BROKER_LEDGER: 'broker-ledger.jsonl', BROKER_TENANTS: 'broker-tenants.json',
    BROKER_ACTIVE_WORKITEMS_PATH: 'broker-active.json', LEAF_CUSTOMIZATION_DB: 'customization.db',
    LEAF_AGENT_GRANTS_FILE: 'agent-grants.json', LEAF_AGENT_RATE_FILE: 'agent-rate.json',
    LEAF_AGENT_KILL_FILE: 'agent-kill.json', LEAF_AGENT_TENANTS_FILE: 'agent-tenants.json',
    LEAF_AGENT_LEDGER: 'agent-ledger.jsonl', LEAF_AGENT_AUDIT: 'agent-audit.jsonl',
    LEAF_CLOUD_GRANTS_FILE: 'cloud-grants.json', LEAF_ENTITLEMENTS_FILE: 'entitlements.json',
    LEAF_ROLES_FILE: 'roles.json', LEAF_TENANTS_FILE: 'tenants.json', LEAF_SITE_CACHE_FILE: 'site-cache.json',
  }
  const overrides = {
    PYTHONDONTWRITEBYTECODE: '1', PYTHONUTF8: '1',
    LEAF_CUSTOMIZATION_STAGE_WORKER_DISABLED: '1', LEAF_CAMPAIGN_RELEASE_WORKER_DISABLED: '1',
  }
  for (const [name, relative] of Object.entries(directories)) {
    overrides[name] = join(root, relative)
    await mkdir(overrides[name], { recursive: true })
  }
  for (const [name, relative] of Object.entries(files)) overrides[name] = join(root, relative)
  return { env: { ...process.env, ...overrides }, args: ['--env-allowlist', Object.keys(overrides).join(',')] }
}

test('--help preserves the legacy CLI and documents the new flags', async () => {
  const run = launch(['--help'])
  const result = await finish(run)
  assert.equal(result.code, 0, output(run))
  for (const flag of ['--no-web', '--with-harness', '--strict-ports', '--ready-file', '--env-allowlist']) {
    assert.ok(run.stdout.includes(flag), `Missing ${flag} in help`)
  }
})

test('--strict-ports rejects each occupied role before starting any children', async (t) => {
  for (const role of roles) {
    await t.test(role, async () => {
      const ports = await freePorts()
      const busy = createServer()
      await new Promise((resolve, reject) => {
        busy.once('error', reject)
        busy.listen(ports[role], '127.0.0.1', resolve)
      })
      try {
        const run = launch(['--strict-ports', '--with-harness', ...portArgs(ports)])
        const result = await finish(run)
        assert.notEqual(result.code, 0, output(run))
        assert.match(run.stderr, new RegExp(`${role}.*${ports[role]}.*busy`), output(run))
        assert.equal(run.stdout.includes('[start-leaf] start '), false, output(run))
        for (const other of roles.filter((other) => other !== role)) {
          assert.equal(await listening(ports[other]), false, `${other} was left listening`)
        }
      } finally {
        await close(busy)
      }
    })
  }
})

test('--env-allowlist excludes database URLs and ambient variables unless explicitly named', async () => {
  const parent = {
    ...process.env, DATABASE_URL: 'postgres://test.invalid/main', LEAF_HARNESS_DATABASE_URL: 'postgres://test.invalid/harness',
    OTHER_PG_URL: 'postgres://test.invalid/other', UNRELATED_SECRET: 'must-not-leak',
    LEAF_AGENT_STORE: 'postgres', LEAF_AUTH_LIVE: '1', ALLOWED_MARKER: 'included',
  }
  const run = launch(['--env-allowlist', 'ALLOWED_MARKER', '--print-child-env'], parent)
  assert.equal((await finish(run)).code, 0, output(run))
  const env = JSON.parse(run.stdout)
  for (const name of ['DATABASE_URL', 'LEAF_HARNESS_DATABASE_URL', 'OTHER_PG_URL', 'UNRELATED_SECRET', 'LEAF_AGENT_STORE']) {
    assert.equal(Object.hasOwn(env, name), false, `${name} leaked into the child environment`)
  }
  assert.equal(env.ALLOWED_MARKER, 'included')
  assert.equal(env.APS_LIVE, '0')
  assert.equal(env.LEAF_AUTH_LIVE, '0')
  for (const name of ['PATH', 'SYSTEMROOT', 'TEMP', 'TMP', 'HOME', 'USERPROFILE', 'APPDATA', 'LOCALAPPDATA', 'PATHEXT', 'COMSPEC', 'LANG']) {
    const key = Object.keys(parent).find((key) => process.platform === 'win32' ? key.toUpperCase() === name : key === name)
    if (key) {
      const childKey = Object.keys(env).find((key) => process.platform === 'win32' ? key.toUpperCase() === name : key === name)
      assert.equal(env[childKey], parent[key], `Missing OS variable ${key}`)
    }
  }
  const allowed = launch(['--env-allowlist', 'DATABASE_URL,ALLOWED_MARKER', '--env-allowlist', 'OTHER_PG_URL,LEAF_HARNESS_DATABASE_URL', '--print-child-env'], parent)
  assert.equal((await finish(allowed)).code, 0, output(allowed))
  const explicit = JSON.parse(allowed.stdout)
  for (const name of ['DATABASE_URL', 'OTHER_PG_URL', 'LEAF_HARNESS_DATABASE_URL']) assert.equal(explicit[name], parent[name])
  const empty = launch(['--env-allowlist', '', '--print-child-env'], parent)
  assert.equal((await finish(empty)).code, 0, output(empty))
  assert.equal(Object.hasOwn(JSON.parse(empty.stdout), 'ALLOWED_MARKER'), false)
  const inherited = launch(['--print-child-env'], parent)
  assert.equal((await finish(inherited)).code, 0, output(inherited))
  assert.equal(JSON.parse(inherited.stdout).DATABASE_URL, parent.DATABASE_URL)
  assert.equal(JSON.parse(inherited.stdout).LEAF_AUTH_LIVE, '1')
})

test('health probes keep legacy 4xx acceptance but ready-file probes require 2xx and reject timeouts', async () => {
  const server = createHttpServer((request, response) => {
    response.writeHead(Number(request.url.slice(1)))
    response.end('probe')
  })
  const port = await listen(server)
  try {
    const code = `
import importlib.util, sys
spec = importlib.util.spec_from_file_location('start_leaf', sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
base = sys.argv[2]
assert module.http_ok(base + '/204', strict=True)
assert module.http_ok(base + '/404')
for status in (404, 503):
    try:
        module.wait_healthy('test', base + '/' + str(status), deadline_s=0.1, strict=True)
    except RuntimeError as error:
        assert str(status) in str(error), str(error)
    else:
        raise AssertionError('non-2xx was accepted')
try:
    module.wait_healthy('test', sys.argv[3], deadline_s=0, strict=True)
except RuntimeError as error:
    assert '2xx' in str(error), str(error)
else:
    raise AssertionError('timeout was accepted')
`
    const run = launch([`http://127.0.0.1:${port}`, `http://127.0.0.1:${port}/204`], process.env, code)
    assert.equal((await finish(run)).code, 0, output(run))
  } finally {
    await close(server)
  }
})

test('--ready-file fails closed on 4xx/5xx, removes stale readiness, and tears down startup children', async (t) => {
  for (const status of [404, 503]) {
    await t.test(String(status), async () => {
      const root = await mkdtemp(join(tmpdir(), 'leaf-ready-failure-'))
      const ports = await freePorts()
      const ready = join(root, 'ready.json')
      const stack = await isolatedStack(root)
      await writeFile(ready, '{"stale":true}')
      // Inject a health response while exercising the real launch and failure cleanup.
      const code = `
import runpy, sys, urllib.request, urllib.error
original = urllib.request.urlopen
def unhealthy(url, *args, **kwargs):
    if '/broker/health' in url:
        raise urllib.error.HTTPError(url, ${status}, 'injected health failure', {}, None)
    return original(url, *args, **kwargs)
urllib.request.urlopen = unhealthy
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name='__main__')
`
      const run = launch(['--strict-ports', '--ready-file', ready, ...stack.args, ...portArgs(ports)], stack.env, code)
      try {
        const result = await finish(run, 60000)
        assert.notEqual(result.code, 0, output(run))
        assert.match(run.stderr, new RegExp(`HTTP ${status}`), output(run))
        assert.equal(await exists(ready), false, 'Failed boot published readiness')
        await assertStopped(ports)
      } finally {
        await stop(run)
        await rm(root, { recursive: true, force: true })
      }
    })
  }
})

test('--ready-file publishes a real broker/app/web stack only after all probes return 2xx; teardown leaves no children', { timeout: 240000 }, async () => {
  const root = await mkdtemp(join(tmpdir(), 'leaf-ready-stack-'))
  const ready = join(root, 'ready.json')
  const ports = await freePorts()
  const stack = await isolatedStack(root)
  const run = launch(['--strict-ports', '--ready-file', ready, ...stack.args, ...portArgs(ports)], stack.env)
  let receipt
  try {
    assert.equal(await exists(ready), false, 'Ready file existed before startup')
    await until(async () => {
      if (run.error) throw run.error
      if (run.result) throw new Error(`Real stack could not boot:\n${output(run)}`)
      return await exists(ready)
    }, 180000, () => `Real stack did not become ready:\n${output(run)}`)
    // Atomic publication means even the first read must be complete JSON.
    receipt = JSON.parse(await readFile(ready, 'utf8'))
    assert.deepEqual(receipt.ports, { broker: ports.broker, app: ports.app, web: ports.web })
    assert.equal(receipt.launcher_pid, run.child.pid)
    assert.deepEqual(Object.keys(receipt.pids).sort(), ['app', 'broker', 'web'])
    for (const pid of Object.values(receipt.pids)) {
      assert.ok(Number.isInteger(pid) && pid > 0)
      assert.equal(pidAlive(pid), true, `Child ${pid} was already dead at readiness`)
    }
    for (const [role, path] of [['broker', '/broker/health'], ['app', '/api/health'], ['web', '/']]) {
      const response = await fetch(`http://127.0.0.1:${ports[role]}${path}`, { signal: AbortSignal.timeout(5000) })
      await response.arrayBuffer()
      assert.ok(response.status >= 200 && response.status < 300, `${role} returned HTTP ${response.status} at readiness`)
    }
    // Each successful probe is logged before the readiness receipt is published.
    await until(() => ['broker', 'app', 'web'].every((role) => new RegExp(`OK\\s+${role}\\s+healthy`).test(run.stdout)), 5000, () => output(run))
    await stop(run)
    await assertStopped(ports, [...Object.values(receipt.pids), receipt.launcher_pid])
  } finally {
    await stop(run)
    await assertStopped(ports, receipt ? Object.values(receipt.pids) : [])
    await rm(root, { recursive: true, force: true })
  }
})
