import assert from 'node:assert/strict'
import { mkdtempSync, mkdirSync, rmSync, writeFileSync } from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import test from 'node:test'
import nextConfig from '../next.config.mjs'
import inlineFeatureFlags from '../webpack/inlineFeatureFlags.cjs'
import elementSourceLoader from '../webpack/elementSourceLoader.cjs'
import { elementRef } from '../vite-plugins/elementSourceStamp.js'
import { bundleGraph, scriptPaths } from './bundle-assets.mjs'
import { bundleManifest, elementRefEnabled, environmentDefinitions, flagAwareCacheVersion, FOLDED_FEATURE_FLAGS, productionFeatureFlags, publicEnvironment, sourceIdentity, STUDIO_PATHS } from './next-runtime.mjs'

test('folded feature flags have stable keys and enable only the string 1', () => {
  assert.ok(Object.isFrozen(FOLDED_FEATURE_FLAGS))
  assert.deepEqual(FOLDED_FEATURE_FLAGS, [
    'VITE_CAD_EDIT', 'VITE_LIFECYCLE_UI', 'VITE_SOLAR_SETTINGS_FORM', 'VITE_SOLAR_FLOW_RAIL',
  ])
  for (const value of ['1', '0', '', undefined, 'true']) {
    const env = Object.fromEntries(FOLDED_FEATURE_FLAGS.map(key => [key, value]))
    env.VITE_UNKNOWN = '1'
    assert.deepEqual(productionFeatureFlags(env), Object.fromEntries(FOLDED_FEATURE_FLAGS.map(key => [key, value === '1'])))
  }
})

test('cache version carries every folded flag and preserves the prior version', () => {
  const flags = productionFeatureFlags({})
  const version = flagAwareCacheVersion('next-test', flags)
  assert.equal(version, 'next-test|leaf-flags:' + JSON.stringify(flags))
  assert.equal(flagAwareCacheVersion('next-test', { ...flags }), version)
  for (const key of FOLDED_FEATURE_FLAGS) {
    assert.notEqual(flagAwareCacheVersion('next-test', { ...flags, [key]: true }), version)
  }
  for (const prior of [undefined, '']) {
    assert.equal(flagAwareCacheVersion(prior, flags), 'leaf-flags:' + JSON.stringify(flags))
  }
})

test('Next webpack hook keys production cache and loader options on the same flags', () => {
  const saved = process.env.VITE_CAD_EDIT
  const run = (dev = false, cache = { type: 'filesystem', version: 'next-test' }) => {
    const config = { resolve: { modules: [] }, plugins: [], module: { rules: [] }, cache }
    return nextConfig('phase-production-build').webpack(config, {
      dev, webpack: { DefinePlugin: class { constructor(definitions) { this.definitions = definitions } } },
    })
  }
  const loaderOptions = config => config.module.rules[0].use[0].options
  try {
    delete process.env.VITE_CAD_EDIT
    const disabled = run()
    assert.equal(loaderOptions(disabled).featureFlags.VITE_CAD_EDIT, false)
    process.env.VITE_CAD_EDIT = '1'
    const enabled = run()
    assert.equal(loaderOptions(enabled).featureFlags.VITE_CAD_EDIT, true)
    assert.notEqual(enabled.cache.version, disabled.cache.version)
    for (const config of [disabled, enabled]) {
      assert.ok(config.cache.version.startsWith('next-test|leaf-flags:'))
    }
    assert.equal(run().cache.version, enabled.cache.version)
    const development = run(true)
    assert.equal(development.cache.version, 'next-test')
    assert.ok(!Object.hasOwn(loaderOptions(development), 'featureFlags'))
    assert.equal(run(false, false).cache, false)
    for (const cache of [undefined, null]) {
      const config = { resolve: { modules: [] }, plugins: [], module: { rules: [] }, cache }
      nextConfig('phase-production-build').webpack(config, {
        dev: false, webpack: { DefinePlugin: class { constructor(definitions) { this.definitions = definitions } } },
      })
      assert.equal(config.cache, cache)
    }
  } finally {
    if (saved === undefined) delete process.env.VITE_CAD_EDIT
    else process.env.VITE_CAD_EDIT = saved
  }
})

test('public compatibility env excludes secrets and unknown public-looking keys', () => {
  const env = { VITE_API_BASE: '/api', VITE_CAD_EDIT: '1', VITE_LEAF_PROOF_BRIDGE: '1',
    AUTH0_CLIENT_SECRET: 'secret', NEXT_PRIVATE_TOKEN: 'secret', VITE_UNREVIEWED_SECRET: 'secret' }
  const exposed = publicEnvironment(env, false)
  assert.equal(exposed.VITE_API_BASE, '/api')
  assert.equal(exposed.VITE_LEAF_PROOF_BRIDGE, '1')
  assert.equal(exposed.PROD, true)
  assert.equal(exposed.DEV, false)
  assert.equal(exposed.MODE, 'production')
  assert.equal(exposed.BASE_URL, '/')
  assert.ok(!JSON.stringify(exposed).includes('secret'))
  const definitions = environmentDefinitions(env, false)
  assert.equal(definitions['import.meta.env.VITE_CAD_EDIT'], '"1"')
  assert.equal(definitions['import.meta.env.VITE_IOS_SURFACE'], 'undefined')
  assert.equal(definitions['import.meta.env.DEV'], 'false')
  assert.equal(publicEnvironment({}, true).DEV, true)
})

test('manifest lists actual relative output scripts and only real workers', (t) => {
  const dist = mkdtempSync(path.join(os.tmpdir(), 'leaf-next-runtime-'))
  t.after(() => rmSync(dist, { recursive: true, force: true }))
  mkdirSync(path.join(dist, '_next/static/chunks'), { recursive: true })
  writeFileSync(path.join(dist, 'index.html'), '<html></html>')
  writeFileSync(path.join(dist, '_next/static/chunks/main.js'), 'boot()')
  let manifest = bundleManifest(dist, 'abc123')
  assert.deepEqual(manifest, { schema: 'leaf.web-bundle.v1', framework: 'next', source_sha: 'abc123', scripts: ['_next/static/chunks/main.js'] })
  writeFileSync(path.join(dist, '_next/static/chunks/worker.js'), 'engine_unavailable: documentLoaded')
  manifest = bundleManifest(dist, 'abc123')
  assert.deepEqual(manifest.worker_assets, ['_next/static/chunks/worker.js'])
  rmSync(path.join(dist, 'index.html'))
  assert.throws(() => bundleManifest(dist, 'abc123'), /index.html/)
})

test('source override, route exports and stamp switch are explicit', () => {
  assert.equal(sourceIdentity('/unused', { LEAF_SOURCE_SHA: 'source-sha' }), 'source-sha')
  assert.equal(STUDIO_PATHS.length, 13)
  for (const pathname of ['/', '/try', '/app', '/app/leaf-platform', '/ty', '/sheets', '/sheets/s-501']) assert.ok(STUDIO_PATHS.includes(pathname))
  assert.equal(elementRefEnabled({}), true)
  assert.equal(elementRefEnabled({ LEAF_ELEMENT_REF_STAMP: '0' }), false)
  assert.throws(() => elementRefEnabled({ LEAF_ELEMENT_REF_STAMP: 'bad' }))
})

test('production flag folding follows imported aliases without changing shadowed locals or other modules', () => {
  const root = path.resolve('/fixture/web')
  const filename = path.join(root, 'src/App.jsx')
  const source = `import { ENV_CAD_EDIT as enabled, otherFlag } from './cadedit/flag.js';
    import { ENV_CAD_EDIT } from './unrelated/flag.js';
    export const disabled = enabled && <div />;
    export const object = { enabled };
    export function nested(enabled) { return enabled && <div />; }
    export const unrelated = ENV_CAD_EDIT;`
  const result = inlineFeatureFlags(source, filename, { root, flags: { VITE_CAD_EDIT: false } })
  assert.match(result.code, /disabled = false &&/)
  assert.match(result.code, /enabled: false/)
  assert.match(result.code, /nested\(enabled\)\s*\{\s*return enabled &&/)
  assert.match(result.code, /import \{ otherFlag \} from ['"]\.\/cadedit\/flag\.js['"]/)
  assert.match(result.code, /unrelated = ENV_CAD_EDIT/)
  assert.match(result.code, /from ['"]\.\/unrelated\/flag\.js['"]/)
  assert.throws(() => inlineFeatureFlags(source, filename, { root, flags: {} }), /Missing compile-time boolean/)
})

test('production flag folding handles lifecycle and both Solar bindings and ignores lookalike module paths', () => {
  const root = path.resolve('/fixture/web')
  const filename = path.join(root, 'src/site/ToolCast.jsx')
  const source = `import { ENV_LIFECYCLE_UI as lifecycle } from '../projects/flag.js';
    import { ENV_SOLAR_SETTINGS_FORM as settings, ENV_SOLAR_FLOW_RAIL as rail } from '../solar/flag.js';
    export const values = [lifecycle, settings, rail];`
  const result = inlineFeatureFlags(source, filename, { root, flags: {
    VITE_LIFECYCLE_UI: true, VITE_SOLAR_SETTINGS_FORM: false, VITE_SOLAR_FLOW_RAIL: true,
  } })
  assert.match(result.code, /values = \[true, false, true\]/)
  assert.doesNotMatch(result.code, /import /)
  assert.equal(inlineFeatureFlags("import { ENV_CAD_EDIT } from '../other/cadedit/flag.js'; export const value = ENV_CAD_EDIT", filename, { root, flags: { VITE_CAD_EDIT: false } }), null)
})

test('production loader stamps original JSX before folding and leaves development imports intact', async () => {
  const root = path.resolve('/fixture/web')
  const resourcePath = path.join(root, 'src/App.jsx')
  const source = `import { ENV_CAD_EDIT } from './cadedit/flag.js';
    export function Panel() { return ENV_CAD_EDIT && <div data-element-id="panel" />; }`
  const run = options => new Promise((resolve, reject) => elementSourceLoader.call({
    resourcePath, getOptions: () => ({ root, ...options }),
    async: () => (error, code) => error ? reject(error) : resolve(code),
  }, source))
  const production = await run({ source: false, ref: true, featureFlags: { VITE_CAD_EDIT: false } })
  assert.match(production, /return false &&/)
  assert.ok(production.includes(`data-element-ref="${elementRef('src/App.jsx:Panel')}"`))
  assert.doesNotMatch(production, /data-element-source|import /)
  const development = await run({ source: true, ref: true })
  assert.match(development, /import \{ ENV_CAD_EDIT \}/)
  assert.match(development, /data-element-source="src\/App.jsx:Panel"/)
})


test('Next graph decodes catchall entries and follows CSS-only lazy chunks', (t) => {
  const dist = mkdtempSync(path.join(os.tmpdir(), 'leaf-next-graph-'))
  t.after(() => rmSync(dist, { recursive: true, force: true }))
  const entry = '_next/static/chunks/app/[[...slug]]/page-abc.js'
  const html = '<script src="/_next/static/chunks/app/%5B%5B...slug%5D%5D/page-abc.js"></script>'
  assert.deepEqual(scriptPaths(html), [entry])
  for (const source of ['/_next/static/%2e%2e/private.js', 'https://evil.test/main.js', '/_next/static/main.js?v=1']) {
    assert.throws(() => scriptPaths(`<script src="${source}"></script>`), /Unsafe script source/)
  }
  mkdirSync(path.dirname(path.join(dist, entry)), { recursive: true })
  mkdirSync(path.join(dist, '_next/static/css'), { recursive: true })
  writeFileSync(path.join(dist, 'index.html'), html)
  writeFileSync(path.join(dist, entry), 'r.e(207);r.miniCssF=e=>"static/css/"+({207:"abcdef"})[e]+".css"')
  const css = path.join(dist, '_next/static/css/abcdef.css')
  writeFileSync(css, 'body{}')
  assert.ok(bundleGraph(dist).reachable.has('_next/static/css/abcdef.css'))
  rmSync(css)
  assert.throws(() => bundleGraph(dist), /Missing CSS chunk/)
})


test('vendor worker loader preserves runtime scope and dynamic engine import', async () => {
  const output = await new Promise((resolve, reject) => {
    elementSourceLoader.call({
      getOptions: () => ({ runtimeImportsOnly: true }),
      async: () => (error, code) => error ? reject(error) : resolve(code),
    }, "if (typeof window === 'undefined') self.addEventListener('message', handler); import(/* @vite-ignore */ url)")
  })
  assert.ok(output.includes("typeof globalThis.window === 'undefined'"))
  assert.ok(output.includes('self.addEventListener'))
  assert.ok(output.includes('webpackIgnore: true'))
  assert.ok(!output.includes('typeof window'))
})
