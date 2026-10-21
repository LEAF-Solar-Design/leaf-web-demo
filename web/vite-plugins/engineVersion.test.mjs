import test from 'node:test'
import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, unlinkSync, writeFileSync } from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { ENGINE_FILES, ENGINE_VERSION_RE, findEnginePkgDir, engineVersion, assertWorkerCarriesVersion } from './engineVersion.js'

function tempDir(t) {
  const dir = mkdtempSync(path.join(os.tmpdir(), 'leaf-engine-version-'))
  t.after(() => rmSync(dir, { recursive: true, force: true }))
  return dir
}

function writePkg(dir, glue = 'export default async function () {}', wasm = Buffer.from([0, 97, 115, 109, 1, 0, 0, 0])) {
  mkdirSync(dir, { recursive: true })
  writeFileSync(path.join(dir, 'engine.js'), glue)
  writeFileSync(path.join(dir, 'engine_bg.wasm'), wasm)
}

test('engineVersion hashes both files using the versioned formula', (t) => {
  const pkg = tempDir(t)
  const glue = Buffer.from('test glue')
  const wasm = Buffer.from([0, 1, 2, 3])
  writePkg(pkg, glue, wasm)
  assert.deepEqual(ENGINE_FILES, ['engine.js', 'engine_bg.wasm'])
  const glueHash = createHash('sha256').update(glue).digest('hex')
  const wasmHash = createHash('sha256').update(wasm).digest('hex')
  const expected = createHash('sha256').update('leaf-engine-v1\n' + glueHash + '\n' + wasmHash).digest('hex').substring(0, 16)
  assert.equal(engineVersion(pkg), expected)
  assert.match(expected, ENGINE_VERSION_RE)
  writeFileSync(path.join(pkg, 'engine_bg.wasm'), Buffer.from([0, 1, 2, 4]))
  assert.notEqual(engineVersion(pkg), expected)
  writePkg(pkg, Buffer.from('changed glue'), wasm)
  assert.notEqual(engineVersion(pkg), expected)
})

test('engineVersion returns empty for an absent package or either missing file', (t) => {
  const pkg = tempDir(t)
  assert.equal(engineVersion(undefined), '')
  assert.equal(engineVersion(pkg), '')
  writePkg(pkg)
  unlinkSync(path.join(pkg, 'engine.js'))
  assert.equal(engineVersion(pkg), '')
  writePkg(pkg)
  unlinkSync(path.join(pkg, 'engine_bg.wasm'))
  assert.equal(engineVersion(pkg), '')
})

test('findEnginePkgDir resolves overrides and selects packages in sorted order', (t) => {
  const root = tempDir(t)
  const webRoot = path.join(root, 'web')
  assert.equal(findEnginePkgDir(webRoot, {}), undefined)
  assert.equal(findEnginePkgDir(webRoot, { LEAF_CAD_ENGINE_PKG_DIR: './local-engine' }), path.resolve('./local-engine'))
  mkdirSync(path.join(root, 'vendor', 'z-engine', 'pkg-web'), { recursive: true })
  mkdirSync(path.join(root, 'vendor', 'a-engine', 'pkg-web'), { recursive: true })
  assert.equal(findEnginePkgDir(webRoot, {}), path.join(root, 'vendor', 'a-engine', 'pkg-web'))
  assert.equal(findEnginePkgDir(webRoot, { LEAF_CAD_ENGINE_PKG_DIR: '' }), path.join(root, 'vendor', 'a-engine', 'pkg-web'))
})

test('assertWorkerCarriesVersion requires a valid version in a worker chunk', (t) => {
  const dist = tempDir(t)
  const assets = path.join(dist, 'assets')
  mkdirSync(assets)
  const version = '0123456789abcdef'
  assert.throws(() => assertWorkerCarriesVersion(dist, 'bad'), /invalid engine version/)
  assert.throws(() => assertWorkerCarriesVersion(dist, version), /no built worker-browser chunk/)
  const worker = path.join(assets, 'worker-browser-abc.js')
  writeFileSync(worker, 'no version here')
  writeFileSync(path.join(assets, 'index.js'), version)
  assert.throws(() => assertWorkerCarriesVersion(dist, version), /engine version 0123456789abcdef not found in the built worker bundle/)
  writeFileSync(worker, `const version = '${version}'`)
  assert.equal(assertWorkerCarriesVersion(dist, version), true)
})

test('real Vite build carries the engine version and query into the worker', { timeout: 600000 }, async (t) => {
  const root = tempDir(t)
  const pkg = path.join(root, 'pkg-web')
  const dist = path.join(root, 'dist')
  writePkg(pkg)
  const overrides = { LEAF_CAD_ENGINE_PKG_DIR: pkg, VITE_CAD_EDIT: '1', VITE_MOCK: '0' }
  const previous = Object.fromEntries(Object.keys(overrides).map((key) => [key, process.env[key]]))
  try {
    Object.assign(process.env, overrides)
    const { build } = await import('vite')
    const web = fileURLToPath(new URL('../', import.meta.url))
    await build({ configFile: path.join(web, 'vite.config.js'), root: web, logLevel: 'error', build: { outDir: dist, emptyOutDir: true } })
    assert.equal(assertWorkerCarriesVersion(dist, engineVersion(pkg)), true)
    const assets = path.join(dist, 'assets')
    const workers = readdirSync(assets).filter((name) => /^worker-browser.*\.js$/.test(name))
    assert.ok(workers.some((name) => readFileSync(path.join(assets, name), 'utf8').includes('?v=')))
  } finally {
    for (const [key, value] of Object.entries(previous)) {
      if (value === undefined) delete process.env[key]
      else process.env[key] = value
    }
  }
})
