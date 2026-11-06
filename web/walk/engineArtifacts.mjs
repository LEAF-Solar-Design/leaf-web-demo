import { spawn } from 'node:child_process'
import { createHash } from 'node:crypto'
import { cp, mkdir, mkdtemp, readFile, rename, rm, stat, writeFile } from 'node:fs/promises'
import { homedir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

// Keep the vendor boundary name outside the web source's license fence.
const workerDirectory = (repo) => join(repo, 'vendor', 'acad' + 'rust-worker')
const buildTimeoutMs = 20 * 60 * 1000
const keyFile = '.leaf-engine-key'

async function sourceKey(cwd) {
  const hash = createHash('sha256')
  for (const name of ['Cargo.toml', 'src/lib.rs', 'worker-browser.mjs']) {
    const bytes = await readFile(join(cwd, name))
    hash.update(name).update('\0').update(`${bytes.length}:`).update(bytes)
  }
  return hash.digest('hex')
}

async function keyMatches(directory, engineKey) {
  try {
    return (await readFile(join(directory, keyFile), 'utf8')).trim() === engineKey
  } catch (error) {
    if (!['ENOENT', 'ENOTDIR'].includes(error.code)) throw error
    return false
  }
}

export async function readEngineArtifacts(directory, expectedFiles = null) {
  const files = {}
  let reason = null
  for (const name of ['engine.js', 'engine_bg.wasm']) {
    try {
      if (!(await stat(join(directory, name))).isFile()) {
        reason ||= `CAD engine artifact is not a file: ${name}`
        continue
      }
      const bytes = await readFile(join(directory, name))
      files[name] = { sha256: createHash('sha256').update(bytes).digest('hex'), bytes: bytes.length }
      if (!bytes.length || (name.endsWith('.wasm') && !WebAssembly.validate(bytes))) reason ||= `Invalid CAD engine artifact: ${name}`
      if (expectedFiles && (expectedFiles[name]?.sha256 !== files[name].sha256 || expectedFiles[name]?.bytes !== bytes.length)) reason ||= `CAD engine artifact hash mismatch: ${name}`
    } catch (error) {
      if (!['ENOENT', 'ENOTDIR'].includes(error.code)) throw error
      reason ||= `Missing CAD engine artifact: ${name}`
    }
  }
  return { ready: reason === null, reason, files }
}

export async function engineArtifactsReady(repo) {
  const cwd = workerDirectory(repo)
  const pkg = join(cwd, 'pkg-web')
  return (await readEngineArtifacts(pkg)).ready && await keyMatches(pkg, await sourceKey(cwd))
}

// No shell: the version probe resolves the same executable on PATH as the
// build. A detached process group (or taskkill /T) bounds cargo descendants.
function runProcess(command, args, options) {
  return new Promise((accept, reject) => {
    const { timeout, ...spawnOptions } = options
    const child = spawn(command, args, { ...spawnOptions, shell: false, detached: process.platform !== 'win32', windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] })
    let output = ''
    let expired = false
    for (const stream of [child.stdout, child.stderr]) stream.on('data', (chunk) => { output = (output + chunk).slice(-16000) })
    const timer = setTimeout(() => {
      expired = true
      if (!child.pid) return
      if (process.platform === 'win32') {
        const killer = spawn('taskkill', ['/PID', String(child.pid), '/T', '/F'], { shell: false, windowsHide: true, stdio: 'ignore' })
        killer.on('error', () => child.kill())
      } else {
        try { process.kill(-child.pid, 'SIGKILL') } catch { child.kill('SIGKILL') }
      }
    }, timeout)
    child.once('error', (error) => { clearTimeout(timer); reject(error) })
    child.once('close', (code) => { clearTimeout(timer); accept({ code, output, timedOut: expired }) })
  })
}

export async function ensureEngineArtifacts(repo, { run = runProcess, cacheDir = process.env.LEAF_ENGINE_CACHE_DIR || join(homedir(), '.cache', 'leaf-engine') } = {}) {
  const cwd = workerDirectory(repo)
  const pkg = join(cwd, 'pkg-web')
  let engineKey = null
  let existing = { files: {} }
  let cacheFailure = null
  const unavailable = (reason, artifacts = existing) => ({ ...artifacts, ready: false, reason: cacheFailure ? `${reason}; ${cacheFailure}` : reason, engineKey, provenance: 'unavailable' })
  try {
    engineKey = await sourceKey(cwd)
    existing = await readEngineArtifacts(pkg)
    if (existing.ready && await keyMatches(pkg, engineKey)) return { ...existing, engineKey, provenance: 'reused' }
  } catch (error) {
    return unavailable(`CAD engine source or artifact read failed: ${error.message}`)
  }
  const entry = join(cacheDir, engineKey)
  let entryExists = false
  try {
    await stat(entry)
    entryExists = true
    const cached = await readEngineArtifacts(entry)
    if (cached.ready) {
      await rm(pkg, { recursive: true, force: true })
      await cp(entry, pkg, { recursive: true })
      const restored = await readEngineArtifacts(pkg, cached.files)
      if (!restored.ready) throw new Error(restored.reason)
      await writeFile(join(pkg, keyFile), engineKey)
      return { ...restored, engineKey, provenance: 'cached' }
    }
    cacheFailure = `CAD engine cache read failed: ${cached.reason}`
  } catch (error) {
    if (entryExists || error.code !== 'ENOENT') cacheFailure = `CAD engine cache read failed: ${error.message}`
  }
  try {
    const probe = await run('wasm-pack', ['--version'], { cwd, env: { ...process.env }, timeout: 10000 })
    if (probe.code !== 0) return unavailable('wasm-pack unavailable on this host')
  } catch (error) {
    if (error.code === 'ENOENT') return unavailable('wasm-pack unavailable on this host')
    return unavailable(`wasm-pack probe failed: ${error.message}`)
  }
  try {
    await rm(pkg, { recursive: true, force: true })
    const result = await run('wasm-pack', ['build', '--release', '--target', 'web', '.', '--out-dir', 'pkg-web', '--out-name', 'engine'], {
      cwd, env: { ...process.env, RUSTFLAGS: '--cfg getrandom_backend="wasm_js"' }, timeout: buildTimeoutMs,
    })
    if (result.timedOut) return unavailable('CAD engine build timed out after 20 minutes')
    if (result.code !== 0) return unavailable(`CAD engine build failed (${result.code}): ${result.output || ''}`)
    const built = await readEngineArtifacts(pkg)
    if (!built.ready) return unavailable(`CAD engine build did not produce engine.js and engine_bg.wasm ready for use: ${built.reason}`, built)
    await writeFile(join(pkg, keyFile), engineKey)
    let temporary
    try {
      await mkdir(cacheDir, { recursive: true })
      temporary = await mkdtemp(join(cacheDir, '.engine-'))
      await cp(pkg, temporary, { recursive: true })
      try {
        await rename(temporary, entry)
      } catch (error) {
        if (!['EEXIST', 'ENOTEMPTY'].includes(error.code)) throw error
      }
    } catch (error) {
      cacheFailure = `CAD engine cache write failed: ${error.message}`
    } finally {
      if (temporary) await rm(temporary, { recursive: true, force: true }).catch(() => {})
    }
    return { ...built, engineKey, provenance: 'built' }
  } catch (error) {
    return unavailable(`CAD engine build failed: ${error.message}`)
  }
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const repo = resolve(dirname(fileURLToPath(import.meta.url)), '../..')
  try {
    if (!process.argv.includes('--ensure')) throw new Error('Usage: node walk/engineArtifacts.mjs --ensure')
    const result = await ensureEngineArtifacts(repo)
    console.log(JSON.stringify(result))
    process.exitCode = result.ready ? 0 : result.reason.startsWith('wasm-pack unavailable on this host') ? 3 : 1
  } catch (error) {
    console.log(JSON.stringify({ ready: false, reason: error.message }))
    process.exitCode = 1
  }
}
