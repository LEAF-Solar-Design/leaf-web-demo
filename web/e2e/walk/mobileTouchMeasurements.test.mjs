import test from 'node:test'
import assert from 'node:assert/strict'
import { runInNewContext } from 'node:vm'
import { evaluateLenses } from './lenses/index.mjs'
import { addPersonaFragment } from './fixtures.mjs'
import { collectMobileTouchMeasurements, activateWithMobileTouchMeasurements } from './mobileTouchMeasurements.mjs'

const reference = 'mobile-touch:open-drawing[ready]@phone'
const probe = { featureId: 'open-drawing', state: 'ready',
  locator: { role: 'button', name: 'Open drawing', trigger: 'click' },
  assertion: { kind: 'opens', target: 'drawing' } }
const evidenceFor = (measurements) => ({ schema: 'leaf.walk-evidence.v1', featureId: probe.featureId,
  state: probe.state, viewport: 'phone', measurements })
const mobileResult = (measurements) => evaluateLenses(evidenceFor(measurements)).find((entry) => entry.lens === 'mobile-touch')

// Execute the real browser callback against a small DOM fake, without changing globals.
function phone({ box = { x: 10, y: 20, width: 44, height: 44 }, connected = true,
  coarsePointer = true, touchPoints = 1, hit, evaluateError = false } = {}) {
  const descendant = {}
  const handle = { isConnected: connected, contains: (node) => node === descendant,
    boundingBox: async () => box }
  const calls = []
  const page = { evaluate: async (callback, argument) => {
    calls.push('evaluate')
    if (evaluateError) throw new Error('page unavailable')
    return structuredClone(runInNewContext(`(${callback.toString()})(argument)`, {
      argument,
      window: { innerWidth: 390, innerHeight: 844, devicePixelRatio: 3,
        matchMedia: (query) => {
          assert.equal(query, '(pointer: coarse)')
          return { matches: coarsePointer }
        } },
      navigator: { maxTouchPoints: touchPoints },
      document: { elementFromPoint: (x, y) => {
        calls.push([x, y])
        return hit ? hit(x, y, handle, descendant) : handle
      } },
    }))
  } }
  const locator = { count: async () => 1, elementHandle: async () => { calls.push('handle'); return handle } }
  return { page, handle, locator, calls }
}

const collect = (fake, options = {}) => collectMobileTouchMeasurements({ ...fake, viewport: 'phone',
  reference, locatorDescription: probe.locator, ...options })

test('real 44px phone geometry passes only mobile-touch and records the actual page values', async () => {
  const fake = phone()
  const measured = await collect(fake)
  assert.equal(measured.reference, reference)
  assert.equal(measured.input.touch, true)
  assert.equal(measured.input.coarsePointer, true)
  assert.equal(measured.viewport.width, 390)
  assert.equal(measured.viewport.height, 844)
  assert.equal(measured.viewport.deviceScaleFactor, 3)
  assert.deepEqual(measured.target, { role: 'button', name: 'Open drawing',
    box: { x: 10, y: 20, width: 44, height: 44 } })
  assert.deepEqual(measured.control.rect, measured.target.box)
  assert.equal(measured.control.reachable, true)
  assert.equal(measured.control.clipped, false)
  assert.deepEqual(fake.calls.slice(1), [[32, 42], [12, 22], [52, 22], [12, 62], [52, 62]])
  for (const result of evaluateLenses(evidenceFor(measured))) {
    assert.equal(result.status, result.lens === 'mobile-touch' ? 'pass' : 'unknown', result.lens)
  }
})

test('small, overflowing, centre-covered and corner-covered targets produce findings', async () => {
  for (const [options, reachable, clipped] of [
    [{ box: { x: 10, y: 20, width: 43, height: 44 } }, true, false],
    [{ box: { x: 360, y: 20, width: 44, height: 44 } }, true, true],
    [{ hit: (x, y, target) => x === 32 && y === 42 ? {} : target }, false, false],
    [{ hit: (x, y, target) => x === 12 && y === 22 ? {} : target }, true, true],
    [{ hit: () => null }, false, true],
  ]) {
    const measured = await collect(phone(options))
    assert.equal(measured.control.reachable, reachable)
    assert.equal(measured.control.clipped, clipped)
    assert.equal(mobileResult(measured).status, 'finding')
  }
})

test('descendants count as hits and out-of-viewport centres are unreachable', async () => {
  assert.equal(mobileResult(await collect(phone({ hit: (x, y, target, child) => child }))).status, 'pass')
  const fake = phone({ box: { x: 400, y: 20, width: 44, height: 44 } })
  const measured = await collect(fake)
  assert.equal(measured.control.reachable, false)
  assert.equal(measured.control.clipped, true)
  assert.deepEqual(fake.calls, ['evaluate'])
})

test('unsupported modalities stay unknown; unavailable reads return null without throwing', async () => {
  for (const options of [{ coarsePointer: false }, { touchPoints: 0 }]) {
    assert.equal(mobileResult(await collect(phone(options))).status, 'unknown')
  }
  const desktop = phone()
  assert.equal(await collect(desktop, { viewport: 'desktop' }), null)
  assert.deepEqual(desktop.calls, [])
  for (const options of [{ connected: false }, { box: null }, { evaluateError: true },
    { box: { x: NaN, y: 0, width: 44, height: 44 } }]) {
    assert.equal(await collect(phone(options)), null)
  }
  assert.equal(await collect(phone(), { handle: null }), null)
  assert.equal(await collect(phone(), { reference: 'mobile-touch:secret=unsafe[ready]@phone' }), null)
  const fake = phone()
  fake.handle.boundingBox = async () => { throw new Error('detached') }
  assert.equal(await collect(fake), null)
  assert.equal(await collectMobileTouchMeasurements(), null)
})

test('locator descriptions are optional and non-string names are not serialized', async () => {
  const measured = await collect(phone(), { locatorDescription: { name: /^Open/ } })
  assert.equal(measured.target.role, '')
  assert.equal(measured.target.name, '')
})

test('the call-site helper captures before activation, measures after, and preserves existing evidence', async () => {
  const fake = phone()
  const result = { result: 'passed', receipt: 'untouched' }
  const evidence = { ...evidenceFor(), result }
  const bundleReference = 'walk:open-drawing[ready]@phone'
  addPersonaFragment(evidence, { persona: 'accessibility', bundleReference,
    measurements: { reference: 'accessibility:open-drawing[ready]@phone', unrelated: { value: 7 },
      input: { otherInput: true }, control: { otherControl: true, rect: { otherRect: true } } } })
  await activateWithMobileTouchMeasurements({ ...fake, probe, viewport: 'phone', evidence,
    addFragment: (persona, measurements) => addPersonaFragment(evidence, { persona, measurements, bundleReference }),
    activate: async () => { fake.calls.push('activate') } })
  assert.deepEqual(fake.calls.filter(call => typeof call === 'string'), ['handle', 'evaluate', 'activate', 'evaluate'])
  assert.equal(evidence.result, result)
  assert.deepEqual(evidence.measurements.unrelated, { value: 7 })
  assert.equal(evidence.measurements.input.otherInput, true)
  assert.equal(evidence.measurements.control.otherControl, true)
  assert.equal(evidence.measurements.control.rect.otherRect, true)
  assert.equal(mobileResult(evidence.measurements).status, 'pass')
  assert.equal(evidence.measurements.reference, bundleReference)
  assert.deepEqual(evidence.measurementSources, [
    { persona: 'accessibility', reference: 'accessibility:open-drawing[ready]@phone', missing: false },
    { persona: 'mobile-touch', reference, missing: false },
  ])
})

test('controls that detach or lose their box on activation emit their pre-activation sample once', async () => {
  for (const disappear of [fake => { fake.handle.isConnected = false },
    fake => { fake.handle.boundingBox = async () => null }]) {
    const fake = phone()
    const expected = await collect(fake)
    const fragments = []
    await activateWithMobileTouchMeasurements({ ...fake, probe, viewport: 'phone',
      addFragment: (...args) => fragments.push(args), activate: async () => disappear(fake) })
    assert.deepEqual(fragments, [['mobile-touch', expected]])
  }
})

test('persistent controls emit their post-activation sample once', async () => {
  const fake = phone()
  const fragments = []
  const afterBox = { x: 30, y: 40, width: 48, height: 48 }
  await activateWithMobileTouchMeasurements({ ...fake, probe, viewport: 'phone',
    addFragment: (...args) => fragments.push(args),
    activate: async () => { fake.handle.boundingBox = async () => afterBox } })
  assert.deepEqual(fragments, [['mobile-touch', await collect(fake)]])
  assert.deepEqual(fragments[0][1].target.box, afterBox)
})

test('activation descriptions label both self-hiding and persistent controls, with the declared locator as fallback', async () => {
  const phoneDescription = { role: 'button', name: 'Phone launcher' }
  const phoneProbe = { ...probe, locator: { ...probe.locator, phone: phoneDescription } }
  for (const selfHiding of [true, false]) {
    for (const activeProbe of [probe, phoneProbe]) {
      for (const locatorDescription of [undefined, { role: 'button', name: 'Close Studio' },
        { role: 'combobox', name: 'Workspace profile' }]) {
        const fake = phone()
        const fragments = []
        const afterBox = { x: 30, y: 40, width: 48, height: 48 }
        await activateWithMobileTouchMeasurements({ ...fake, probe: activeProbe, viewport: 'phone', locatorDescription,
          addFragment: (...args) => fragments.push(args),
          activate: async () => {
            if (selfHiding) fake.handle.isConnected = false
            else fake.handle.boundingBox = async () => afterBox
          } })
        assert.equal(fragments.length, 1)
        assert.equal(fragments[0][0], 'mobile-touch')
        assert.deepEqual(fragments[0][1].target, {
          ...(locatorDescription || activeProbe.locator.phone || { role: probe.locator.role, name: probe.locator.name }),
          box: selfHiding ? { x: 10, y: 20, width: 44, height: 44 } : afterBox,
        })
      }
    }
  }
})

test('a throwing pre-activation sample leaves the post-activation sample available', async () => {
  const fake = phone()
  const expected = await collect(fake)
  const boundingBox = fake.handle.boundingBox
  fake.handle.boundingBox = async () => { throw new Error('pre-sample unavailable') }
  const fragments = []
  await activateWithMobileTouchMeasurements({ ...fake, probe, viewport: 'phone',
    addFragment: (...args) => fragments.push(args),
    activate: async () => { fake.handle.boundingBox = boundingBox } })
  assert.deepEqual(fragments, [['mobile-touch', expected]])
})

test('a throwing activation propagates its error without emitting the pre-activation sample', async () => {
  const fake = phone()
  const error = new Error('activation failed')
  const fragments = []
  await assert.rejects(activateWithMobileTouchMeasurements({ ...fake, probe, viewport: 'phone',
    addFragment: (...args) => fragments.push(args), activate: async () => { throw error } }),
  caught => caught === error)
  assert.deepEqual(fragments, [])
  assert.deepEqual(fake.calls.filter(call => typeof call === 'string'), ['handle', 'evaluate'])
})

test('non-click, disabled, navigation, ambiguous and desktop activations collect nothing', async () => {
  for (const changes of [
    { viewport: 'desktop' }, { count: 0 }, { count: 2 },
    ...['keyboard', 'navigate', 'select', 'type', 'fill-enter'].map((trigger) => ({ locator: { trigger } })),
    { locator: { keyboardAction: 'engine:repeat' } },
    { assertion: { kind: 'disabled_with_reason' } }, { assertion: { kind: 'navigates' } },
    ...['script-file-picker', 'signed-out-session', 'viewer-overview-pan'].map((target) => ({ assertion: { target } })),
  ]) {
    const fake = phone()
    if (changes.count !== undefined) fake.locator.count = async () => changes.count
    const evidence = { result: { result: 'passed' } }
    let activations = 0
    await activateWithMobileTouchMeasurements({ ...fake, viewport: changes.viewport || 'phone', evidence,
      addFragment: () => assert.fail('unsupported activation must not add a fragment'),
      probe: { ...probe, locator: { ...probe.locator, ...changes.locator },
        assertion: { ...probe.assertion, ...changes.assertion } }, activate: async () => { activations++ } })
    assert.equal(activations, 1)
    assert.deepEqual(fake.calls, [])
    assert.deepEqual(evidence, { result: { result: 'passed' } })
  }
})

test('measurement and handle failures preserve the verdict; activation failures still propagate', async () => {
  for (const failCapture of [false, true]) {
    const fake = phone({ evaluateError: true })
    if (failCapture) fake.locator.elementHandle = async () => { throw new Error('handle unavailable') }
    const evidence = { result: { result: 'passed' }, measurements: { existing: true } }
    const before = structuredClone(evidence)
    await activateWithMobileTouchMeasurements({ ...fake, probe, viewport: 'phone', evidence,
      addFragment: () => assert.fail('failed measurement must not add a fragment'), activate: async () => {} })
    assert.deepEqual(evidence, before)
  }
  const fake = phone()
  const error = new Error('functional failure')
  await assert.rejects(activateWithMobileTouchMeasurements({ ...fake, probe, viewport: 'phone', evidence: {},
    addFragment: () => assert.fail('failed activation must not add a fragment'),
    activate: async () => { throw error } }), (caught) => caught === error)
  assert.deepEqual(fake.calls.filter(call => typeof call === 'string'), ['handle', 'evaluate'])
})
