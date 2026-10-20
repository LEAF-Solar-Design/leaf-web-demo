import { spawn, spawnSync } from 'node:child_process'
import { access, mkdtemp, rm, stat } from 'node:fs/promises'
import { constants, existsSync, rmSync } from 'node:fs'
import { createServer } from 'node:net'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { setTimeout as delay } from 'node:timers/promises'
import { PostgresUnavailableError as ServerUnavailableError } from './pgStack.mjs'

export class PostgresUnavailableError extends ServerUnavailableError {
  constructor(searched, missing) {
    super()
    this.code = 'postgres_binaries_missing'
    this.searched = searched
    this.missing = missing
    this.message = `PostgreSQL binaries missing (${missing.join(', ')}); searched: ${searched.join(', ') || '(empty PATH)'}`
  }
}

export async function resolvePostgresBinaries({ env = process.env, platform = process.platform,
  executable = async (path) => { try { await access(path, platform === 'win32' ? constants.F_OK : constants.X_OK); return (await stat(path)).isFile() } catch { return false } },
} = {}) {
  const directories = [...new Set([
    env.LEAF_WALK_PG_BIN,
    ...(env.PATH || env.Path || '').split(platform === 'win32' ? ';' : ':'),
    ...(platform === 'darwin' ? ['/opt/homebrew/opt/postgresql@16/bin', '/usr/local/opt/postgresql@16/bin'] : []),
  ].filter(Boolean))]
  const binaries = {}, searched = [], missing = []
  for (const name of ['initdb', 'pg_ctl', 'pg_isready']) {
    for (const directory of directories) {
      const path = join(directory, name + (platform === 'win32' ? '.exe' : ''))
      searched.push(path)
      if (await executable(path)) { binaries[name] = path; break }
    }
    if (!binaries[name]) missing.push(name)
  }
  if (missing.length) throw new PostgresUnavailableError(searched, missing)
  return binaries
}

// Every invocation has an independent deadline. Reject at the deadline even
// if a broken child never emits close after being killed.
export function runPostgresCommand(binary, args, { timeoutMs = 30000, spawnProcess = spawn } = {}) {
  return new Promise((accept, reject) => {
    const child = spawnProcess(binary, args, { windowsHide: true, shell: false, stdio: ['ignore', 'pipe', 'pipe'],
      env: { ...process.env, LC_ALL: 'C', LANG: 'C' } })
    let output = '', settled = false
    const finish = (error, code) => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      if (error) reject(error)
      else accept({ code, output })
    }
    const timer = setTimeout(() => {
      try { child.kill('SIGKILL') } catch (error) { finish(error); return }
      finish(new Error(`PostgreSQL command ${binary} timed out after ${timeoutMs}ms`))
    }, timeoutMs)
    for (const stream of [child.stdout, child.stderr]) stream?.on('data', (chunk) => { output = (output + chunk).slice(-8000) })
    child.once('error', (error) => finish(error))
    child.once('close', (code) => finish(null, code))
  })
}

async function freePort() {
  const reservation = createServer()
  await new Promise((accept, reject) => {
    reservation.once('error', reject)
    reservation.listen({ host: '127.0.0.1', port: 0, exclusive: true }, accept)
  })
  const { port } = reservation.address()
  await new Promise((accept, reject) => reservation.close((error) => error ? reject(error) : accept()))
  return port
}

export async function startEphemeralPostgres({ slot, env, platform, executable,
  spawnProcess = spawn, spawnSyncProcess = spawnSync, exitProcess = process,
  timeouts = {},
} = {}) {
  if (!Number.isInteger(slot) || slot < 0 || slot > 474) throw new RangeError('slot must be an integer from 0 through 474')
  const binaries = await resolvePostgresBinaries({ env, platform, executable })
  const limits = { init: 60000, start: 65000, ready: 15000, probe: 2000, poll: 100, stop: 35000, ...timeouts }
  for (const [name, value] of Object.entries(limits)) if (!Number.isFinite(value) || value <= 0) throw new RangeError(`${name} timeout must be positive`)
  const dataDir = await mkdtemp(join(tmpdir(), `leaf-walk-pg-${slot}-`))
  const stopArgs = ['stop', '-D', dataDir, '-m', 'fast', '-w', '-t', '30']
  const command = async (name, args, timeoutMs) => {
    const result = await runPostgresCommand(binaries[name], args, { timeoutMs, spawnProcess })
    if (result.code !== 0) throw new Error(`PostgreSQL ${name} failed (${result.code}): ${result.output}`)
    return result
  }
  let startAttempted = false, stopped = false, stopping
  const onExit = () => {
    if (stopped) return
    // exit cannot await promises. pg_ctl is bounded here too.
    if (startAttempted) {
      const result = spawnSyncProcess(binaries.pg_ctl, stopArgs, { windowsHide: true, shell: false, timeout: limits.stop, stdio: 'ignore',
        env: { ...process.env, LC_ALL: 'C', LANG: 'C' } })
      if (result.error || result.status !== 0 && existsSync(join(dataDir, 'postmaster.pid'))) { exitProcess.exitCode ||= 1; return }
    }
    rmSync(dataDir, { recursive: true, force: true })
    stopped = true
  }
  exitProcess.once('exit', onExit)
  const stop = () => {
    if (stopped) return Promise.resolve()
    if (!stopping) stopping = (async () => {
      if (startAttempted) {
        const result = await runPostgresCommand(binaries.pg_ctl, stopArgs, { timeoutMs: limits.stop, spawnProcess })
        if (result.code !== 0 && existsSync(join(dataDir, 'postmaster.pid'))) throw new Error(`PostgreSQL stop failed (${result.code}): ${result.output}`)
      }
      await rm(dataDir, { recursive: true, force: true })
      stopped = true
      exitProcess.removeListener('exit', onExit)
    })().finally(() => { stopping = undefined })
    return stopping
  }
  try {
    const port = await freePort()
    await command('initdb', ['-D', dataDir, '-A', 'trust', '-U', 'postgres', '--no-sync', '--locale=C', '-E', 'UTF8'], limits.init)
    startAttempted = true
    const socketDir = /\s/.test(dataDir) ? `"${dataDir.replaceAll('"', '\\"')}"` : dataDir
    // A logfile prevents postgres from keeping pg_ctl's captured pipes open.
    await command('pg_ctl', ['start', '-D', dataDir, '-l', join(dataDir, 'postgres.log'), '-w', '-t', '60',
      '-o', `-p ${port} -k ${socketDir} -c listen_addresses=127.0.0.1 -c fsync=off`], limits.start)
    const deadline = Date.now() + limits.ready
    let ready = false
    while (Date.now() < deadline) {
      const result = await runPostgresCommand(binaries.pg_isready, ['-h', '127.0.0.1', '-p', String(port), '-U', 'postgres', '-d', 'postgres'],
        { timeoutMs: Math.min(limits.probe, Math.max(1, deadline - Date.now())), spawnProcess })
      if (result.code === 0) { ready = true; break }
      await delay(Math.min(limits.poll, Math.max(1, deadline - Date.now())))
    }
    if (!ready) throw new Error(`PostgreSQL readiness timed out after ${limits.ready}ms`)
    return { adminUrl: `postgresql://postgres@127.0.0.1:${port}/postgres`, dataDir, port, stop }
  } catch (error) {
    try { await stop() } catch (cleanupError) { throw new AggregateError([error, cleanupError], 'Ephemeral PostgreSQL boot and cleanup failed') }
    throw error
  }
}
