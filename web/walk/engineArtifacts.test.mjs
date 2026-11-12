import test from 'node:test'
import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { mkdtemp, mkdir, readFile, readdir, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { engineArtifactsReady, ensureEngineArtifacts, readEngineArtifacts } from './engineArtifacts.mjs'

const glue = '// fake test glue'
const wasm = Buffer.from([0, 97, 115, 109, 1, 0, 0, 0])
const fileIdentity = (bytes) => ({ sha256: createHash('sha256').update(bytes).digest('hex'), bytes: Buffer.byteLength(bytes) })
const files = { 'engine.js': fileIdentity(glue), 'engine_bg.wasm': fileIdentity(wasm) }
const sources = { 'Cargo.toml': '[package]\nname = "test-engine"\n', 'src/lib.rs': '// test Rust source', 'worker-browser.mjs': '// test worker' }
const sourceHash = createHash('sha256')
for (const [name, content] of Object.entries(sources)) sourceHash.update(name).update('\0').update(`${Buffer.byteLength(content)}:`).update(content)
const engineKey = sourceHash.digest('hex')

async function fixture(t) {
  const repo = await mkdtemp(join(tmpdir(), 'leaf-walk-engine-'))
  t.after(() => rm(repo, { recursive: true, force: true }))
  const cwd = join(repo, 'vendor', 'acad' + 'rust-worker')
  const pkg = join(cwd, 'pkg-web')
  const cacheDir = join(repo, 'cache')
  await mkdir(pkg, { recursive: true })
  await mkdir(join(cwd, 'src'))
  for (const [name, content] of Object.entries(sources)) await writeFile(join(cwd, name), content)
  const artifacts = async () => {
    await mkdir(pkg, { recursive: true })
    await writeFile(join(pkg, 'engine.js'), glue)
    await writeFile(join(pkg, 'engine_bg.wasm'), wasm)
  }
  return { repo, cwd, pkg, cacheDir, artifacts }
}

test('missing engine builds with the deployment command and is rechecked', async (t) => {
  const { repo, cwd, cacheDir, artifacts } = await fixture(t)
  const calls = []
  assert.equal(await engineArtifactsReady(repo), false)
  const result = await ensureEngineArtifacts(repo, { cacheDir, run: async (command, args, options) => {
    calls.push({ command, args, options })
    if (args[0] === 'build') await artifacts()
    return { code: 0, output: '' }
  } })
  assert.deepEqual(result, { ready: true, reason: null, files, engineKey, provenance: 'built' })
  assert.equal(await engineArtifactsReady(repo), true)
  assert.equal(calls.length, 2)
  assert.equal(calls[0].command, 'wasm-pack')
  assert.deepEqual(calls[0].args, ['--version'])
  assert.equal(calls[1].command, 'wasm-pack')
  assert.deepEqual(calls[1].args, ['build', '--release', '--target', 'web', '.', '--out-dir', 'pkg-web', '--out-name', 'engine'])
  assert.equal(calls[1].options.cwd, cwd)
  assert.equal(calls[1].options.env.RUSTFLAGS, '--cfg getrandom_backend="wasm_js"')
  assert.equal(calls[1].options.timeout, 20 * 60 * 1000)
})

test('existing artifacts need neither a tool probe nor a build', async (t) => {
  const { repo, pkg, cacheDir, artifacts } = await fixture(t)
  await artifacts()
  await writeFile(join(pkg, '.leaf-engine-key'), engineKey)
  assert.deepEqual(await ensureEngineArtifacts(repo, { cacheDir, run: () => { throw new Error('must not run') } }), { ready: true, reason: null, files, engineKey, provenance: 'reused' })
})

test('artifacts without a source key rebuild instead of being reused', async (t) => {
  const { repo, pkg, cacheDir, artifacts } = await fixture(t)
  await artifacts()
  assert.equal(await engineArtifactsReady(repo), false)
  const calls = []
  const result = await ensureEngineArtifacts(repo, { cacheDir, run: async (command, args) => {
    calls.push(args[0])
    if (args[0] === 'build') {
      assert.equal((await readEngineArtifacts(pkg)).ready, false)
      await artifacts()
    }
    return { code: 0 }
  } })
  assert.deepEqual(calls, ['--version', 'build'])
  assert.equal(result.provenance, 'built')
  assert.equal(result.engineKey, engineKey)
  assert.equal(await readFile(join(pkg, '.leaf-engine-key'), 'utf8'), engineKey)
})

for (const name of Object.keys(sources)) {
  test(`changing ${name} invalidates existing artifacts and rebuilds`, async (t) => {
    const { repo, cwd, pkg, cacheDir, artifacts } = await fixture(t)
    await artifacts()
    await writeFile(join(pkg, '.leaf-engine-key'), engineKey)
    await writeFile(join(cwd, name), sources[name] + '\n// changed')
    assert.equal(await engineArtifactsReady(repo), false)
    const calls = []
    const result = await ensureEngineArtifacts(repo, { cacheDir, run: async (command, args) => {
      calls.push(args[0])
      if (args[0] === 'build') await artifacts()
      return { code: 0 }
    } })
    assert.deepEqual(calls, ['--version', 'build'])
    assert.equal(result.ready, true)
    assert.equal(result.provenance, 'built')
    assert.match(result.engineKey, /^[a-f0-9]{64}$/)
    assert.notEqual(result.engineKey, engineKey)
    assert.equal(await readFile(join(pkg, '.leaf-engine-key'), 'utf8'), result.engineKey)
  })
}

test('a build populates the cache and another checkout restores it without wasm-pack', async (t) => {
  const producer = await fixture(t)
  const consumer = await fixture(t)
  const result = await ensureEngineArtifacts(producer.repo, { cacheDir: producer.cacheDir, run: async (command, args) => {
    if (args[0] === 'build') {
      await producer.artifacts()
      await writeFile(join(producer.pkg, 'package.json'), '{"type":"module"}')
    }
    return { code: 0 }
  } })
  assert.deepEqual(result, { ready: true, reason: null, files, engineKey, provenance: 'built' })
  const entry = join(producer.cacheDir, engineKey)
  assert.deepEqual(await readEngineArtifacts(entry), { ready: true, reason: null, files })
  assert.deepEqual(await readdir(producer.cacheDir), [engineKey])
  const restored = await ensureEngineArtifacts(consumer.repo, { cacheDir: producer.cacheDir, run: () => { throw new Error('must not run') } })
  assert.deepEqual(restored, { ready: true, reason: null, files, engineKey, provenance: 'cached' })
  assert.deepEqual(await readEngineArtifacts(consumer.pkg), { ready: true, reason: null, files })
  assert.equal(await readFile(join(consumer.pkg, '.leaf-engine-key'), 'utf8'), engineKey)
  assert.equal(await readFile(join(consumer.pkg, 'package.json'), 'utf8'), '{"type":"module"}')
  assert.equal(await engineArtifactsReady(consumer.repo), true)
})

test('an unwritable cache directory still returns the built result', async (t) => {
  const { repo, pkg, cacheDir, artifacts } = await fixture(t)
  // A file in place of the directory fails even when tests run as root.
  await writeFile(cacheDir, 'not a directory')
  const result = await ensureEngineArtifacts(repo, { cacheDir, run: async (command, args) => {
    if (args[0] === 'build') await artifacts()
    return { code: 0 }
  } })
  assert.deepEqual(result, { ready: true, reason: null, files, engineKey, provenance: 'built' })
  assert.equal(await readFile(join(pkg, '.leaf-engine-key'), 'utf8'), engineKey)
})

test('a cache population race keeps the existing entry and removes temporary output', async (t) => {
  const { repo, cacheDir, artifacts } = await fixture(t)
  const entry = join(cacheDir, engineKey)
  const result = await ensureEngineArtifacts(repo, { cacheDir, run: async (command, args) => {
    if (args[0] === 'build') {
      await artifacts()
      // Another producer publishes after the lookup but before this build ends.
      await mkdir(entry, { recursive: true })
      await writeFile(join(entry, 'engine.js'), glue + '\n// other producer')
      await writeFile(join(entry, 'engine_bg.wasm'), wasm)
    }
    return { code: 0 }
  } })
  assert.deepEqual(result, { ready: true, reason: null, files, engineKey, provenance: 'built' })
  assert.equal(await readFile(join(entry, 'engine.js'), 'utf8'), glue + '\n// other producer')
  assert.deepEqual(await readdir(cacheDir), [engineKey])
})

test('cache failures are included in the reason only when the engine is unavailable', async (t) => {
  const { repo, cacheDir } = await fixture(t)
  await writeFile(cacheDir, 'not a directory')
  const result = await ensureEngineArtifacts(repo, { cacheDir, run: async () => ({ code: 1 }) })
  assert.equal(result.ready, false)
  assert.equal(result.provenance, 'unavailable')
  assert.equal(result.engineKey, engineKey)
  assert.match(result.reason, /^wasm-pack unavailable on this host; CAD engine cache read failed:/)
})

test('a single artifact is not ready and an absent wasm-pack is explicit', async (t) => {
  const { repo, pkg, cacheDir } = await fixture(t)
  await writeFile(join(pkg, 'engine.js'), '// glue alone')
  assert.equal(await engineArtifactsReady(repo), false)
  let calls = 0
  const result = await ensureEngineArtifacts(repo, { cacheDir, run: async () => {
    calls++
    throw Object.assign(new Error('missing executable'), { code: 'ENOENT' })
  } })
  assert.deepEqual(result, { ready: false, reason: 'wasm-pack unavailable on this host', files: { 'engine.js': fileIdentity('// glue alone') }, engineKey, provenance: 'unavailable' })
  assert.equal(calls, 1)
})

test('a nonzero build returns its failure instead of claiming readiness', async (t) => {
  const { repo, cacheDir } = await fixture(t)
  const result = await ensureEngineArtifacts(repo, { cacheDir, run: async (command, args) => args[0] === '--version'
    ? { code: 0 } : { code: 2, output: 'compiler error' } })
  assert.deepEqual(result, { ready: false, reason: 'CAD engine build failed (2): compiler error', files: {}, engineKey, provenance: 'unavailable' })
})

test('a successful exit without both artifacts still reports failure', async (t) => {
  const { repo, cacheDir, artifacts } = await fixture(t)
  await artifacts()
  const result = await ensureEngineArtifacts(repo, { cacheDir, run: async () => ({ code: 0 }) })
  assert.equal(result.ready, false)
  assert.match(result.reason, /did not produce engine.js and engine_bg.wasm/)
})

test('a timed out build reports the bounded deadline', async (t) => {
  const { repo, cacheDir } = await fixture(t)
  const result = await ensureEngineArtifacts(repo, { cacheDir, run: async (command, args) => args[0] === '--version'
    ? { code: 0 } : { code: null, timedOut: true } })
  assert.deepEqual(result, { ready: false, reason: 'CAD engine build timed out after 20 minutes', files: {}, engineKey, provenance: 'unavailable' })
})

test('artifact reads hash actual bytes and reject empty, invalid, missing and tampered files', async (t) => {
  const { repo, pkg, cacheDir, artifacts } = await fixture(t)
  await artifacts()
  assert.deepEqual(await readEngineArtifacts(pkg, files), { ready: true, reason: null, files })
  await writeFile(join(pkg, 'engine.js'), glue + '\n// changed')
  const changed = await readEngineArtifacts(pkg, files)
  assert.equal(changed.ready, false)
  assert.match(changed.reason, /hash mismatch/)
  assert.notEqual(changed.files['engine.js'].sha256, files['engine.js'].sha256)
  await writeFile(join(pkg, 'engine.js'), '')
  assert.equal(await engineArtifactsReady(repo), false)
  await artifacts()
  await writeFile(join(pkg, 'engine_bg.wasm'), 'not wasm')
  assert.equal(await engineArtifactsReady(repo), false)
  const unavailable = await ensureEngineArtifacts(repo, { cacheDir, run: async () => ({ code: 1 }) })
  assert.equal(unavailable.ready, false)
  await rm(join(pkg, 'engine_bg.wasm'))
  assert.match((await readEngineArtifacts(pkg)).reason, /Missing/)
})
