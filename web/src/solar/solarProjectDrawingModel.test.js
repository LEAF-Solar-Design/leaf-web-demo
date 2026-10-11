import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { solarGraphOverlay } from './solarGraphOverlay.js'
import {
  projectSolarDrawingView, SOLAR_PROJECT_DRAWING_CODES,
} from './solarProjectDrawingModel.js'

const ids = [
  'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
  'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb',
  'cccccccc-cccc-cccc-cccc-cccccccccccc',
  'dddddddd-dddd-dddd-dddd-dddddddddddd',
]
const other = 'eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee'
const fields = ['organization_id', 'project_id', 'drawing_id', 'input_version_id']
const key = 'project:' + ids.join(':')
const lines = [{ pts: [[1, 2], [3, 4]], color: '#22c55e' }]
const codes = [
  'READY', 'GRAPHLESS', 'SCENE_PENDING', 'BINDING_INVALID', 'SCOPE_STALE',
  'CONTEXT_INVALID', 'CONTEXT_MISMATCH', 'GRAPH_PAIR_INVALID',
  'GRAPH_UNREADABLE', 'INVALID_GRAPH', 'INVALID_SCALE', 'OVERLAY_LIMIT',
]

// Projection-valid fixture; hashes are format sentinels, not integrity receipts.
function fixture() {
  const solarBinding = { kind: 'project', ...Object.fromEntries(fields.map((f, i) => [f, ids[i]])) }
  const graph = {
    graph_schema_version: 1,
    project: { units: { compute_units: 'm', meters_per_unit: 2 } },
    frames: [], panels: [{ id: 'panel', centre: [2, 4] }],
    strings: [{ id: 'string', ordered_panel_refs: ['panel'], route: [[2, 4], [6, 8]] }],
    inverters: [], routes: [],
  }
  const intake = {
    entities: [],
    solar_design_graph: graph,
    solar_design_graph_sha256: 'a'.repeat(64),
  }
  const contextView = {
    context: {
      ...Object.fromEntries(fields.map((f, i) => [f, ids[i]])),
      head_version_id: ids[3], is_head: true, intake_sha256: 'b'.repeat(64),
    },
    intake, error: null, degraded_mode: false,
  }
  return { solarBinding, contextView, current: true,
    scene: { intake, drawingKey: key, current: true } }
}

const graphOf = input => input.contextView.intake.solar_design_graph
const boom = () => { throw new Error('unreadable') }
const getter = (object, name) => Object.defineProperty(object, name, {
  configurable: true, enumerable: true, get: boom,
})
// An accessor that returns the valid value: refused for being an accessor, not for throwing.
const held = (object, name) => {
  const value = object[name]
  return Object.defineProperty(object, name, {
    configurable: true, enumerable: true, get: () => value,
  })
}
const revoked = () => {
  const value = Proxy.revocable({}, {})
  value.revoke()
  return value.proxy
}
function freeze(value) {
  if (value && typeof value === 'object' && !Object.isFrozen(value)) {
    for (const child of Object.values(value)) freeze(child)
    Object.freeze(value)
  }
  return value
}
function expectRefused(input, reason) {
  expect(projectSolarDrawingView(input)).toEqual({
    mode: 'refused', intake: null, drawingKey: null, overlay: null, reason,
  })
}
function expectBase(input, reason) {
  const result = projectSolarDrawingView(input)
  expect(result).toEqual({
    mode: 'ready', intake: input.contextView.intake, drawingKey: key, overlay: null, reason,
  })
  expect(result.intake).toBe(input.contextView.intake)
}

describe('projectSolarDrawingView', () => {
  it('R13E-01 returns the identical exact canonical intake', () => {
    const input = fixture()
    const result = projectSolarDrawingView(input)
    expect(result).toEqual({
      mode: 'ready', intake: input.contextView.intake, drawingKey: key,
      overlay: { intake: input.contextView.intake, drawingKey: key, polylines: lines },
      reason: 'READY',
    })
    expect(result.intake).toBe(input.contextView.intake)
    expect(result.overlay.intake).toBe(input.contextView.intake)
    expect(SOLAR_PROJECT_DRAWING_CODES).toEqual(codes)
    expect(Object.isFrozen(SOLAR_PROJECT_DRAWING_CODES)).toBe(true)
  })

  it('R13E-02 refuses missing or malformed canonical binding', () => {
    const good = fixture().solarBinding
    const cases = [
      undefined, null, [], Object.create(null), {}, { ...good, kind: 'standalone' },
      { ...good, extra: true }, { ...good, [Symbol('extra')]: true },
      getter({ ...good }, 'drawing_id'), revoked(),
      new Proxy(good, { ownKeys: boom }),
    ]
    for (const field of fields) {
      cases.push({ ...good, [field]: other.toUpperCase() })
      cases.push({ ...good, [field]: null })
      const missing = { ...good }
      delete missing[field]
      cases.push(missing)
    }
    for (const solarBinding of cases) {
      expectRefused({ ...fixture(), solarBinding }, 'BINDING_INVALID')
    }
    for (const input of [undefined, null, revoked(), getter(fixture(), 'solarBinding')]) {
      expectRefused(input, 'BINDING_INVALID')
    }
  })

  it('R13E-03 refuses a context from another project drawing or version', () => {
    for (const field of fields) {
      const input = fixture()
      input.contextView.context[field] = other
      expectRefused(input, 'CONTEXT_MISMATCH')
    }
    for (const current of [false, undefined, null, 1, 'true']) {
      expectRefused({ ...fixture(), current }, 'SCOPE_STALE')
    }
    expectRefused(getter(fixture(), 'current'), 'SCOPE_STALE')
    expectRefused(held(fixture(), 'current'), 'SCOPE_STALE')
    for (const contextView of [undefined, null, [], {}, revoked()]) {
      expectRefused({ ...fixture(), contextView }, 'CONTEXT_INVALID')
    }
    const changes = [
      input => getter(input, 'contextView'),
      input => getter(input.contextView, 'context'),
      input => getter(input.contextView, 'intake'),
      input => held(input.contextView, 'intake'),
      input => held(input.contextView.context, 'is_head'),
      input => { input.contextView.intake = [] },
      input => { input.contextView.intake = revoked() },
      input => { input.contextView.context = revoked() },
      input => { input.contextView.context.organization_id = 'bad' },
      input => { input.contextView.context.head_version_id = 'bad' },
      input => { input.contextView.context.is_head = false },
      input => { input.contextView.context.intake_sha256 = null },
      input => { input.contextView.context.intake_sha256 = 'b' },
      input => { input.contextView.context.intake_sha256 = 'b'.repeat(63) },
      input => { input.contextView.context.intake_sha256 = 'b'.repeat(65) },
      // Head consistency in both directions: is_head true with another head refuses too.
      input => { input.contextView.context.head_version_id = other },
      // Exact types: an upper-case digest and a non-boolean head flag refuse too.
      input => { input.contextView.context.intake_sha256 = 'B'.repeat(64) },
      input => { input.contextView.context.is_head = 1 },
      input => { input.contextView.context.is_head = '1' },
      input => {
        input.contextView.context.head_version_id = other
        input.contextView.context.is_head = null
      },
    ]
    for (const change of changes) {
      const input = fixture()
      change(input)
      expectRefused(input, 'CONTEXT_INVALID')
    }
  })

  it('R13E-04 never accepts or returns a standalone intake or graph passed beside the context', () => {
    const input = fixture()
    for (const name of ['intake', 'standaloneIntake', 'graph', 'previousIntake']) getter(input, name)
    input.scene.intake = { entities: [], solar_design_graph: graphOf(fixture()) }
    expectBase(input, 'SCENE_PENDING')
    input.scene.intake = input.contextView.intake
    const result = projectSolarDrawingView(input)
    expect(result.reason).toBe('READY')
    expect(result.intake).toBe(input.contextView.intake)
    expect(result.overlay.polylines).toEqual(lines)
  })

  it('R13E-05 presents graphless canonical intake without a starter fallback', () => {
    const input = fixture()
    input.contextView.intake = { entities: [] }
    input.scene = null
    expectBase(input, 'GRAPHLESS')
  })

  it('R13E-06 presents the exact historical intake without substituting head', () => {
    const input = fixture()
    input.contextView.context.head_version_id = other
    input.contextView.context.is_head = false
    const result = projectSolarDrawingView(input)
    expect(result.reason).toBe('READY')
    expect(result.intake).toBe(input.contextView.intake)
    expect(result.drawingKey).toBe(key)
    expect(result.overlay.polylines).toEqual(lines)
    input.contextView.intake = { entities: [] }
    expectBase(input, 'GRAPHLESS')
  })

  it('R13E-07 hides an unreadable or unpaired graph overlay while retaining exact geometry', () => {
    const cases = [
      [input => { delete input.contextView.intake.solar_design_graph }, 'GRAPH_PAIR_INVALID'],
      [input => { delete input.contextView.intake.solar_design_graph_sha256 }, 'GRAPH_PAIR_INVALID'],
      [input => { input.contextView.intake.solar_design_graph_sha256 = null }, 'GRAPH_PAIR_INVALID'],
      [input => { input.contextView.intake.solar_design_graph_sha256 = 'A'.repeat(64) }, 'GRAPH_PAIR_INVALID'],
      [input => { input.contextView.intake.solar_design_graph_sha256 = 'a' }, 'GRAPH_PAIR_INVALID'],
      [input => { input.contextView.intake.solar_design_graph_sha256 = 'a'.repeat(63) }, 'GRAPH_PAIR_INVALID'],
      [input => { input.contextView.intake.solar_design_graph_sha256 = 'a'.repeat(65) }, 'GRAPH_PAIR_INVALID'],
      [input => { input.contextView.intake.solar_design_graph_sha256 = 'sha256:' + 'a'.repeat(64) }, 'GRAPH_PAIR_INVALID'],
      [input => { input.contextView.intake.solar_design_graph = null }, 'INVALID_GRAPH'],
      [input => { input.contextView.intake.solar_design_graph = undefined }, 'INVALID_GRAPH'],
      [input => { graphOf(input).graph_schema_version = 0 }, 'INVALID_GRAPH'],
      [input => { graphOf(input).strings[0].ordered_panel_refs = ['missing'] }, 'INVALID_GRAPH'],
      [input => { graphOf(input).project.units.meters_per_unit = 0 }, 'INVALID_SCALE'],
      [input => getter(input.contextView.intake, 'solar_design_graph'), 'GRAPH_UNREADABLE'],
      [input => getter(input.contextView.intake, 'solar_design_graph_sha256'), 'GRAPH_UNREADABLE'],
      [input => getter(graphOf(input).project, 'units'), 'GRAPH_UNREADABLE'],
      [input => { input.contextView.intake.solar_design_graph = revoked() }, 'GRAPH_UNREADABLE'],
      [input => getter(graphOf(input), 'panels'), 'INVALID_GRAPH'],
    ]
    for (const [change, reason] of cases) {
      const input = fixture()
      change(input)
      expectBase(input, reason)
    }
  })

  it('R13E-08 attaches overlay only to the matching ready scene', () => {
    const input = fixture()
    const good = input.scene
    const cases = [
      undefined, null, {}, revoked(), { ...good, intake: { ...good.intake } },
      { ...good, drawingKey: 'console:' + ids[2] },
      { ...good, drawingKey: 'engine:' + ids[2] },
      { ...good, drawingKey: key + ':old' },
      { ...good, current: false }, { ...good, current: 1 },
      getter({ ...good }, 'intake'), getter({ ...good }, 'drawingKey'),
      getter({ ...good }, 'current'),
    ]
    for (const scene of cases) expectBase({ ...input, scene }, 'SCENE_PENDING')
    expectBase(getter({ ...input }, 'scene'), 'SCENE_PENDING')
    expect(projectSolarDrawingView(input).overlay.polylines).toEqual(lines)
    expect(projectSolarDrawingView(input).reason).toBe('READY')
  })

  it('R13E-09 preserves inputs and uses the existing bounded graph projector', () => {
    const input = freeze(fixture())
    const before = JSON.stringify(input)
    expect(solarGraphOverlay(graphOf(input), { metersPerUnit: 2 })).toEqual({
      polylines: lines, refusal: null,
    })
    expect(projectSolarDrawingView(input).overlay.polylines).toEqual(lines)
    expect(JSON.stringify(input)).toBe(before)
    expect(solarGraphOverlay(graphOf(input), { metersPerUnit: 1 })).toEqual({
      polylines: [], refusal: 'invalid_graph',
    })
    const malformed = fixture()
    graphOf(malformed).strings[0].ordered_panel_refs = ['missing']
    expect(solarGraphOverlay(graphOf(malformed), { metersPerUnit: 2 })).toEqual({
      polylines: [], refusal: 'invalid_graph',
    })
    const large = fixture()
    graphOf(large).panels = new Array(100001)
    expect(solarGraphOverlay(graphOf(large), { metersPerUnit: 2 })).toEqual({
      polylines: [], refusal: 'overlay_limit',
    })
    expectBase(large, 'OVERLAY_LIMIT')
  })

  it('R13E-10 the module imports only the overlay projector and touches no Viewer, App or transport', () => {
    const source = readFileSync(`${process.cwd()}/src/solar/solarProjectDrawingModel.js`, 'utf8')
    const imports = source.split('\n').filter(line => /^import\s/.test(line))
    expect(imports).toEqual(["import { solarGraphOverlay } from './solarGraphOverlay.js'"])
    expect(/\b(?:Viewer|App|fetch|XMLHttpRequest|WebSocket|React)\b/.test(source)).toBe(false)
    expect(/\b(?:import|require)\s*\(|^export\s+.*\bfrom\s/m.test(source)).toBe(false)
  })
})
