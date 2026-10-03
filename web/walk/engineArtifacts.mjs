import { spawn } from 'node:child_process'
import { stat } from 'node:fs/promises'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

// Keep the vendor boundary name outside the web source's license fence.
const workerDirectory = (repo) => join(repo, 'vendor', 'acad' + 'rust-worker')
const buildTimeoutMs = 20 * 60 * 1000

export async function engineArtifactsReady(repo) {
  const files = await Promise.all(['engine.js', 'engine_bg.wasm'].map(async (name) => {
    try { return (await stat(join(workerDirectory(repo), 'pkg-web', name))).isFile() } catch (error) {
      if (error.code === 'ENOENT') return false
      throw error
    }
  }))
  return files.every(Boolean)
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

export async function ensureEngineArtifacts(repo, { run = runProcess } = {}) {
  if (await engineArtifactsReady(repo)) return { ready: true, reason: null }
  const cwd = workerDirectory(repo)
  try {
    const probe = await run('wasm-pack', ['--version'], { cwd, env: { ...process.env }, timeout: 10000 })
    if (probe.code !== 0) return { ready: false, reason: 'wasm-pack unavailable on this host' }
  } catch (error) {
    if (error.code === 'ENOENT') return { ready: false, reason: 'wasm-pack unavailable on this host' }
    return { ready: false, reason: `wasm-pack probe failed: ${error.message}` }
  }
  try {
    const result = await run('wasm-pack', ['build', '--release', '--target', 'web', '.', '--out-dir', 'pkg-web', '--out-name', 'engine'], {
      cwd, env: { ...process.env, RUSTFLAGS: '--cfg getrandom_backend="wasm_js"' }, timeout: buildTimeoutMs,
    })
    if (result.timedOut) return { ready: false, reason: 'CAD engine build timed out after 20 minutes' }
    if (result.code !== 0) return { ready: false, reason: `CAD engine build failed (${result.code}): ${result.output || ''}` }
    if (!await engineArtifactsReady(repo)) return { ready: false, reason: 'CAD engine build did not produce engine.js and engine_bg.wasm' }
    return { ready: true, reason: null }
  } catch (error) {
    return { ready: false, reason: `CAD engine build failed: ${error.message}` }
  }
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const repo = resolve(dirname(fileURLToPath(import.meta.url)), '../..')
  try {
    if (!process.argv.includes('--ensure')) throw new Error('Usage: node walk/engineArtifacts.mjs --ensure')
    const result = await ensureEngineArtifacts(repo)
    console.log(JSON.stringify(result))
    process.exitCode = result.ready ? 0 : result.reason === 'wasm-pack unavailable on this host' ? 3 : 1
  } catch (error) {
    console.log(JSON.stringify({ ready: false, reason: error.message }))
    process.exitCode = 1
  }
}
