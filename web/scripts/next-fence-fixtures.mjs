// Two real Next exports shared by the bundle fences. Each build has its own
// source copy, .next and dist, so it cannot race the ordinary application build.
import { createHash } from 'node:crypto'
import { spawn, spawnSync } from 'node:child_process'
import { copyFileSync, existsSync, mkdirSync, readFileSync, readdirSync, realpathSync, renameSync, rmSync, symlinkSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { PUBLIC_ENV_KEYS } from './next-runtime.mjs'

const WEB = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const REPO = path.dirname(WEB)
const BUILD_TIMEOUT = 300_000
export const NEXT_FENCE_TIMEOUT = 660_000
const pending = new Map()
const hash = bytes => createHash('sha256').update(bytes).digest('hex')
const pause = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds))

function inputFiles() {
  const files = []
  function walk(relative) {
    const filename = path.join(REPO, relative)
    if (!existsSync(filename)) return
    for (const entry of readdirSync(filename, { withFileTypes: true }).sort((a, b) => a.name.localeCompare(b.name))) {
      if (['node_modules', '.git', '.next', 'dist', 'out', 'target'].includes(entry.name) || entry.name.startsWith('.env')) continue
      const child = path.join(relative, entry.name)
      if (entry.isSymbolicLink()) throw new Error(`Fixture source must be a regular file: ${child}`)
      if (entry.isDirectory()) walk(child)
      else if (entry.isFile()) files.push(child)
    }
  }
  for (const directory of ['web/src', 'web/public', 'web/webpack', 'web/vite-plugins', 'vendor/assistant-contract', 'vendor/assistant-react', 'vendor/acadrust-worker']) walk(directory)
  for (const filename of ['web/package.json', 'web/package-lock.json', 'web/next.config.mjs', 'web/scripts/next-build.mjs', 'web/scripts/next-runtime.mjs', 'web/scripts/bundle-assets.mjs', 'web/scripts/next-fence-fixtures.mjs']) {
    if (!existsSync(path.join(REPO, filename))) throw new Error(`Missing Next fixture input: ${filename}`)
    files.push(filename)
  }
  return files.sort()
}

function sourceFingerprint(files) {
  const digest = createHash('sha256').update(process.version)
  for (const filename of files) digest.update(filename).update('\0').update(readFileSync(path.join(REPO, filename)))
  // The installed compiler is part of the cache identity, not only its lockfile.
  for (const name of ['next', 'react', 'react-dom']) digest.update(readFileSync(path.join(WEB, 'node_modules', name, 'package.json')))
  return digest.digest('hex')
}

function artifactDigest(directory) {
  const digest = createHash('sha256')
  function walk(folder) {
    for (const entry of readdirSync(folder, { withFileTypes: true }).sort((a, b) => a.name.localeCompare(b.name))) {
      const filename = path.join(folder, entry.name)
      if (entry.isSymbolicLink()) throw new Error('Fixture output contains a symbolic link')
      if (entry.isDirectory()) walk(filename)
      else if (entry.isFile()) digest.update(path.relative(directory, filename)).update('\0').update(readFileSync(filename))
    }
  }
  walk(directory)
  return digest.digest('hex')
}

function ready(directory, source, variant) {
  try {
    const receipt = JSON.parse(readFileSync(path.join(directory, 'receipt.json'), 'utf8'))
    return receipt.source === source && receipt.variant === variant &&
      existsSync(path.join(directory, 'web/dist/_next/static')) &&
      receipt.output === artifactDigest(path.join(directory, 'web/dist'))
  } catch { return false }
}

async function build(directory, files, variant, source) {
  for (const relative of files) {
    const destination = path.join(directory, relative)
    mkdirSync(path.dirname(destination), { recursive: true })
    copyFileSync(path.join(REPO, relative), destination)
  }
  const web = path.join(directory, 'web')
  symlinkSync(realpathSync(path.join(WEB, 'node_modules')), path.join(web, 'node_modules'), process.platform === 'win32' ? 'junction' : 'dir')
  const env = { ...process.env }
  for (const key of PUBLIC_ENV_KEYS) delete env[key]
  delete env.LEAF_CAD_ENGINE_PKG_DIR
  Object.assign(env, {
    NODE_ENV: 'production', NEXT_TELEMETRY_DISABLED: '1', LEAF_ELEMENT_REF_STAMP: '1',
    LEAF_SOURCE_SHA: source, VITE_MOCK: '0', VITE_API_BASE: 'http://leaf-proof.invalid',
    VITE_TENANT_ID: 'next-bundle-fence', VITE_CAD_EDIT: variant === 'enabled' ? '1' : '0',
    VITE_LIFECYCLE_UI: variant === 'enabled' ? '1' : '0', VITE_IOS_SURFACE: variant === 'enabled' ? '1' : '0',
    VITE_SOLAR_FLOW_RAIL: variant === 'enabled' ? '1' : '0', VITE_SOLAR_SETTINGS_FORM: variant === 'enabled' ? '1' : '0',
  })
  await new Promise((resolve, reject) => {
    const child = spawn(process.execPath, ['scripts/next-build.mjs'], { cwd: web, env, detached: process.platform !== 'win32', stdio: ['ignore', 'pipe', 'pipe'] })
    let tail = ''
    const collect = bytes => { tail = (tail + bytes.toString()).slice(-20_000) }
    child.stdout.on('data', collect)
    child.stderr.on('data', collect)
    const timer = setTimeout(() => {
      if (process.platform === 'win32') spawnSync('taskkill', ['/PID', String(child.pid), '/T', '/F'], { stdio: 'ignore', timeout: 5000 })
      else { try { process.kill(-child.pid, 'SIGKILL') } catch { /* already exited */ } }
    }, BUILD_TIMEOUT)
    child.on('error', error => { clearTimeout(timer); reject(error) })
    child.on('close', (code, signal) => {
      clearTimeout(timer)
      writeFileSync(path.join(directory, 'build.log'), tail)
      if (code !== 0) reject(new Error(`Next ${variant} fixture failed (${signal || code}); ${path.join(directory, 'build.log')}\n${tail}`))
      else resolve()
    })
  })
  if (sourceFingerprint(inputFiles()) !== source) throw new Error('Source changed while producing the Next fixture; rerun against the settled source')
  const output = artifactDigest(path.join(web, 'dist'))
  writeFileSync(path.join(directory, 'receipt.json'), JSON.stringify({ source, variant, output }) + '\n')
}

export function nextFenceDirectory(variant = 'enabled') {
  if (!['enabled', 'disabled'].includes(variant)) throw new Error(`Unknown Next fixture: ${variant}`)
  if (pending.has(variant)) return pending.get(variant)
  const result = (async () => {
    const files = inputFiles()
    const source = sourceFingerprint(files)
    const cache = path.join(tmpdir(), 'leaf-next-fences', hash(REPO).slice(0, 16), source)
    const directory = path.join(cache, variant)
    // One lock for both variants keeps compiler work serial, including when
    // different Vitest processes reach different fences first.
    const lock = path.join(cache, 'build.lock')
    mkdirSync(cache, { recursive: true })
    const deadline = Date.now() + NEXT_FENCE_TIMEOUT - 10_000
    while (!ready(directory, source, variant)) {
      let acquired = false
      try { mkdirSync(lock); acquired = true } catch (error) { if (error.code !== 'EEXIST') throw error }
      if (!acquired) {
        if (Date.now() >= deadline) throw new Error(`Next fixture producer is still busy: ${lock}`)
        await pause(250)
        continue
      }
      const scratch = path.join(cache, `${variant}-${process.pid}-${Date.now()}`)
      try {
        if (!ready(directory, source, variant)) {
          await build(scratch, files, variant, source)
          // Only this source-scoped fixture directory is replaced. The app's
          // checkout and its canonical output never enter this path.
          rmSync(directory, { recursive: true, force: true })
          renameSync(scratch, directory)
        }
      } catch (error) {
        if (existsSync(path.join(scratch, 'build.log'))) copyFileSync(path.join(scratch, 'build.log'), path.join(cache, `${variant}-failed.log`))
        throw error
      } finally {
        rmSync(scratch, { recursive: true, force: true })
        rmSync(lock, { recursive: true, force: true })
      }
    }
    return path.join(directory, 'web/dist')
  })()
  pending.set(variant, result)
  return result
}
