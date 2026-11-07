import test from 'node:test'
import assert from 'node:assert/strict'
import { mkdtemp, mkdir, readFile, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { prepareProductionBundle } from './sameOriginProxy.mjs'
import { readEngineArtifacts } from './engineArtifacts.mjs'
import { shippedViteFlags, productionBundleCacheKey, PRODUCTION_BUNDLE_INPUTS, PRODUCTION_BUILD_COLD_DIRS, productionBundleMarker } from './buildFlags.mjs'

const dockerfile = await readFile(new URL('../../deploy/Dockerfile.web', import.meta.url), 'utf8')

test('production builds require a cold Next compiler directory', () => {
  assert.deepEqual(PRODUCTION_BUILD_COLD_DIRS, ['.next'])
  assert.ok(Object.isFrozen(PRODUCTION_BUILD_COLD_DIRS))
})

test('production compiler state is removed only after reuse and prerequisite checks and before spawn', async () => {
  const source = await readFile(new URL('./sameOriginProxy.mjs', import.meta.url), 'utf8')
  const start = source.indexOf('export async function prepareProductionBundle')
  assert.ok(start >= 0)
  const end = source.indexOf('\nfunction ', start)
  assert.ok(end > start)
  const body = source.slice(start, end)
  const reuse = body.indexOf('return dist')
  const prerequisites = body.indexOf('Production bundle needs web/node_modules')
  const coldDirs = body.indexOf('for (const path of PRODUCTION_BUILD_COLD_DIRS)')
  const removal = body.indexOf('await rm(join(web, path), { recursive: true, force: true })')
  const spawn = body.indexOf('spawn(command')
  assert.ok(reuse >= 0)
  assert.ok(prerequisites > reuse)
  assert.ok(coldDirs > prerequisites)
  assert.ok(removal > coldDirs)
  assert.ok(spawn > removal)
})

test('production bundle inputs retain the previous inputs and track Next build inputs', () => {
  for (const path of ['src', 'public', 'vite-plugins', 'vite.config.js', 'index.html', 'package.json', 'package-lock.json', 'next.config.mjs', 'webpack', 'scripts']) {
    assert.ok(PRODUCTION_BUNDLE_INPUTS.includes(path), path)
  }
  assert.ok(Object.isFrozen(PRODUCTION_BUNDLE_INPUTS))
})

test('production bundle markers preserve existing fields and record build and reuse times', () => {
  const flags = shippedViteFlags(dockerfile)
  for (const engine_unavailable_reason of [null, 'wasm-pack unavailable on this host']) {
    const cacheKey = productionBundleCacheKey(flags, engine_unavailable_reason)
    const existing = Object.freeze({ sourceMtime: 123456.75, apiBase: '', mock: false, authLive: false, flags, engine_unavailable_reason, cacheKey })
    const at = '2026-11-01T10:00:00.000Z'
    const built = Object.freeze(productionBundleMarker({ ...existing, provenance: 'built', at }))
    const reused = productionBundleMarker({ ...built, provenance: 'reused', at: '2026-11-01T11:00:00.000Z' })
    const reusedAgain = productionBundleMarker({ ...reused, provenance: 'reused', at: '2026-11-01T12:00:00.000Z' })
    for (const marker of [built, reused, reusedAgain]) {
      assert.equal(JSON.stringify(Object.fromEntries(Object.keys(existing).map((key) => [key, marker[key]]))), JSON.stringify(existing))
      assert.equal(marker.builtAt, at)
      assert.equal(marker.buildProvenance, 'built')
    }
    assert.equal(built.provenance, 'built')
    assert.equal(built.builtAt, built.lastUsedAt)
    assert.equal(reused.provenance, 'reused')
    assert.equal(reused.lastUsedAt, '2026-11-01T11:00:00.000Z')
    assert.equal(reusedAgain.lastUsedAt, '2026-11-01T12:00:00.000Z')
    assert.equal(productionBundleMarker({ ...existing, provenance: 'reused', at }).builtAt, null)
    assert.equal(productionBundleMarker({ ...reused, provenance: 'built', at: reused.lastUsedAt }).builtAt, reused.lastUsedAt)
  }
})

test('production bundle hashes staged bytes on build and reuse and rebuilds missing or tampered artifacts', async (t) => {
  const repo = await mkdtemp(join(tmpdir(), 'leaf-walk-bundle-'))
  t.after(() => rm(repo, { recursive: true, force: true }))
  const webRoot = join(repo, 'web')
  const pkg = join(repo, 'vendor', 'acad' + 'rust-worker', 'pkg-web')
  const staged = join(webRoot, 'dist', 'engine')
  for (const directory of [pkg, staged, join(repo, 'deploy'), join(webRoot, 'node_modules')]) await mkdir(directory, { recursive: true })
  await writeFile(join(repo, 'deploy', 'Dockerfile.web'), 'ARG VITE_CAD_EDIT=1\n')
  await writeFile(join(pkg, 'engine.js'), '// fixture engine')
  await writeFile(join(pkg, 'engine_bg.wasm'), Buffer.from([0, 97, 115, 109, 1, 0, 0, 0]))
  const prepared = await readEngineArtifacts(pkg)
  const stageProvenance = { contract: 'leaf.cad-engine-stage.v1', crate_rev: 'abc1234', staged_at: '2026-11-01T10:00:00.000Z', files: prepared.files }
  let builds = 0
  let stages = 0
  let at = '2026-11-01T10:00:00.000Z'
  const options = { webRoot, at: () => at,
    ensureEngine: async () => ({ ...prepared, provenance: 'reused' }),
    runBuild: async () => { builds++; await writeFile(join(webRoot, 'dist', 'index.html'), '<html></html>') },
    stageEngine: async () => {
      stages++
      for (const name of Object.keys(prepared.files)) await writeFile(join(staged, name), await readFile(join(pkg, name)))
      await writeFile(join(staged, 'PROVENANCE.json'), JSON.stringify(stageProvenance))
    } }
  const marker = async () => JSON.parse(await readFile(join(webRoot, 'dist', '.leaf-walk-build.json'), 'utf8'))
  assert.equal(await prepareProductionBundle(options), join(webRoot, 'dist'))
  const built = await marker()
  assert.equal(built.engine.ready, true)
  assert.deepEqual(built.engine.files, prepared.files)
  assert.deepEqual(built.engine.provenance, stageProvenance)
  at = '2026-11-01T11:00:00.000Z'
  await prepareProductionBundle(options)
  const reused = await marker()
  assert.equal(builds, 1)
  assert.equal(stages, 1)
  assert.equal(reused.provenance, 'reused')
  assert.equal(reused.buildProvenance, 'built')
  assert.equal(reused.builtAt, built.builtAt)
  assert.equal(reused.lastUsedAt, at)
  assert.deepEqual(reused.engine, built.engine)
  await writeFile(join(staged, 'engine.js'), '// tampered')
  await prepareProductionBundle(options)
  assert.equal(builds, 2)
  assert.deepEqual((await marker()).engine.files, prepared.files)
  await rm(join(staged, 'engine_bg.wasm'))
  await prepareProductionBundle(options)
  assert.equal(builds, 3)
  const cached = await marker()
  delete cached.engine
  await writeFile(join(webRoot, 'dist', '.leaf-walk-build.json'), JSON.stringify(cached))
  await prepareProductionBundle(options)
  assert.equal(builds, 4)
  const previousHash = prepared.files['engine.js'].sha256
  await writeFile(join(pkg, 'engine.js'), '// next fixture engine')
  prepared.files = (await readEngineArtifacts(pkg)).files
  stageProvenance.files = prepared.files
  at = '2026-11-01T12:00:00.000Z'
  await prepareProductionBundle(options)
  const changed = await marker()
  assert.equal(builds, 5)
  assert.notEqual(changed.engine.files['engine.js'].sha256, previousHash)
  assert.deepEqual(changed.engine.files, prepared.files)
  assert.equal(changed.builtAt, at)
  await writeFile(join(staged, 'engine.js'), '// tampered again')
  await assert.rejects(prepareProductionBundle({ ...options, stageEngine: async () => {} }), /hash mismatch/)
  await assert.rejects(prepareProductionBundle({ ...options, stageEngine: async () => {
    await options.stageEngine()
    await writeFile(join(staged, 'PROVENANCE.json'), JSON.stringify({ files: {} }))
  } }), /provenance hash mismatch/)
  const unavailableReason = 'wasm-pack unavailable on this host'
  await prepareProductionBundle({ ...options,
    ensureEngine: async () => ({ ready: false, reason: unavailableReason, files: {} }),
    stageEngine: async () => { throw new Error('disabled engine must not stage') } })
  const unavailable = await marker()
  assert.equal(unavailable.flags.VITE_CAD_EDIT, '0')
  assert.equal(unavailable.engine_unavailable_reason, unavailableReason)
  assert.deepEqual(unavailable.engine, { ready: false, reason: unavailableReason, files: {} })
  const unavailableBuilds = builds
  await prepareProductionBundle({ ...options,
    ensureEngine: async () => ({ ready: false, reason: unavailableReason, files: {} }) })
  assert.equal(builds, unavailableBuilds)
  assert.equal((await marker()).engine.ready, false)
})

test('the deployed defaults enable all five shipped feature surfaces and exclude walk overrides', () => {
  const flags = shippedViteFlags(dockerfile)
  for (const name of ['VITE_LIFECYCLE_UI', 'VITE_IOS_SURFACE', 'VITE_CAD_EDIT', 'VITE_SOLAR_FLOW_RAIL', 'VITE_SOLAR_SETTINGS_FORM']) assert.equal(flags[name], '1')
  for (const name of Object.keys(flags)) {
    assert.ok(!['VITE_API_BASE', 'VITE_MOCK', 'VITE_TENANT_ID'].includes(name))
    assert.ok(!name.startsWith('VITE_AUTH0_'))
  }
  assert.deepEqual(shippedViteFlags('ARG VITE_TENANT_ID=live\nARG VITE_AUTH0_NEW=secret'), {})
})

test('new Dockerfile flags are discovered without a code change', () => {
  assert.equal(shippedViteFlags(dockerfile + '\nARG VITE_NEW_SURFACE=1\n').VITE_NEW_SURFACE, '1')
  assert.deepEqual(shippedViteFlags('# ARG VITE_COMMENT=1\nARG VITE_QUOTED="on" # enabled\nARG VITE_EMPTY='), { VITE_QUOTED: 'on', VITE_EMPTY: '' })
})

test('bundle cache identity changes with effective flags and engine availability', () => {
  assert.equal(productionBundleCacheKey({ VITE_Z: '0', VITE_A: '1' }), '{"flags":{"VITE_A":"1","VITE_Z":"0"},"engine_unavailable_reason":null}')
  const flags = shippedViteFlags(dockerfile)
  const ready = productionBundleCacheKey(flags)
  assert.notEqual(ready, productionBundleCacheKey({ ...flags, VITE_CAD_EDIT: '0' }))
  assert.notEqual(ready, productionBundleCacheKey(flags, 'wasm-pack unavailable on this host'))
  assert.notEqual(productionBundleCacheKey(flags, 'first failure'), productionBundleCacheKey(flags, 'second failure'))
  assert.equal(ready, productionBundleCacheKey(Object.fromEntries(Object.entries(flags).reverse())))
})
