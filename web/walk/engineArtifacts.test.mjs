import test from 'node:test'
import assert from 'node:assert/strict'
import { mkdtemp, mkdir, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { engineArtifactsReady, ensureEngineArtifacts } from './engineArtifacts.mjs'

async function fixture(t) {
  const repo = await mkdtemp(join(tmpdir(), 'leaf-walk-engine-'))
  t.after(() => rm(repo, { recursive: true, force: true }))
  const cwd = join(repo, 'vendor', 'acad' + 'rust-worker')
  const pkg = join(cwd, 'pkg-web')
  await mkdir(pkg, { recursive: true })
  const artifacts = async () => {
    await writeFile(join(pkg, 'engine.js'), '// fake test glue')
    await writeFile(join(pkg, 'engine_bg.wasm'), Buffer.from([0, 97, 115, 109]))
  }
  return { repo, cwd, pkg, artifacts }
}

test('missing engine builds with the deployment command and is rechecked', async (t) => {
  const { repo, cwd, artifacts } = await fixture(t)
  const calls = []
  assert.equal(await engineArtifactsReady(repo), false)
  const result = await ensureEngineArtifacts(repo, { run: async (command, args, options) => {
    calls.push({ command, args, options })
    if (args[0] === 'build') await artifacts()
    return { code: 0, output: '' }
  } })
  assert.deepEqual(result, { ready: true, reason: null })
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
  const { repo, artifacts } = await fixture(t)
  await artifacts()
  assert.deepEqual(await ensureEngineArtifacts(repo, { run: () => { throw new Error('must not run') } }), { ready: true, reason: null })
})

test('a single artifact is not ready and an absent wasm-pack is explicit', async (t) => {
  const { repo, pkg } = await fixture(t)
  await writeFile(join(pkg, 'engine.js'), '// glue alone')
  assert.equal(await engineArtifactsReady(repo), false)
  let calls = 0
  const result = await ensureEngineArtifacts(repo, { run: async () => {
    calls++
    throw Object.assign(new Error('missing executable'), { code: 'ENOENT' })
  } })
  assert.deepEqual(result, { ready: false, reason: 'wasm-pack unavailable on this host' })
  assert.equal(calls, 1)
})

test('a nonzero build returns its failure instead of claiming readiness', async (t) => {
  const { repo } = await fixture(t)
  const result = await ensureEngineArtifacts(repo, { run: async (command, args) => args[0] === '--version'
    ? { code: 0 } : { code: 2, output: 'compiler error' } })
  assert.deepEqual(result, { ready: false, reason: 'CAD engine build failed (2): compiler error' })
})

test('a successful exit without both artifacts still reports failure', async (t) => {
  const { repo } = await fixture(t)
  const result = await ensureEngineArtifacts(repo, { run: async () => ({ code: 0 }) })
  assert.equal(result.ready, false)
  assert.match(result.reason, /did not produce engine.js and engine_bg.wasm/)
})

test('a timed out build reports the bounded deadline', async (t) => {
  const { repo } = await fixture(t)
  const result = await ensureEngineArtifacts(repo, { run: async (command, args) => args[0] === '--version'
    ? { code: 0 } : { code: null, timedOut: true } })
  assert.deepEqual(result, { ready: false, reason: 'CAD engine build timed out after 20 minutes' })
})
