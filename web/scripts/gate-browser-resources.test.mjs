import assert from 'node:assert/strict'
import { mkdtempSync, readFileSync, readdirSync, renameSync, rmSync, statSync, utimesSync, writeFileSync } from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import test from 'node:test'
import { GATE_BROWSER_SUITES, gateBrowserResources, stageEngineAsset } from './gate-browser-resources.mjs'

const kindFor = (suite) => suite === 'web-ios-ship-surface-proof' ? 'ios'
  : suite === 'web-lifecycle-panel-proof' ? 'lifecycle' : 'base'

test('all admitted suites and workers have disjoint ports, Next instances and report roots', () => {
  assert.deepEqual(GATE_BROWSER_SUITES, [
    'web-studio-panels-proof', 'web-author-quota-gate', 'web-version-restore-proof',
    'web-async-author-stage', 'web-lifecycle-panel-proof', 'web-ios-ship-surface-proof',
  ])
  const ports = new Set()
  const instances = new Set()
  const reports = new Set()
  const reserved = new Set([5275, 8230, 8240, 8250])
  for (let worker = 0; worker < 8; worker++) {
    for (let offset = 0; offset < 100; offset++) reserved.add(5185 + 100 * worker + offset)
  }
  for (let worker = 0; worker < 8; worker++) {
    for (const suite of GATE_BROWSER_SUITES) {
      const env = { LEAF_GATE_BROWSER_SUITE: suite, LEAF_NATIVE_GATE_WORKER: String(worker) }
      const resources = gateBrowserResources(env, kindFor(suite))
      assert.deepEqual(resources, gateBrowserResources(env, kindFor(suite)))
      assert.equal(resources.isolated, true)
      assert.equal(resources.worker, worker)
      assert.equal(resources.ports.length, kindFor(suite) === 'ios' ? 2 : 1)
      assert.equal(resources.instances.length, resources.ports.length)
      for (const port of resources.ports) {
        assert.ok(Number.isInteger(port) && port > 0 && port < 65535)
        assert.ok(port >= 15000 + worker * 100 && port < 15100 + worker * 100)
        assert.ok(!reserved.has(port), `reserved port ${port}`)
        assert.ok(!ports.has(port), `duplicate port ${port}`)
        ports.add(port)
      }
      for (const instance of resources.instances) {
        assert.match(instance, /^[a-z0-9-]{1,48}$/)
        assert.ok(instance.includes(suite) && instance.includes(`w${worker}`))
        assert.ok(!instances.has(instance), `duplicate instance ${instance}`)
        instances.add(instance)
      }
      assert.ok(!reports.has(resources.reportRoot))
      reports.add(resources.reportRoot)
    }
  }
  assert.equal(ports.size, 56)
  assert.equal(instances.size, 56)
  assert.equal(reports.size, 48)
})

test('missing suite preserves every legacy worker port, report root and instance default', () => {
  const roots = {
    base: '../artifacts/cat-operator-proof',
    lifecycle: '../artifacts/project-lifecycle-proof',
    ios: '../artifacts/ios-ship-surface',
  }
  for (let worker = 0; worker < 8; worker++) {
    for (const kind of Object.keys(roots)) {
      const resources = gateBrowserResources({ LEAF_NATIVE_GATE_WORKER: String(worker) }, kind)
      const port = 5185 + 100 * worker
      assert.deepEqual(resources, {
        isolated: false,
        worker,
        ports: kind === 'ios' ? [port, port + 50] : [port],
        instances: kind === 'ios' ? [`ios-${port}`, `ios-${port + 50}`] : [null],
        reportRoot: roots[kind],
      })
    }
  }
  assert.deepEqual(gateBrowserResources({}), gateBrowserResources({ LEAF_NATIVE_GATE_WORKER: '0' }))
  assert.deepEqual(gateBrowserResources({ LEAF_NEXT_DEV_INSTANCE: 'manual' }), gateBrowserResources({}))
})

test('malformed suite and worker identities fail clearly, including empty opt-in', () => {
  for (const suite of ['', 'unknown', ' web-studio-panels-proof', 'web-studio-panels-proof ',
    'WEB-STUDIO-PANELS-PROOF', '../web-studio-panels-proof', null, 1]) {
    assert.throws(() => gateBrowserResources({ LEAF_GATE_BROWSER_SUITE: suite }), /Invalid LEAF_GATE_BROWSER_SUITE/)
  }
  for (const worker of ['', '-1', '8', '01', '1.0', ' 1', '1 ', 'NaN', '1\n']) {
    assert.throws(() => gateBrowserResources({ LEAF_NATIVE_GATE_WORKER: worker }), /Invalid native gate worker/)
    assert.throws(() => gateBrowserResources({ LEAF_NATIVE_GATE_WORKER: worker,
      LEAF_GATE_BROWSER_SUITE: GATE_BROWSER_SUITES[0] }), /Invalid native gate worker/)
  }
  assert.throws(() => gateBrowserResources({}, 'unknown'), /Invalid browser config kind/)
  assert.throws(() => gateBrowserResources({ LEAF_GATE_BROWSER_SUITE: GATE_BROWSER_SUITES[5] }), /requires ios config/)
  assert.throws(() => gateBrowserResources({ LEAF_GATE_BROWSER_SUITE: GATE_BROWSER_SUITES[4] }), /requires lifecycle config/)
})

function assets(t) {
  const directory = mkdtempSync(path.join(os.tmpdir(), 'gate-browser-assets-'))
  t.after(() => rmSync(directory, { recursive: true, force: true }))
  return { directory, source: path.join(directory, 'source'), target: path.join(directory, 'target') }
}

test('staging publishes complete bytes by adjacent rename and skips equal targets', (t) => {
  const { directory, source, target } = assets(t)
  const bytes = Buffer.from([0, 255, 1, 128, 10])
  writeFileSync(source, bytes)
  writeFileSync(target, 'previous')
  let published = false
  stageEngineAsset(source, target, { rename(temporary, destination) {
    assert.equal(path.dirname(temporary), directory)
    assert.notEqual(temporary, target)
    assert.equal(destination, target)
    assert.deepEqual(readFileSync(temporary), bytes)
    assert.equal(readFileSync(target, 'utf8'), 'previous')
    renameSync(temporary, destination)
    published = true
  } })
  assert.equal(published, true)
  assert.deepEqual(readFileSync(target), bytes)
  utimesSync(target, 1000000000, 1000000000)
  const previous = statSync(target)
  stageEngineAsset(source, target, {
    writeFile() { assert.fail('equal bytes must not be written') },
    rename() { assert.fail('equal bytes must not be renamed') },
  })
  assert.equal(statSync(target).mtimeMs, previous.mtimeMs)
  assert.deepEqual(readdirSync(directory).sort(), ['source', 'target'])
})

test('staging creates missing directories and removes missing-source targets idempotently', (t) => {
  const { directory, source } = assets(t)
  const target = path.join(directory, 'engine', 'asset.wasm')
  writeFileSync(source, 'engine bytes')
  stageEngineAsset(source, target)
  assert.equal(readFileSync(target, 'utf8'), 'engine bytes')
  rmSync(source)
  stageEngineAsset(source, target)
  stageEngineAsset(source, target)
  assert.deepEqual(readdirSync(path.dirname(target)), [])
  writeFileSync(target, 'stale')
  stageEngineAsset(null, target)
  stageEngineAsset(null, target)
  assert.deepEqual(readdirSync(path.dirname(target)), [])
})

test('failed writes and renames clean unique temporary files and preserve the target', (t) => {
  const { directory, source, target } = assets(t)
  writeFileSync(source, 'new bytes')
  writeFileSync(target, 'previous')
  const temporaries = new Set()
  for (const operation of ['write', 'rename', 'rename']) {
    const failure = new Error(`injected ${operation} failure`)
    assert.throws(() => stageEngineAsset(source, target, {
      writeFile(temporary, bytes, options) {
        assert.ok(!temporaries.has(temporary))
        temporaries.add(temporary)
        assert.equal(options.flag, 'wx')
        writeFileSync(temporary, operation === 'write' ? bytes.subarray(0, 2) : bytes, options)
        assert.equal(readFileSync(target, 'utf8'), 'previous')
        if (operation === 'write') throw failure
      },
      rename() { throw failure },
    }), (error) => error === failure)
    assert.equal(readFileSync(target, 'utf8'), 'previous')
    assert.deepEqual(readdirSync(directory).sort(), ['source', 'target'])
  }
})

test('source read errors propagate without removing the current target', (t) => {
  const { directory, target } = assets(t)
  writeFileSync(target, 'previous')
  assert.throws(() => stageEngineAsset(path.join(directory, 'bad\u0000source'), target))
  assert.equal(readFileSync(target, 'utf8'), 'previous')
  assert.deepEqual(readdirSync(directory), ['target'])
})
