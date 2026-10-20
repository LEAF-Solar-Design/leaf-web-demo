import assert from 'node:assert/strict'
import { spawn, spawnSync } from 'node:child_process'
import { EventEmitter } from 'node:events'
import { mkdtemp, readFile, rm, stat, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import test from 'node:test'
import { PostgresUnavailableError, resolvePostgresBinaries, runPostgresCommand, startEphemeralPostgres } from './pgEphemeral.mjs'
import { queryPostgres } from './pgStack.mjs'

async function fakeBinaries(t, extraEnv = {}) {
  const root = await mkdtemp(join(tmpdir(), 'walk pg binaries-'))
  const log = join(root, 'calls.jsonl')
  const exitProcess = new EventEmitter()
  t.after(async () => {
    if (exitProcess.listenerCount('exit')) exitProcess.emit('exit')
    await rm(root, { recursive: true, force: true })
  })
  const script = `
    const fs = require('node:fs'), path = require('node:path')
    const name = path.basename(process.argv[1]).replace(/\\.exe$/, '')
    const args = process.argv.slice(2)
    fs.appendFileSync(process.env.PG_FAKE_LOG, JSON.stringify({ name, args, pid: process.pid }) + '\\n')
    if (process.env.PG_FAKE_HANG === name) setInterval(() => {}, 1000)
    else if (name === 'pg_isready' && process.env.PG_FAKE_NOT_READY) process.exit(1)
  `
  for (const name of ['initdb', 'pg_ctl', 'pg_isready']) {
    await writeFile(join(root, name + (process.platform === 'win32' ? '.exe' : '')), script, { mode: 0o700 })
  }
  // Run the fake scripts through Node on Windows too, without a shell or .cmd.
  const env = { ...process.env, PG_FAKE_LOG: log, ...extraEnv }
  const spawnCalls = []
  const options = {
    slot: 0, env: { LEAF_WALK_PG_BIN: root, PATH: '' }, exitProcess,
    spawnProcess: (binary, args, options) => {
      spawnCalls.push({ binary, args, options, sync: false })
      return spawn(process.execPath, [binary, ...args], { ...options, env })
    },
    spawnSyncProcess: (binary, args, options) => {
      spawnCalls.push({ binary, args, options, sync: true })
      return spawnSync(process.execPath, [binary, ...args], { ...options, env })
    },
  }
  return { root, log, options, exitProcess, spawnCalls,
    calls: async () => (await readFile(log, 'utf8')).trim().split('\n').map(JSON.parse) }
}

test('binary resolution prefers the override, then PATH, then both Homebrew prefixes', async () => {
  const checked = []
  const binaries = await resolvePostgresBinaries({ platform: 'darwin',
    env: { LEAF_WALK_PG_BIN: '/override', PATH: '/first:/second' },
    executable: async (path) => {
      checked.push(path)
      return [join('/override', 'initdb'), join('/second', 'pg_ctl'), join('/usr/local/opt/postgresql@16/bin', 'pg_isready')].includes(path)
    },
  })
  assert.deepEqual(binaries, { initdb: join('/override', 'initdb'), pg_ctl: join('/second', 'pg_ctl'),
    pg_isready: join('/usr/local/opt/postgresql@16/bin', 'pg_isready') })
  assert.deepEqual(checked, [join('/override', 'initdb'),
    ...['/override', '/first', '/second'].map((dir) => join(dir, 'pg_ctl')),
    ...['/override', '/first', '/second', '/opt/homebrew/opt/postgresql@16/bin', '/usr/local/opt/postgresql@16/bin']
      .map((dir) => join(dir, 'pg_isready'))])
  const windows = await resolvePostgresBinaries({ platform: 'win32', env: { PATH: 'one;two' }, executable: async (path) => path.startsWith('two') })
  assert.equal(windows.initdb, join('two', 'initdb.exe'))
})

test('missing binaries have a typed error naming every missing binary and searched location', async () => {
  await assert.rejects(resolvePostgresBinaries({ platform: 'linux', env: { LEAF_WALK_PG_BIN: '/override', PATH: '/bin' },
    executable: async () => false }), (error) => {
    assert.ok(error instanceof PostgresUnavailableError)
    assert.equal(error.name, 'PostgresUnavailableError')
    assert.equal(error.code, 'postgres_binaries_missing')
    assert.deepEqual(error.missing, ['initdb', 'pg_ctl', 'pg_isready'])
    for (const path of error.searched) assert.ok(error.message.includes(path))
    assert.equal(error.searched.length, 6)
    return true
  })
})

test('fake binaries witness exact init, start, readiness and idempotent stop arguments', async (t) => {
  const fake = await fakeBinaries(t)
  const pg = await startEphemeralPostgres(fake.options)
  t.after(() => pg.stop())
  assert.equal(fake.exitProcess.listenerCount('exit'), 1)
  assert.equal(pg.adminUrl, `postgresql://postgres@127.0.0.1:${pg.port}/postgres`)
  assert.ok(pg.port > 0)
  const calls = await fake.calls()
  assert.deepEqual(calls.map((row) => row.name), ['initdb', 'pg_ctl', 'pg_isready'])
  assert.deepEqual(calls[0].args, ['-D', pg.dataDir, '-A', 'trust', '-U', 'postgres', '--no-sync', '--locale=C', '-E', 'UTF8'])
  const socketDir = /\s/.test(pg.dataDir) ? `"${pg.dataDir}"` : pg.dataDir
  assert.deepEqual(calls[1].args, ['start', '-D', pg.dataDir, '-l', join(pg.dataDir, 'postgres.log'), '-w', '-t', '60',
    '-o', `-p ${pg.port} -k ${socketDir} -c listen_addresses=127.0.0.1 -c fsync=off`])
  assert.deepEqual(calls[2].args, ['-h', '127.0.0.1', '-p', String(pg.port), '-U', 'postgres', '-d', 'postgres'])
  await Promise.all([pg.stop(), pg.stop()])
  await pg.stop()
  await assert.rejects(stat(pg.dataDir), { code: 'ENOENT' })
  assert.equal(fake.exitProcess.listenerCount('exit'), 0)
  const stops = (await fake.calls()).filter((row) => row.args[0] === 'stop')
  assert.equal(stops.length, 1)
  assert.deepEqual(stops[0].args, ['stop', '-D', pg.dataDir, '-m', 'fast', '-w', '-t', '30'])
})

test('every child forces the C locale while preserving the rest of the process environment', async (t) => {
  const previous = { LC_ALL: process.env.LC_ALL, LANG: process.env.LANG }
  try {
    process.env.LC_ALL = 'invalid-walk-locale'
    process.env.LANG = 'invalid-walk-language'
    const fake = await fakeBinaries(t)
    const pg = await startEphemeralPostgres(fake.options)
    await pg.stop()
    await startEphemeralPostgres(fake.options)
    fake.exitProcess.emit('exit')
    assert.equal(fake.spawnCalls.length, 8)
    const stops = fake.spawnCalls.filter((call) => call.args[0] === 'stop')
    assert.deepEqual(stops.map((call) => call.sync), [false, true])
    for (const { options } of fake.spawnCalls) {
      assert.equal(options.env.LC_ALL, 'C')
      assert.equal(options.env.LANG, 'C')
      assert.deepEqual(options.env, { ...process.env, LC_ALL: 'C', LANG: 'C' })
    }
    assert.equal(process.env.LC_ALL, 'invalid-walk-locale')
    assert.equal(process.env.LANG, 'invalid-walk-language')
  } finally {
    for (const [name, value] of Object.entries(previous)) {
      if (value === undefined) delete process.env[name]
      else process.env[name] = value
    }
  }
})

test('a command deadline kills even a child that never reports close', async () => {
  const killed = []
  const child = new EventEmitter()
  child.kill = (signal) => { killed.push(signal) }
  const started = Date.now()
  await assert.rejects(runPostgresCommand('stuck', [], { timeoutMs: 20, spawnProcess: () => child }), /timed out after 20ms/)
  assert.deepEqual(killed, ['SIGKILL'])
  assert.ok(Date.now() - started < 2000)
})

test('a hanging initdb is killed and its temp directory is removed on failed boot', async (t) => {
  const fake = await fakeBinaries(t, { PG_FAKE_HANG: 'initdb' })
  await assert.rejects(startEphemeralPostgres({ ...fake.options, timeouts: { init: 3000 } }), /timed out/)
  const [call] = await fake.calls()
  assert.equal(call.name, 'initdb')
  await assert.rejects(stat(call.args[1]), { code: 'ENOENT' })
  assert.equal(fake.exitProcess.listenerCount('exit'), 0)
  // kill delivery/close may trail the timeout rejection by one event-loop turn.
  await new Promise((accept) => setTimeout(accept, 100))
  assert.throws(() => process.kill(call.pid, 0), { code: 'ESRCH' })
})

test('readiness has a total deadline and failed boot stops and deletes the server', async (t) => {
  const fake = await fakeBinaries(t, { PG_FAKE_NOT_READY: '1' })
  const started = Date.now()
  await assert.rejects(startEphemeralPostgres({ ...fake.options, timeouts: { ready: 5000, poll: 10 } }), /timed out/)
  assert.ok(Date.now() - started < 20000)
  const calls = await fake.calls()
  assert.ok(calls.some((row) => row.name === 'pg_isready'))
  assert.equal(calls.at(-1).args[0], 'stop')
  await assert.rejects(stat(calls[0].args[1]), { code: 'ENOENT' })
})

test('process exit registers bounded synchronous shutdown and directory cleanup', async (t) => {
  const fake = await fakeBinaries(t)
  const syncCalls = []
  const pg = await startEphemeralPostgres({ ...fake.options, spawnSyncProcess: (binary, args, options) => {
    syncCalls.push({ binary, args, options })
    return fake.options.spawnSyncProcess(binary, args, options)
  } })
  fake.exitProcess.emit('exit')
  assert.equal(syncCalls.length, 1)
  assert.equal(syncCalls[0].options.timeout, 35000)
  assert.equal(syncCalls[0].options.shell, false)
  assert.deepEqual(syncCalls[0].args, ['stop', '-D', pg.dataDir, '-m', 'fast', '-w', '-t', '30'])
  await assert.rejects(stat(pg.dataDir), { code: 'ENOENT' })
  fake.exitProcess.removeAllListeners()
})

test('real binaries start a private server, accept a connection and stop', { timeout: 180000 }, async (t) => {
  try { await resolvePostgresBinaries() } catch (error) {
    if (error.code !== 'postgres_binaries_missing') throw error
    t.skip(error.message)
    return
  }
  const pg = await startEphemeralPostgres({ slot: 0 })
  try {
    assert.deepEqual(await queryPostgres(pg.adminUrl, 'SELECT current_database() AS name'), [{ name: 'postgres' }])
  } finally { await pg.stop() }
  await assert.rejects(stat(pg.dataDir), { code: 'ENOENT' })
})
