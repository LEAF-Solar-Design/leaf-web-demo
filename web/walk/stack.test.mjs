import assert from 'node:assert/strict'
import { randomUUID } from 'node:crypto'
import { mkdir, mkdtemp, readFile, readdir, rm, stat, writeFile } from 'node:fs/promises'
import http from 'node:http'
import { createServer as createNetServer } from 'node:net'
import { tmpdir } from 'node:os'
import { dirname, isAbsolute, join, relative, resolve, sep } from 'node:path'
import { test } from 'node:test'
import { setTimeout as delay } from 'node:timers/promises'
import { fileURLToPath } from 'node:url'
import { allocatePorts, pidAlive, portListening, QueuedError, resolveStackSolver, SolverMissingError, StoppedError, startStack, waitForStackTeardown } from './stack.mjs'
import { backendPaths, prepareProductionBundle, startSameOriginProxy } from './sameOriginProxy.mjs'
import { PostgresUnavailableError, queryPostgres, stackAdminUrl } from './pgStack.mjs'

const repo = resolve(dirname(fileURLToPath(import.meta.url)), '../..')
const firstSlot = Number(process.env.LEAF_WALK_TEST_SLOT || 0)
const tenantHeaders = { 'X-Tenant-Id': 'demo-tenant' }

// Build once BEFORE node:test's stack timing. Missing prerequisites fail,
// including the compiled real harness; there is no skip/readiness fallback.
try { await stat(join(repo, 'harness/dist/scripts/serve.js')) } catch (error) {
  throw new Error('Missing prerequisite harness/dist/scripts/serve.js; compile the harness before this proof', { cause: error })
}
const bundleDir = await prepareProductionBundle()

async function until(check, timeoutMs, reason) {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    if (await check()) return
    await delay(100)
  }
  throw new Error(reason)
}

async function request(stack, path, options = {}) {
  const { json, headers, ...rest } = options
  try {
    const response = await fetch(stack.baseURL + path, {
      ...rest, signal: AbortSignal.timeout(20000),
      headers: { ...tenantHeaders, ...(json === undefined ? {} : { 'content-type': 'application/json' }), ...headers },
      ...(json === undefined ? {} : { body: JSON.stringify(json) }),
    })
    const text = await response.text()
    let body
    try { body = JSON.parse(text) } catch { body = null }
    return { status: response.status, body, text }
  } catch (error) {
    throw new Error(`${rest.method || 'GET'} ${stack.baseURL}${path} failed: ${error.message}\n${stack.output?.() || ''}`, { cause: error })
  }
}

function expectStatus(result, status) {
  assert.equal(result.status, status, JSON.stringify(result))
  return result.body
}

async function assertStopped(stack) {
  await until(async () => {
    const listeners = await Promise.all(Object.values(stack.ports).map(portListening))
    return listeners.every((open) => !open) && [...stack.pids].every((pid) => !pidAlive(pid))
  }, 15000, `Surviving listener/child: ${JSON.stringify(stack.ports)} pids=${JSON.stringify([...stack.pids])}`)
  await assert.rejects(stat(stack.root), { code: 'ENOENT' })
}

async function listen(server) {
  await new Promise((accept, reject) => {
    server.once('error', reject)
    server.listen(0, '127.0.0.1', accept)
  })
  return server.address().port
}

async function close(server) {
  const done = new Promise((accept, reject) => server.close((error) => error ? reject(error) : accept()))
  server.closeAllConnections()
  await done
}

// Observe a FRAME, not merely the headers (which a buffering proxy can flush).
// The caller explicitly ends/cancels the stream after observing the first frame.
function openSSE(url, headers = {}) {
  let incoming
  let ended = false
  let text = ''
  let timer
  const outgoing = http.get(url, { headers, agent: false })
  const first = new Promise((accept, reject) => {
    timer = setTimeout(() => { reject(new Error('No SSE event frame within 10 seconds')); outgoing.destroy() }, 10000)
    outgoing.once('error', reject)
    outgoing.once('response', (response) => {
      incoming = response
      if (response.statusCode !== 200 || !response.headers['content-type']?.includes('text/event-stream')) {
        reject(new Error(`SSE returned ${response.statusCode} ${JSON.stringify(response.headers)}`))
        response.resume()
        return
      }
      response.setEncoding('utf8')
      response.on('error', reject)
      response.on('end', () => { ended = true; reject(new Error('SSE ended before an event frame')) })
      response.on('data', (chunk) => {
        text += chunk
        if (text.length > 256 * 1024) { reject(new Error('SSE first-frame limit exceeded')); outgoing.destroy(); return }
        if (/event: [^\n]+\ndata: [^\n]+\n\n/.test(text)) {
          clearTimeout(timer)
          accept({ text, response, ended })
        }
      })
    })
  })
  // Tests may arrange a producer after opening the request; avoid an early
  // unhandled rejection while that POST is pending.
  first.catch(() => {})
  return { first, get ended() { return ended }, stop() { clearTimeout(timer); incoming?.destroy(); outgoing.destroy() } }
}

async function leaseHarness({ realPorts = false } = {}) {
  const source = await readFile(new URL('./stack.mjs', import.meta.url), 'utf8')
  const leases = source.slice(source.indexOf('async function acquireLease'), source.indexOf('async function privateEnvironment'))
  const allocation = source.slice(source.indexOf('export async function allocatePorts'), source.indexOf('function commandArgs'))
    .replace('export async function', 'async function')
  const launch = source.slice(source.indexOf('    state.lease = await acquireLease'), source.indexOf('    // Fail closed if a repo-local dotenv'))
  const locks = new Map()
  const open = async (path, mode) => {
    assert.equal(mode, 'wx')
    if (locks.has(path)) throw Object.assign(new Error('occupied'), { code: 'EEXIST' })
    locks.set(path, '')
    return { writeFile: async (body) => locks.set(path, body), close: async () => {} }
  }
  const createServer = () => ({
    once: () => {}, listen: (options, ready) => ready(), close: (done) => done(),
  })
  const harness = new Function('tmpdir', 'join', 'dirname', 'randomUUID', 'open', 'readFile', 'writeFile', 'rm', 'pidAlive',
    'QueuedError', 'readFileSync', 'rmSync', 'createServer', 'roles',
    `${leases}\n${allocation}\nreturn {
      claim: async (slot) => { const state = {}; const cap = 2; ${launch} return state },
      release: (state) => releaseLeaseSync(state.lease),
    }`)(() => 'private-test-leases', join, dirname, randomUUID, open,
    async (path) => {
      if (!locks.has(path)) throw Object.assign(new Error('missing'), { code: 'ENOENT' })
      return locks.get(path)
    }, async (path, body) => locks.set(path, body), async (path) => locks.delete(path), () => true,
    QueuedError, (path) => {
      if (!locks.has(path)) throw Object.assign(new Error('missing'), { code: 'ENOENT' })
      return locks.get(path)
    }, (path) => locks.delete(path), realPorts ? createNetServer : createServer,
    ['app', 'broker', 'harness', 'web', 'proxy'])
  return { ...harness, locks }
}

test('stack ports follow machine lease indices across distinct and reused worker slots', async () => {
  const harness = await leaseHarness()
  const { locks } = harness
  const first = await harness.claim(12)
  const second = await harness.claim(13)
  try {
    assert.equal(first.lease.index, 0)
    assert.equal(second.lease.index, 1)
    assert.equal(JSON.parse(locks.get(first.lease.path)).slot, 12)
    assert.equal(JSON.parse(locks.get(second.lease.path)).slot, 13)
    assert.deepEqual(first.ports, { app: 18010, broker: 18020, harness: 18030, web: 18040, proxy: 18000 })
    assert.deepEqual(second.ports, { app: 18110, broker: 18120, harness: 18130, web: 18140, proxy: 18100 })
    assert.ok(Object.values(first.ports).every((port) => !Object.values(second.ports).includes(port)))
    harness.release(first)
    const reused = await harness.claim(12)
    try {
      assert.equal(reused.lease.index, 0)
      assert.notEqual(reused.lease.token, first.lease.token)
      assert.notDeepEqual(reused.ports, first.ports)
    } finally { harness.release(reused) }
  } finally {
    harness.release(first)
    harness.release(second)
  }
  assert.equal([...locks.keys()].filter((path) => path.endsWith('.lock')).length, 0)
})

test('two consecutive acquisitions of the same lease index choose different port blocks', async () => {
  const harness = await leaseHarness()
  const first = await harness.claim(12)
  harness.release(first)
  const second = await harness.claim(12)
  try {
    assert.equal(second.lease.index, first.lease.index)
    assert.equal(first.block, 0)
    assert.equal(second.block, 2)
    assert.notEqual(second.ports.app, first.ports.app)
    assert.equal(JSON.parse(harness.locks.get(second.lease.path)).block, second.block)
    assert.equal(harness.locks.get(join(dirname(second.lease.path), 'leaf-walk-local-slot-0.gen')), '2\n')
  } finally { harness.release(second) }
})

test('different lease indices never choose the same block over 20 acquisitions each', async () => {
  const harness = await leaseHarness()
  const blocks = [new Set(), new Set()]
  for (let cycle = 0; cycle < 20; cycle++) {
    const first = await harness.claim(12)
    let second
    try {
      second = await harness.claim(13)
      assert.equal(first.lease.index, 0)
      assert.equal(second.lease.index, 1)
      blocks[0].add(first.block)
      blocks[1].add(second.block)
    } finally {
      harness.release(first)
      if (second) harness.release(second)
    }
  }
  assert.equal(blocks[0].size, 20)
  assert.equal(blocks[1].size, 20)
  assert.ok([...blocks[0]].every((block) => !blocks[1].has(block)))
})

test('an occupied app port is skipped and the next block is returned', async () => {
  const harness = await leaseHarness({ realPorts: true })
  let block
  for (let candidate = 400; candidate < 472; candidate += 2) {
    try {
      await allocatePorts(candidate)
      await allocatePorts(candidate + 2)
      block = candidate
      break
    } catch (error) {
      if (!/ port \d+ is unavailable:/.test(error.message)) throw error
    }
  }
  assert.ok(Number.isInteger(block), 'two adjacent test blocks must be available')
  harness.locks.set(join('private-test-leases', 'leaf-walk-local-slot-0.gen'), `${block / 2}\n`)
  const occupied = http.createServer()
  let state
  try {
    await new Promise((accept, reject) => {
      occupied.once('error', reject)
      occupied.listen(18000 + 100 * block + 10, '127.0.0.1', accept)
    })
    state = await harness.claim(12)
    assert.equal(state.block, block + 2)
    assert.equal(state.ports.app, 18000 + 100 * (block + 2) + 10)
    assert.equal(harness.locks.get(join('private-test-leases', 'leaf-walk-local-slot-0.gen')), `${block / 2 + 2}\n`)
  } finally {
    if (state) harness.release(state)
    if (occupied.listening) await close(occupied)
  }
})

test('a malformed generation file falls back to zero', async () => {
  const harness = await leaseHarness()
  const path = join('private-test-leases', 'leaf-walk-local-slot-0.gen')
  harness.locks.set(path, 'not-a-generation\n')
  const state = await harness.claim(12)
  try {
    assert.equal(state.block, 0)
    assert.equal(state.ports.app, 18010)
    assert.equal(harness.locks.get(path), '1\n')
  } finally { harness.release(state) }
})

test('teardown timeout names the unclosed launcher, surviving PID and listening port', async () => {
  const state = {
    child: { pid: 101 }, closed: false, pids: new Set([101, 202, 303]),
    receipt: { pids: { harness: 202, app: 303 } },
    processNames: new Map([[202, 'node.exe']]), ports: { harness: 18030, app: 18010 },
  }
  const checkedPids = []
  const checkedPorts = []
  const probes = {
    timeoutMs: 50,
    alive: (pid) => { checkedPids.push(pid); return pid === 202 },
    listening: async (port) => { checkedPorts.push(port); return port === 18030 },
  }
  await assert.rejects(waitForStackTeardown(state, probes), {
    message: 'Stack teardown failed after force-kill: launcher pid=101 not closed; live pids=[202 (harness/node.exe)]; listening ports=[harness:18030]',
  })
  assert.deepEqual(checkedPids, [101, 202, 303])
  assert.deepEqual(checkedPorts, [18030, 18010])
  // Neither a closed launcher nor an exited PID licenses a live listener.
  state.closed = true
  await assert.rejects(waitForStackTeardown(state, { ...probes, alive: () => false }), {
    message: 'Stack teardown failed after force-kill: listening ports=[harness:18030]',
  })
  await waitForStackTeardown(state, { ...probes, alive: () => false, listening: async () => false })
})

test('start and stop a real stack three times in a row without teardown errors', { timeout: 600000 }, async () => {
  for (let cycle = 0; cycle < 3; cycle++) {
    const stack = await startStack({ slot: firstSlot, admission: () => ({ status: 'admitted', slots: 2 }) })
    let record
    try {
      assert.equal(await portListening(stack.ports.app), true)
      assert.equal(await portListening(stack.ports.harness), true)
    } finally {
      record = await stack.stop()
    }
    await assertStopped(stack)
    assert.deepEqual(await stack.stop(), record, `cycle ${cycle + 1} stop must be idempotent`)
  }
})

test('queued/stopped admission and zero slots cannot allocate or launch', async () => {
  const ports = await allocatePorts(firstSlot)
  const before = (await readdir(tmpdir())).filter((name) => name.startsWith(`leaf-walk-stack-${firstSlot}-`)).sort()
  let calls = 0
  await assert.rejects(startStack({ slot: firstSlot, admission: () => { calls++; return { status: 'queued', slots: 0 } } }), QueuedError)
  await assert.rejects(startStack({ slot: firstSlot, admission: () => { calls++; return { status: 'stopped' } } }), StoppedError)
  await assert.rejects(startStack({ slot: firstSlot, slots: 999, admission: () => { calls++; return { status: 'admitted', slots: 0 } } }), QueuedError)
  const previous = process.env.LEAF_WALK_SLOTS
  try {
    process.env.LEAF_WALK_SLOTS = '0'
    await assert.rejects(startStack({ slot: firstSlot, slots: 999, admission: () => { calls++; return { status: 'admitted', slots: 999 } } }), QueuedError)
  } finally {
    if (previous === undefined) delete process.env.LEAF_WALK_SLOTS
    else process.env.LEAF_WALK_SLOTS = previous
  }
  assert.equal(calls, 4, 'exactly one admission decision per attempted launch')
  assert.deepEqual((await readdir(tmpdir())).filter((name) => name.startsWith(`leaf-walk-stack-${firstSlot}-`)).sort(), before)
  assert.deepEqual(await Promise.all(Object.values(ports).map(portListening)), [false, false, false, false, false])
})

test('missing solver fails closed before database creation or launch', { timeout: 60000 }, async () => {
  const parent = await mkdtemp(join(tmpdir(), 'leaf-walk-solver-'))
  const empty = join(parent, 'empty')
  const previous = process.env.AUTOFILL_SOLVER_ROOT
  const adminUrl = stackAdminUrl(process.env.LEAF_WALK_PG_ADMIN_URL || process.env.LEAF_GATE_DATABASE_URL || 'postgresql://leaf@127.0.0.1:25432/postgres')
  // Use a slot outside the database helper tests so their parallel CREATE and
  // DROP operations cannot change this preflight's catalog comparison.
  const slot = 474
  const catalog = () => queryPostgres(adminUrl, "SELECT datname FROM pg_database WHERE starts_with(datname, %s) ORDER BY datname", [`leaf_walk_${slot}_`])
  let admissions = 0
  try {
    await mkdir(empty)
    const before = await catalog()
    process.env.AUTOFILL_SOLVER_ROOT = empty
    const missing = (error) => {
      assert.ok(error instanceof SolverMissingError)
      assert.equal(error.code, 'SOLVER_MISSING')
      assert.deepEqual(error.paths, [join(empty, 'solver.py')])
      assert.ok(error.message.includes(join(empty, 'solver.py')))
      return true
    }
    await assert.rejects(resolveStackSolver({ repoParent: parent }), missing)
    await assert.rejects(startStack({ slot, postgres: { adminUrl },
      admission: () => { admissions++; return { status: 'admitted', slots: 2 } },
    }), missing)
    assert.equal(admissions, 0, 'solver preflight must precede admission and database provisioning')
    assert.deepEqual(await catalog(), before, 'missing solver must not create a database')
    await assert.rejects(resolveStackSolver({ repoParent: parent, env: {} }), (error) => {
      assert.ok(error instanceof SolverMissingError)
      assert.deepEqual(error.paths, [join(parent, 'autofill-solver', 'solver.py')])
      return true
    })
    const root = join(parent, 'autofill-solver')
    await mkdir(root)
    await writeFile(join(root, 'solver.py'), '# resolver fixture\n')
    assert.deepEqual(await resolveStackSolver({ repoParent: parent, env: {} }), { AUTOFILL_SOLVER_ROOT: root })
    assert.deepEqual(await resolveStackSolver({ repoParent: parent, env: {
      AUTOFILL_SOLVER_ROOT: root, AUTOFILL_SOLVER_REVISION: 'fixture-revision',
    } }), { AUTOFILL_SOLVER_ROOT: root, AUTOFILL_SOLVER_REVISION: 'fixture-revision' })
  } finally {
    if (previous === undefined) delete process.env.AUTOFILL_SOLVER_ROOT
    else process.env.AUTOFILL_SOLVER_ROOT = previous
    await rm(parent, { recursive: true, force: true })
  }
})

test('unreachable PostgreSQL fails closed before creating a stack root or launching services', { timeout: 60000 }, async () => {
  const ports = await allocatePorts(firstSlot)
  const before = (await readdir(tmpdir())).filter((name) => name.startsWith(`leaf-walk-stack-${firstSlot}-`)).sort()
  await assert.rejects(startStack({ slot: firstSlot, admission: () => ({ status: 'admitted', slots: 2 }),
    postgres: { adminUrl: 'postgresql://leaf@127.0.0.1:1/postgres' },
  }), (error) => {
    assert.ok(error instanceof PostgresUnavailableError)
    assert.equal(error.code, 'postgres_unavailable')
    assert.ok(error.cause instanceof Error)
    assert.ok(error.cause.message.length > 0, 'retain the redacted driver diagnostic')
    return true
  })
  assert.deepEqual((await readdir(tmpdir())).filter((name) => name.startsWith(`leaf-walk-stack-${firstSlot}-`)).sort(), before)
  assert.deepEqual(await Promise.all(Object.values(ports).map(portListening)), [false, false, false, false, false])
})

test('proxy streams before producer EOF, limits bodies, times out, and reports upstream failure', { timeout: 30000 }, async () => {
  let endStream
  let producerEnded = false
  const producerGate = new Promise((accept) => { endStream = accept })
  const upstream = http.createServer((req, res) => {
    if (req.url === '/api/events') {
      res.writeHead(200, { 'content-type': 'text/event-stream' })
      res.flushHeaders()
      res.write('event: first\ndata: {"value":1}\n\n')
      void producerGate.then(() => { producerEnded = true; res.end('event: last\ndata: {}\n\n') })
    } else if (req.url === '/api/idle') {
      // Deliberately never respond: the proxy's idle deadline must answer 502.
    } else {
      req.resume()
      res.end('upstream')
    }
  })
  const appPort = await listen(upstream)
  // A temporary HTTP bind discovers a free proxy port without a fixed PID or
  // touching any of the worker slots. The proxy itself binds strictly.
  const reservation = http.createServer()
  const port = await listen(reservation)
  await close(reservation)
  let proxy
  let stream
  try {
    proxy = await startSameOriginProxy({ port, appPort, bundleDir, idleTimeoutMs: 1000, connectTimeoutMs: 500, maxBodyBytes: 64 })
    assert.ok(backendPaths.includes('/api'))
    const page = await request(proxy, '/app')
    assert.equal(page.status, 200)
    assert.match(page.text, /<html/i)
    stream = openSSE(proxy.baseURL + '/api/events')
    const frame = await stream.first
    assert.equal(producerEnded, false, 'first event must precede upstream EOF')
    assert.equal(frame.ended, false)
    assert.equal(frame.response.complete, false)
    assert.equal(frame.response.headers['x-accel-buffering'], 'no')
    assert.equal(frame.response.headers['content-encoding'], undefined)
    endStream()
    await until(() => stream.ended, 3000, 'SSE did not finish after producer EOF')
    const overLimit = await request(proxy, '/api/body', { method: 'POST', body: 'x'.repeat(65) })
    expectStatus(overLimit, 413)
    assert.match(overLimit.text, /body exceeds limit/)
    const idle = await request(proxy, '/api/idle')
    expectStatus(idle, 502)
    assert.match(idle.text, /upstream idle timeout/)
    await close(upstream)
    const failure = await request(proxy, '/api/health')
    expectStatus(failure, 502)
    assert.match(failure.text, /Stack upstream failed:.*ECONNREFUSED/)
  } finally {
    endStream()
    stream?.stop()
    if (proxy) await proxy.stop()
    if (upstream.listening) await close(upstream)
  }
  assert.equal(await portListening(port), false)
  assert.equal(await portListening(appPort), false)
})

test('two simultaneous real stacks isolate public API state and leave metrics without children', { timeout: 600000 }, async (t) => {
  let admissions = 0
  const seenSlots = []
  const admission = ({ slot, kind }) => {
    assert.equal(kind, 'walk_local')
    seenSlots.push(slot)
    admissions++
    return { status: 'admitted', slots: 2 }
  }
  const stacks = []
  const stopped = []
  const unsupported = []
  try {
    // Poison ambient database authority to prove it is excluded from both
    // children. The fake local identity is the SAME on A and B deliberately.
    const previous = process.env.DATABASE_URL
    const previousHarness = process.env.LEAF_HARNESS_DATABASE_URL
    let boots
    try {
      process.env.DATABASE_URL = 'postgresql://must-not-leak.invalid/host'
      process.env.LEAF_HARNESS_DATABASE_URL = 'postgresql://must-not-leak.invalid/harness'
      boots = await Promise.allSettled([firstSlot, firstSlot + 1].map((slot) => startStack({ slot, admission, slots: 999 })))
    } finally {
      if (previous === undefined) delete process.env.DATABASE_URL
      else process.env.DATABASE_URL = previous
      if (previousHarness === undefined) delete process.env.LEAF_HARNESS_DATABASE_URL
      else process.env.LEAF_HARNESS_DATABASE_URL = previousHarness
    }
    for (const boot of boots) if (boot.status === 'fulfilled') stacks.push(boot.value)
    const failures = boots.filter((boot) => boot.status === 'rejected')
    if (failures.length) throw new AggregateError(failures.map((boot) => boot.reason), 'Both isolated stacks are required; prerequisite or admission failure')
    const [a, b] = stacks
    assert.equal(admissions, 2)
    assert.deepEqual(seenSlots.sort((x, y) => x - y), [firstSlot, firstSlot + 1])

    await t.test('all ten disjoint ports answer and all stores are private', async () => {
      assert.equal(new Set([...Object.values(a.ports), ...Object.values(b.ports)]).size, 10)
      assert.notEqual(a.root, b.root)
      for (const stack of stacks) {
        const outside = relative(repo, stack.root)
        assert.ok(outside === '..' || outside.startsWith('..' + sep) || isAbsolute(outside), 'temp root must be outside checkout')
        assert.equal(Object.hasOwn(stack.env, 'DATABASE_URL'), false)
        assert.equal(Object.hasOwn(stack.env, 'LEAF_HARNESS_DATABASE_URL'), false)
        assert.equal(stack.env.LEAF_UPLOAD_IMPORT_MUTATIONS_ENABLED, '1', 'the isolated upload lane must be explicitly enabled')
        for (const [name, value] of Object.entries(stack.env)) if (/^(LEAF_|BROKER_|SESSIONS_DB|JOBS_DB|PENDING_REAPS_PATH)/.test(name) && /(_DIR|_BASE|_FILE|_DB|_PATH|LEDGER|AUDIT|BROKER_TENANTS)$/.test(name)) {
          assert.equal(value.startsWith(stack.root + sep), true, `${name} escaped ${stack.root}: ${value}`)
        }
        for (const [role, port] of Object.entries(stack.ports)) {
          assert.equal(await portListening(port), true, `${role}:${port} did not listen`)
          const path = role === 'broker' ? '/broker/health' : role === 'harness' ? '/health' : ['web', 'proxy'].includes(role) ? '/' : '/api/health'
          const result = await request({ baseURL: `http://127.0.0.1:${port}` }, path)
          assert.ok(result.status >= 200 && result.status < 300, `${role}: ${JSON.stringify(result)}`)
        }
        assert.ok(stack.pids.size >= 4, 'launcher plus app, broker and real harness PIDs must be recorded')
      }
      let thirdCalls = 0
      await assert.rejects(startStack({ slot: firstSlot + 2, slots: 999, admission: () => { thirdCalls++; return { status: 'admitted', slots: 999 } } }), QueuedError)
      assert.equal(thirdCalls, 1, 'override must not raise the two-stack machine cap')
      assert.equal(admissions, 2, 'two successful launches consulted the hook twice')
    })

    let drawingId
    await t.test('uploaded drawing on A is absent on B under the same local user', async () => {
      const dxf = await readFile(join(repo, 'web/e2e/fixtures/distinctive-panel.dxf'))
      const form = new FormData()
      form.append('file', new Blob([dxf], { type: 'application/dxf' }), 'walk-isolation.dxf')
      const uploaded = expectStatus(await request(a, '/api/drawings/upload', { method: 'POST', body: form }), 202)
      assert.equal(uploaded.tenant_id, 'demo-tenant')
      assert.equal(uploaded.tenant_kind, 'account')
      drawingId = uploaded.drawing_id
      assert.match(drawingId, /^[0-9a-f-]{36}$/i)
      await until(async () => {
        const status = expectStatus(await request(a, `/api/drawings/${drawingId}/upload-status`), 200)
        assert.notEqual(status.status, 'failed', JSON.stringify(status))
        return status.status === 'ready'
      }, 30000, 'Uploaded drawing did not finish extraction')
      const intakeA = expectStatus(await request(a, `/api/drawings/${drawingId}/intake`), 200)
      const uploadMarker = intakeA.intake?.dwg
      assert.ok(typeof uploadMarker === 'string' && /\.dxf$/i.test(uploadMarker), 'A must expose the uploaded DXF source marker')
      expectStatus(await request(b, `/api/drawings/${drawingId}/upload-status`), 404)
      const foreign = await request(b, `/api/drawings/${drawingId}/intake`)
      assert.ok(foreign.status === 404 || foreign.status === 200, JSON.stringify(foreign))
      if (foreign.status === 200) {
        assert.notDeepEqual(foreign.body, intakeA, 'B must not return A\'s uploaded intake')
        assert.equal(JSON.stringify(foreign.body).includes(uploadMarker), false, 'B must not contain A\'s uploaded DXF source marker')
      }
    })

    let sessionId
    await t.test('session created on A is rejected and unlisted on B', async () => {
      assert.ok(drawingId, 'drawing probe must have created a real drawing')
      const session = expectStatus(await request(a, '/api/sessions', { method: 'POST', json: { drawing_id: drawingId } }), 200)
      sessionId = session.session_id
      assert.ok(typeof sessionId === 'string' && sessionId.length > 0)
      expectStatus(await request(a, `/api/sessions/${sessionId}/transcript`), 200)
      const foreign = expectStatus(await request(b, `/api/sessions/${sessionId}/transcript`), 404)
      assert.equal(foreign.error.error_code, 'session_not_found')
      const listed = expectStatus(await request(b, '/api/sessions'), 200)
      assert.equal(listed.sessions.some((session) => session.session_id === sessionId), false)
    })

    await t.test('grant linked on A is absent and cannot be activated on B', async () => {
      const fake = 'FAKE-OAUTH-not-a-real-token-' + randomUUID()
      const linked = expectStatus(await request(a, '/api/tenant/claude-grant', { method: 'POST', json: { token: fake, kind: 'oauth', label: 'walk fixture', plan: 'pro' } }), 200)
      assert.equal(linked.linked, true)
      assert.ok(linked.active_account_id)
      assert.ok(linked.accounts.some((account) => account.id === linked.active_account_id))
      assert.equal(JSON.stringify(linked).includes(fake), false)
      const foreign = expectStatus(await request(b, '/api/tenant/claude-grant'), 200)
      assert.equal(foreign.linked, false)
      assert.equal(foreign.active_account_id, null)
      assert.deepEqual(foreign.accounts, [])
      expectStatus(await request(b, '/api/tenant/claude-grant', { method: 'PATCH', json: { account_id: linked.active_account_id } }), 400)
    })

    await t.test('quota charged on A leaves B a separate first-attempt allowance', async () => {
      const options = { method: 'POST', headers: { 'X-Tenant-Id': 'walk-quota-' + randomUUID() }, json: { description: 'Create a simple local fixture tool.' } }
      // No PostgreSQL writer lease: the harness refuses authoring. The real
      // public quota still CHARGES the attempt before that refusal. Keep the
      // author/template fallback off, so no source file is ever fabricated.
      const firstA = await request(a, '/api/author', options)
      expectStatus(firstA, 502)
      assert.match(firstA.text, /harness/i)
      const cappedA = expectStatus(await request(a, '/api/author', options), 429)
      assert.equal(cappedA.quota_kind, 'daily_author')
      assert.equal(cappedA.limit, 1)
      assert.equal(cappedA.used, 1)
      const firstB = await request(b, '/api/author', options)
      expectStatus(firstB, 502)
      assert.match(firstB.text, /harness/i)
      const cappedB = expectStatus(await request(b, '/api/author', options), 429)
      assert.equal(cappedB.quota_kind, 'daily_author')
      assert.equal(cappedB.used, 1)
    })

    await t.test('project creation explicitly reports UNSUPPORTED_LOCAL with server evidence', async () => {
      const org = randomUUID()
      const replies = []
      for (const stack of stacks) {
        const result = await request(stack, '/api/projects', { method: 'POST', headers: { 'X-Org-Id': org }, json: { name: 'walk-isolation-' + randomUUID() } })
        expectStatus(result, 500)
        assert.equal(result.body.error.error_code, 'INTERNAL')
        assert.match(result.body.error.message, /^internal server error \(error_id: [0-9a-f]{16}\)$/)
        await until(() => /DATABASE_URL is not set/.test(stack.output()), 5000, 'Project failure lacks the server missing-PostgreSQL diagnostic')
        const readiness = expectStatus(await request(stack, '/api/ready'), 200)
        assert.equal(readiness.dependencies.database.state, 'degraded')
        assert.equal(readiness.dependencies.database.required, false)
        replies.push({ status: result.status, body: result.body, serverDiagnostic: stack.output().split('\n').find((line) => line.includes('DATABASE_URL is not set')), database: readiness.dependencies.database })
      }
      unsupported.push({ feature: 'project', reason: 'No private PostgreSQL authority configured', responses: replies })
    })

    await t.test('real session SSE delivers an event while the public stream is open', async () => {
      assert.ok(sessionId, 'session probe must have created a real session')
      const stream = openSSE(`${a.baseURL}/api/sessions/${sessionId}/stream`, tenantHeaders)
      try {
        expectStatus(await request(a, `/api/sessions/${sessionId}/messages`, { method: 'POST', json: { text: 'Say hello to the local drawing.' } }), 202)
        const frame = await stream.first
        assert.equal(frame.ended, false)
        assert.equal(frame.response.complete, false)
        assert.match(frame.text, /event: turn_started\ndata: /)
        assert.equal(frame.response.headers['x-accel-buffering'], 'no')
      } finally { stream.stop() }
    })
  } finally {
    console.log('UNSUPPORTED_LOCAL ' + JSON.stringify(unsupported))
    console.log('NOTES Unknown drawing intake may return the bundled rooftop_demo.dwg with status 200; isolation requires distinct content without A\'s uploaded source marker, while upload-status remains 404.')
    const results = await Promise.allSettled(stacks.map((stack) => stack.stop()))
    for (const result of results) if (result.status === 'fulfilled') stopped.push(result.value)
    const failures = results.filter((result) => result.status === 'rejected')
    if (failures.length) throw new AggregateError(failures.map((result) => result.reason), 'Stack teardown failed')
  }

  await t.test('teardown is idempotent and leaves neither listeners, roots nor child PIDs', async () => {
    assert.equal(stacks.length, 2)
    for (let i = 0; i < stacks.length; i++) {
      await assertStopped(stacks[i])
      assert.deepEqual(await stacks[i].stop(), stopped[i])
    }
  })
  await t.test('both per-stack JSON metrics contain boot time and measured process-tree RSS', async () => {
    assert.equal(stopped.length, 2)
    for (const record of stopped) {
      assert.equal(record.metricsPath, join(dirname(record.root), `leaf-walk-stack-metrics-${record.slot}.json`))
      const saved = JSON.parse(await readFile(record.metricsPath, 'utf8'))
      assert.deepEqual(saved, record)
      assert.ok(Number.isFinite(saved.bootSeconds) && saved.bootSeconds > 0)
      assert.ok(Number.isFinite(saved.peakRssBytes) && saved.peakRssBytes > 0)
      assert.ok(Number.isInteger(saved.rssSamples) && saved.rssSamples > 0)
      assert.ok(saved.pids.length >= 4)
      console.log('STACK_METRICS ' + JSON.stringify(saved))
    }
  })
})

test('two PostgreSQL-backed stacks isolate projects and run authored work', { timeout: 600000 }, async (t) => {
  const adminUrl = stackAdminUrl(process.env.LEAF_WALK_PG_ADMIN_URL || process.env.LEAF_GATE_DATABASE_URL || 'postgresql://leaf@127.0.0.1:25432/postgres')
  const stacks = []
  const unsupported = []
  const admission = () => ({ status: 'admitted', slots: 2 })
  try {
    // Exercise postgres:true's documented environment resolution while also
    // poisoning host authority. The fixture must supply both child DSNs itself.
    const previous = { admin: process.env.LEAF_WALK_PG_ADMIN_URL, database: process.env.DATABASE_URL, harness: process.env.LEAF_HARNESS_DATABASE_URL }
    let boots
    try {
      process.env.LEAF_WALK_PG_ADMIN_URL = adminUrl
      process.env.DATABASE_URL = 'postgresql://must-not-leak.invalid/host'
      process.env.LEAF_HARNESS_DATABASE_URL = 'postgresql://must-not-leak.invalid/harness'
      boots = await Promise.allSettled([firstSlot, firstSlot + 1].map((slot) => startStack({ slot, admission, postgres: true })))
    } finally {
      for (const [key, value] of [['LEAF_WALK_PG_ADMIN_URL', previous.admin], ['DATABASE_URL', previous.database], ['LEAF_HARNESS_DATABASE_URL', previous.harness]]) {
        if (value === undefined) delete process.env[key]
        else process.env[key] = value
      }
    }
    for (const boot of boots) if (boot.status === 'fulfilled') stacks.push(boot.value)
    const failures = boots.filter((boot) => boot.status === 'rejected')
    if (failures.length) throw new AggregateError(failures.map((boot) => boot.reason),
      'Both PostgreSQL stacks must boot; database or canonical worker prerequisite failed:\n' +
      failures.map((boot) => boot.reason.stack || String(boot.reason)).join('\n'))
    const [a, b] = stacks
    assert.notEqual(a.database.name, b.database.name)
    for (const stack of stacks) {
      assert.equal(stack.env.DATABASE_URL, stack.database.url)
      assert.equal(stack.env.LEAF_HARNESS_DATABASE_URL, stack.database.url)
      assert.equal(stack.env.LEAF_AUTHORED_EXECUTION, '0', 'local queued solves must not arm the E2B-only authored execution boundary')
      assert.equal(stack.env.AUTOFILL_SOLVER_ROOT, resolve(process.env.AUTOFILL_SOLVER_ROOT || join(dirname(repo), 'autofill-solver')))
      assert.equal(stack.env.AUTOFILL_SOLVER_REVISION, process.env.AUTOFILL_SOLVER_REVISION || undefined)
      assert.deepEqual(await queryPostgres(stack.database.url, 'SELECT current_database() AS name'), [{ name: stack.database.name }])
      const ready = expectStatus(await request(stack, '/api/ready'), 200)
      assert.equal(ready.dependencies.database.state, 'ready', JSON.stringify(ready))
      assert.equal(ready.dependencies.worker.state, 'ready', JSON.stringify(ready))
      assert.equal(ready.dependencies.worker.required, true)
    }

    const org = expectStatus(await request(a, '/api/orgs', { method: 'POST', json: { name: 'walk PostgreSQL ' + randomUUID(), tier: 'hosted_pro' } }), 200).org
    const headers = { 'X-Org-Id': org.org_id, 'X-Tenant-Id': org.org_id }
    let project
    await t.test('project created through the public API on A is absent on B', async () => {
      project = expectStatus(await request(a, '/api/projects', { method: 'POST', headers, json: { name: 'walk isolated project ' + randomUUID() } }), 200).project
      assert.match(project.project_id, /^[0-9a-f-]{36}$/i)
      expectStatus(await request(a, `/api/projects/${project.project_id}`, { headers }), 200)
      expectStatus(await request(b, `/api/projects/${project.project_id}`, { headers }), 404)
      const listedA = expectStatus(await request(a, '/api/projects', { headers }), 200)
      const listedB = expectStatus(await request(b, '/api/projects', { headers }), 200)
      assert.ok(listedA.projects.some((item) => item.project_id === project.project_id))
      assert.equal(listedB.projects.some((item) => item.project_id === project.project_id), false)
    })

    await t.test('a real canonical queued solve completes on A and is invisible on B', async () => {
      assert.ok(project, 'the public project factory must succeed first')
      // Seed only the input drawing version, matching the canonical server
      // walkthrough; project creation, enqueue and all reads remain public API.
      const drawingId = randomUUID()
      const versionId = randomUUID()
      await queryPostgres(a.database.url,
        'INSERT INTO drawing_artifacts (drawing_id, project_id, org_id, name) VALUES (%s, %s, %s, %s)',
        [drawingId, project.project_id, org.org_id, 'walk solver input'])
      await queryPostgres(a.database.url,
        'INSERT INTO drawing_versions (version_id, drawing_id, project_id, org_id, seq, oss_object, intake_ref, created_by) VALUES (%s, %s, %s, %s, 1, %s, %s, %s)',
        [versionId, drawingId, project.project_id, org.org_id, 'walk/roof.dwg', 'walk/roof-intake.json', 'walk-fixture'])
      const catalog = expectStatus(await request(a, '/api/tools', { headers }), 200)
      const tool = catalog.tools.find((item) => item.name === 'string-autofill-opt')
      assert.ok(tool?.catalog_digest, 'the server must issue the canonical tool confirmation digest')
      const runHeaders = { ...headers, 'X-Project-Id': project.project_id, 'Idempotency-Key': 'walk-solve-' + randomUUID() }
      const submitted = expectStatus(await request(a, '/api/run', { method: 'POST', headers: runHeaders, json: {
        tool: tool.name, catalog_digest: tool.catalog_digest, dwg: versionId,
        params: {
          groups: [
            { handle: 'A', name: 'A', count: 25, centroidX: 0, centroidY: 0, electricalZone: 'Z', elevationZone: '' },
            { handle: 'B', name: 'B', count: 15, centroidX: 10, centroidY: 0, electricalZone: 'Z', elevationZone: '' },
          ], panelsPerString: 10,
          options: { drainThreshold: 23, drainDiscount: 0, activeGroupPenalty: 10, concentrationBias: 0.15, clusterMarginPitches: 2 },
        },
      } }), 202)
      assert.ok(submitted.job_id)
      let terminal
      await until(async () => {
        const record = expectStatus(await request(a, `/api/jobs/${submitted.job_id}`, { headers }), 200)
        if (['complete', 'failed'].includes(record.status)) { terminal = record; return true }
        assert.ok(['submitted', 'running'].includes(record.status), JSON.stringify(record))
        return false
      }, 90000, 'Canonical worker did not claim and terminate the queued job')
      assert.ok(terminal.attempt >= 1, 'terminal job must carry an actual worker claim')
      assert.ok(terminal.started_at && terminal.finished_at)
      assert.equal(terminal.execution_context.authority_mode, 'postgres_canonical')
      assert.equal(terminal.provenance.attempt, terminal.attempt)
      assert.equal(terminal.status, 'complete', JSON.stringify(terminal))
      assert.deepEqual(terminal.result.solver_result.groupTargets, { A: 40, B: 0 })
      assert.equal(terminal.result.result_sha256, '525e2d417d916ab896ab25525352783302c98f6f436631777731a4c08bb1ed59')
      assert.ok(terminal.result.solve_hash && terminal.result.history_hash)
      assert.ok(terminal.provenance.solver_revision)
      if (a.env.AUTOFILL_SOLVER_REVISION) assert.equal(terminal.provenance.solver_revision, a.env.AUTOFILL_SOLVER_REVISION)
      expectStatus(await request(b, `/api/jobs/${submitted.job_id}`, { headers }), 404)
      const jobsB = expectStatus(await request(b, '/api/jobs', { headers }), 200)
      assert.equal(jobsB.jobs.some((item) => item.job_id === submitted.job_id), false)
      assert.deepEqual(await queryPostgres(b.database.url, 'SELECT job_id FROM jobs WHERE job_id = %s', [submitted.job_id]), [])
    })

    await t.test('stop retries a failed database drop after its children are dead', async () => {
      const drop = a.database.drop
      a.database.drop = async () => { throw new Error('fixture PostgreSQL drop failure') }
      try {
        await assert.rejects(a.stop(), /fixture PostgreSQL drop failure/)
        assert.deepEqual(await Promise.all(Object.values(a.ports).map(portListening)), [false, false, false, false, false])
        assert.ok([...a.pids].every((pid) => !pidAlive(pid)), 'children must be dead before attempting the drop')
        assert.equal((await stat(a.root)).isDirectory(), true, 'failed drop must retain the root until cleanup can retry')
        assert.equal((await queryPostgres(adminUrl, 'SELECT datname FROM pg_database WHERE datname = %s', [a.database.name])).length, 1)
      } finally { a.database.drop = drop }
      await a.stop()
      await assertStopped(a)
      assert.deepEqual(await queryPostgres(adminUrl, 'SELECT datname FROM pg_database WHERE datname = %s', [a.database.name]), [])
    })
  } finally {
    // No PostgreSQL feature is silently labelled unsupported: a failed public
    // probe or real queued solve failure fails this test.
    console.log('UNSUPPORTED_LOCAL ' + JSON.stringify(unsupported))
    const stopped = await Promise.allSettled(stacks.map((stack) => stack.stop()))
    const cleanupFailures = stopped.filter((result) => result.status === 'rejected')
    if (cleanupFailures.length) throw new AggregateError(cleanupFailures.map((result) => result.reason), 'PostgreSQL stack teardown failed')
    for (const stack of stacks) {
      await assertStopped(stack)
      assert.deepEqual(await queryPostgres(adminUrl, 'SELECT datname FROM pg_database WHERE datname = %s', [stack.database.name]), [])
      await stack.stop()
    }
  }
  assert.equal(stacks.length, 2)
  assert.ok(unsupported.length < 1, 'PostgreSQL must remove the non-PostgreSQL project limitation')
})
