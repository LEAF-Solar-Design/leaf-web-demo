#!/usr/bin/env node

import { spawn, spawnSync } from 'node:child_process'
import { createHash } from 'node:crypto'
import {
  copyFileSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  readdirSync,
  rmSync,
} from 'node:fs'
import { createServer as createNetServer } from 'node:net'
import os from 'node:os'
import path from 'node:path'
import { performance } from 'node:perf_hooks'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { gzipSync } from 'node:zlib'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const WEB_ROOT = path.resolve(HERE, '..')
const REPO_ROOT = path.resolve(WEB_ROOT, '..')
const ENGINE_ROOT = discoverEngineRoot()
const FIXTURE = path.join(ENGINE_ROOT, 'fixtures', 'one_line.dxf')
const PERF_HTML = path.join(WEB_ROOT, 'e2e', 'fixtures', 'cad-edit-perf.html')
const SAMPLES = 10
const P95_BUDGET_MS = 2_000
const DELTA_BUDGET_BYTES = 5_120
const WASM_PREREQUISITE = 'run wasm-pack build --release --target web . --out-dir pkg-web --out-name engine from the vendored browser-worker package directory'

function discoverEngineRoot() {
  const vendorRoot = path.join(REPO_ROOT, 'vendor')
  const candidates = readdirSync(vendorRoot, { withFileTypes: true })
    .filter((entry) => entry.isDirectory())
    .map((entry) => path.join(vendorRoot, entry.name))
    .filter((candidate) => (
      existsSync(path.join(candidate, 'worker-browser.mjs'))
      && existsSync(path.join(candidate, 'fixtures', 'one_line.dxf'))
    ))
  if (candidates.length !== 1) {
    throw new Error(`expected exactly one vendored browser-worker engine, found ${candidates.length}`)
  }
  return candidates[0]
}

function repoPath(candidate) {
  return path.relative(REPO_ROOT, candidate).split(path.sep).join('/')
}

function sha256(bytes) {
  return createHash('sha256').update(bytes).digest('hex')
}

/** Nearest-rank percentile. The input is never changed. */
export function percentile(values, rank) {
  if (!Array.isArray(values) || values.length === 0) {
    throw new TypeError('percentile requires at least one finite sample')
  }
  if (!Number.isFinite(rank) || rank <= 0 || rank > 100) {
    throw new RangeError('percentile rank must be greater than 0 and at most 100')
  }
  const sorted = values.map(Number).sort((a, b) => a - b)
  if (sorted.some((value) => !Number.isFinite(value))) {
    throw new TypeError('percentile samples must be finite numbers')
  }
  return sorted[Math.ceil((rank / 100) * sorted.length) - 1]
}

/**
 * Account for an entry's initial Vite graph. Only ENTRY plus transitive static
 * imports and their CSS are counted. Dynamic imports, workers, and wasm are
 * deliberately outside this base graph.
 */
export function accountManifestGraph(manifest, entryKey, assets) {
  if (!manifest || typeof manifest !== 'object' || !manifest[entryKey]) {
    throw new Error(`manifest entry not found: ${entryKey}`)
  }
  const chunks = new Set()
  const files = new Set()
  const visit = (key) => {
    if (chunks.has(key)) return
    const chunk = manifest[key]
    if (!chunk) throw new Error(`manifest static import not found: ${key}`)
    chunks.add(key)
    if (chunk.file) files.add(chunk.file)
    for (const css of chunk.css || []) files.add(css)
    for (const imported of chunk.imports || []) visit(imported)
  }
  visit(entryKey)

  const details = [...files].sort().map((file) => {
    const bytes = assets instanceof Map ? assets.get(file) : assets?.[file]
    if (bytes === undefined) throw new Error(`initial artifact not found: ${file}`)
    const content = Buffer.isBuffer(bytes) ? bytes : Buffer.from(bytes)
    return {
      file,
      bytes: content.length,
      gzip_bytes: gzipSync(content, { mtime: 0 }).length,
      sha256: sha256(content),
    }
  })
  return {
    entry: entryKey,
    files: details,
    gzip_bytes: details.reduce((total, item) => total + item.gzip_bytes, 0),
  }
}

function requirePath(candidate, message) {
  if (!existsSync(candidate)) throw new Error(message)
  return candidate
}

function filesWithExtension(root, extension) {
  const found = []
  const visit = (directory) => {
    for (const entry of readdirSync(directory, { withFileTypes: true })) {
      const candidate = path.join(directory, entry.name)
      if (entry.isDirectory()) visit(candidate)
      else if (entry.isFile() && entry.name.endsWith(extension)) found.push(candidate)
    }
  }
  visit(root)
  return found
}

function viteBin() {
  return requirePath(
    path.join(WEB_ROOT, 'node_modules', 'vite', 'bin', 'vite.js'),
    'prerequisite missing: run npm ci in web before this measurement',
  )
}

function findManifest(outDir) {
  const candidates = [
    path.join(outDir, '.vite', 'manifest.json'),
    path.join(outDir, 'manifest.json'),
  ]
  return requirePath(
    candidates.find(existsSync) || candidates[0],
    `Vite --manifest did not emit a manifest under ${outDir}`,
  )
}

function entryKey(manifest) {
  const entries = Object.entries(manifest).filter(([, value]) => value?.isEntry)
  if (entries.length !== 1) {
    throw new Error(`expected exactly one Vite ENTRY, found ${entries.length}`)
  }
  return entries[0][0]
}

function build(flag, outDir) {
  const result = spawnSync(
    process.execPath,
    [viteBin(), 'build', '--manifest', '--outDir', outDir, '--emptyOutDir'],
    {
      cwd: WEB_ROOT,
      env: { ...process.env, VITE_CAD_EDIT: flag },
      encoding: 'utf8',
      timeout: 240_000,
      windowsHide: true,
    },
  )
  if (result.status !== 0) {
    throw new Error(`Vite flag=${flag} build failed: ${(result.stderr || result.stdout || '').trim()}`)
  }
  const manifestPath = findManifest(outDir)
  const manifestBytes = readFileSync(manifestPath)
  const manifest = JSON.parse(manifestBytes)
  const entry = entryKey(manifest)
  const needed = new Set()
  const collect = (key) => {
    if (needed.has(key)) return
    needed.add(key)
    for (const imported of manifest[key]?.imports || []) collect(imported)
  }
  collect(entry)
  const assetFiles = new Set()
  for (const key of needed) {
    const chunk = manifest[key]
    if (chunk?.file) assetFiles.add(chunk.file)
    for (const css of chunk?.css || []) assetFiles.add(css)
  }
  const assets = new Map([...assetFiles].map((file) => [file, readFileSync(path.join(outDir, file))]))
  return {
    manifest_sha256: sha256(manifestBytes),
    initial: accountManifestGraph(manifest, entry, assets),
  }
}

function enginePackage() {
  const pkgDir = path.join(ENGINE_ROOT, 'pkg-web')
  if (!existsSync(pkgDir)) {
    throw new Error(`prerequisite missing: ${WASM_PREREQUISITE}`)
  }
  const names = readdirSync(pkgDir).filter((name) => name.endsWith('_bg.wasm'))
  if (names.length !== 1) {
    throw new Error(`prerequisite missing: expected one *_bg.wasm in ${pkgDir}; run: ${WASM_PREREQUISITE}`)
  }
  const wasmName = names[0]
  const bytes = readFileSync(path.join(pkgDir, wasmName))
  return {
    pkgDir,
    wasmName,
    receipt: {
      file: repoPath(path.join(pkgDir, wasmName)),
      bytes: bytes.length,
      gzip_bytes: gzipSync(bytes, { mtime: 0 }).length,
      sha256: sha256(bytes),
    },
  }
}

async function buildPerformanceFixture(outDir, engine) {
  const priorFlag = process.env.VITE_CAD_EDIT
  process.env.VITE_CAD_EDIT = '1'
  try {
    const { build: viteBuild } = await import('vite')
    await viteBuild({
      configFile: path.join(WEB_ROOT, 'vite.config.js'),
      root: WEB_ROOT,
      logLevel: 'error',
      build: {
        outDir,
        emptyOutDir: true,
        rollupOptions: {
          input: { 'cad-edit-perf': PERF_HTML },
        },
      },
    })
  } finally {
    if (priorFlag === undefined) delete process.env.VITE_CAD_EDIT
    else process.env.VITE_CAD_EDIT = priorFlag
  }

  const engineOut = path.join(outDir, 'engine')
  mkdirSync(engineOut, { recursive: true })
  for (const name of ['engine.js', engine.wasmName]) {
    const source = requirePath(
      path.join(engine.pkgDir, name),
      `prerequisite missing: expected ${name} in ${engine.pkgDir}; run: ${WASM_PREREQUISITE}`,
    )
    copyFileSync(source, path.join(engineOut, name))
  }

  const pages = filesWithExtension(outDir, '.html')
  if (pages.length !== 1) {
    throw new Error(`expected exactly one performance HTML artifact, found ${pages.length}`)
  }
  return `/${path.relative(outDir, pages[0]).split(path.sep).join('/')}`
}

function gitSha() {
  const result = spawnSync('git', ['rev-parse', 'HEAD'], {
    cwd: REPO_ROOT,
    encoding: 'utf8',
    windowsHide: true,
  })
  if (result.status !== 0 || !/^[a-f0-9]{40}$/.test(result.stdout.trim())) {
    throw new Error('could not resolve the exact checkout source SHA')
  }
  return result.stdout.trim()
}

async function freePort() {
  return new Promise((resolve, reject) => {
    const server = createNetServer()
    server.once('error', reject)
    server.listen(0, '127.0.0.1', () => {
      const address = server.address()
      server.close((error) => error ? reject(error) : resolve(address.port))
    })
  })
}

async function waitForVite(url, child) {
  const deadline = Date.now() + 30_000
  while (Date.now() < deadline) {
    if (child.exitCode !== null) throw new Error(`Vite exited before readiness with code ${child.exitCode}`)
    try {
      const response = await fetch(url)
      if (response.ok) return
    } catch { /* server is still starting */ }
    await new Promise((resolve) => setTimeout(resolve, 100))
  }
  throw new Error('Vite did not become ready within 30000 ms')
}

async function stopProcess(child) {
  if (child.exitCode !== null) return
  child.kill()
  await Promise.race([
    new Promise((resolve) => child.once('exit', resolve)),
    new Promise((resolve) => setTimeout(resolve, 2_000)),
  ])
}

async function measure(chromium, pageUrl) {
  const browser = await chromium.launch({ headless: true })
  const raw = []
  try {
    for (let index = 0; index < SAMPLES; index += 1) {
      const context = await browser.newContext()
      try {
        const page = await context.newPage()
        await page.goto(pageUrl, { waitUntil: 'domcontentloaded' })
        const input = page.getByLabel('DXF file')
        const started = performance.now()
        await input.setInputFiles(FIXTURE)
        const list = page.getByTestId('cad-edit-entity-list')
        await list.waitFor({ state: 'visible', timeout: 30_000 })
        const text = (await list.textContent())?.trim() || ''
        if (!text) throw new Error(`sample ${index + 1} produced an empty entity list`)
        raw.push(Number((performance.now() - started).toFixed(3)))
      } finally {
        await context.close()
      }
    }
    return { raw, browser: await browser.version() }
  } finally {
    await browser.close()
  }
}

export async function main() {
  requirePath(FIXTURE, `named fixture missing: ${FIXTURE}`)
  requirePath(PERF_HTML, `performance fixture missing: ${PERF_HTML}`)
  const engine = enginePackage()
  const tempRoot = mkdtempSync(path.join(os.tmpdir(), 'leaf-cad-edit-f3-'))
  let vite = null
  try {
    const on = build('1', path.join(tempRoot, 'on'))
    const off = build('0', path.join(tempRoot, 'off'))
    const delta = on.initial.gzip_bytes - off.initial.gzip_bytes
    const perfOut = path.join(tempRoot, 'perf')
    const perfPage = await buildPerformanceFixture(perfOut, engine)

    const port = await freePort()
    const origin = `http://127.0.0.1:${port}`
    vite = spawn(
      process.execPath,
      [
        viteBin(),
        'preview',
        '--host', '127.0.0.1',
        '--port', String(port),
        '--strictPort',
        '--outDir', perfOut,
      ],
      {
        cwd: WEB_ROOT,
        stdio: 'ignore',
        windowsHide: true,
      },
    )
    await waitForVite(`${origin}${perfPage}`, vite)
    const { chromium } = await import('@playwright/test')
    const measured = await measure(chromium, `${origin}${perfPage}`)
    const p50 = percentile(measured.raw, 50)
    const p95 = percentile(measured.raw, 95)
    const performancePass = p95 <= P95_BUDGET_MS
    const bundlePass = delta < DELTA_BUDGET_BYTES
    const fixtureBytes = readFileSync(FIXTURE)
    const receipt = {
      schema: 'leaf.cad-edit-f3-performance.v1',
      source_sha: gitSha(),
      fixture: {
        path: repoPath(FIXTURE),
        bytes: fixtureBytes.length,
        sha256: sha256(fixtureBytes),
      },
      builds: {
        command: 'vite build --manifest',
        graph: 'ENTRY and transitive static imports with reachable JS and CSS; dynamic imports, workers, and wasm excluded',
        on: { manifest_sha256: on.manifest_sha256, ...on.initial },
        off: { manifest_sha256: off.manifest_sha256, ...off.initial },
        delta_gzip_bytes: delta,
        budget: { comparison: '<', gzip_bytes: DELTA_BUDGET_BYTES },
        pass: bundlePass,
      },
      engine_wasm: engine.receipt,
      timing: {
        harness: 'production-built fixture served by Vite preview',
        page: perfPage,
        definition: 'setInputFiles start until nonempty data-testid cad-edit-entity-list',
        fresh_chromium_contexts: SAMPLES,
        samples_ms: measured.raw,
        p50_ms: p50,
        p95_ms: p95,
        budget: { percentile: 95, comparison: '<=', milliseconds: P95_BUDGET_MS },
        pass: performancePass,
      },
      environment: {
        browser: `Chromium ${measured.browser}`,
        os: `${os.type()} ${os.release()} ${os.arch()}`,
        cpu: os.cpus()[0]?.model || 'unknown',
        logical_cpus: os.cpus().length,
      },
      pass: performancePass && bundlePass,
    }
    process.stdout.write(`${JSON.stringify(receipt)}\n`)
    return receipt.pass ? 0 : 1
  } finally {
    if (vite) await stopProcess(vite)
    rmSync(tempRoot, { recursive: true, force: true })
  }
}

if (import.meta.url === pathToFileURL(process.argv[1] || '').href) {
  try {
    process.exitCode = await main()
  } catch (error) {
    process.stderr.write(`${error?.message || error}\n`)
    process.exitCode = 1
  }
}
