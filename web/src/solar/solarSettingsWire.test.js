import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it, vi } from 'vitest'
import { prepareCatalogRunParams } from '../runIntent.js'
import {
  canInitializeSolarSettings, canOpenSolarSettingsForm, catalogRunOverlays,
  solarSettingsFormChoice, solarSettingsLoaders, solarSettingsRunFeedback, solarSettingsScope,
} from './solarSettingsWire.js'

const A = { entitled: true, implemented: true, engine_ready: true, input_ready: false, refusal_reasons: ['graph_seed_required'] }
const C = { drawingId: 'd1', drawingVersion: 3, projectId: null }
const I = { schema_version: 1, source_intake_sha256: 'a'.repeat(64), units: { drawing_units: 'ft', wcs_to_ucs: [1,0,0,0,0,1,0,0,0,0,1,0,0,0,0,1], elevation_datum: 'unknown', crs: null } }
const P = { expected_rev: 0, changes: { panels_in_sequence: 3 }, initialize: I }
const M = 'This saved drawing version cannot be used to start a Solar design.'
const refused = { ok: false, error: { error_code: 'bad_params', message: 'invalid_seed_parent', retryable: false }, reason_code: 'invalid_seed_parent' }
const association = (envelope) => ({ intentId: 'i1', drawingId: 'd1', drawingVersion: 3, envelope })

describe('Solar settings wiring rules', () => {
  it('CF14 enabled conductors omit CAD handles from the whole-graph request', () => {
    const input = { toolName: 'solar-string-conductors', selectedHandle: 'AB', isWrite: true }
    expect(catalogRunOverlays({ ...input, enabled: true })).toEqual({})
    expect(catalogRunOverlays({ ...input, enabled: false })).toEqual({ target_handle: 'AB', handle: 'AB' })
    expect(prepareCatalogRunParams(
      { name: input.toolName, capabilities: ['drawing.write'], params: { properties: {} } },
      { operation: 'set-conductors', expected_rev: 7, assignments: [{ string_ref: 'T1', wire_gauge: '10 AWG' }] }, C,
      catalogRunOverlays({ ...input, enabled: true }),
    )).toEqual({ operation: 'set-conductors', expected_rev: 7,
      assignments: [{ string_ref: 'T1', wire_gauge: '10 AWG' }], drawing_id: 'd1' })
  })

  it('CF15 chooses conductors only for a live standalone drawing', () => {
    const input = { enabled: true, mock: false, toolName: 'solar-string-conductors', context: C }
    expect(solarSettingsFormChoice(input)).toBe('conductors')
    for (const override of [{ enabled: false }, { mock: true }, { context: { ...C, projectId: 'p1' } },
      { context: { ...C, drawingId: '' } }, { context: null }, { context: Object.assign(new Date(), C) }]) {
      expect(solarSettingsFormChoice({ ...input, ...override })).toBe('generic')
    }
    expect(solarSettingsFormChoice({ ...input, toolName: 'solar-settings' })).toBe('typed')
    expect(solarSettingsFormChoice({ ...input, toolName: 'solar-homeruns' })).toBe('generic')
  })

  it('SF2 row14 the typed form is chosen for a standalone live settings context', () => {
    expect(solarSettingsFormChoice({ enabled: true, mock: false, toolName: 'solar-settings', context: C })).toBe('typed')
  })

  it('SF2 row15 a project context keeps the generic form', () => {
    expect(solarSettingsFormChoice({ enabled: true, mock: false, toolName: 'solar-settings', context: { ...C, projectId: 'p1' } })).toBe('generic')
  })

  it('SF2 row16 the flag off keeps the generic form', () => {
    expect(solarSettingsFormChoice({ enabled: false, mock: false, toolName: 'solar-settings', context: C })).toBe('generic')
  })

  it('SF2 row17 a settings write drops the selection overlays', () => {
    expect(prepareCatalogRunParams(
      { name: 'solar-settings', capabilities: ['drawing.write'], params: { properties: {} } },
      { expected_rev: 2, changes: { num_mppt: 4 } }, C,
      catalogRunOverlays({ enabled: true, toolName: 'solar-settings', selectedHandle: 'AB', isWrite: true }),
    )).toEqual({ expected_rev: 2, changes: { num_mppt: 4 }, drawing_id: 'd1' })
  })

  it('SF2 row18 another write tool keeps handle and target_handle', () => {
    expect(prepareCatalogRunParams(
      { name: 'other-write', capabilities: ['drawing.write'], params: { properties: {} } }, {}, C,
      catalogRunOverlays({ enabled: true, toolName: 'other-write', selectedHandle: 'AB', isWrite: true }),
    )).toEqual({ target_handle: 'AB', handle: 'AB', drawing_id: 'd1' })
  })

  it('SF2 row19 a read tool keeps target_handle', () => {
    expect(prepareCatalogRunParams(
      { name: 'other-read', capabilities: [], params: { properties: {} } }, {}, C,
      catalogRunOverlays({ enabled: true, toolName: 'other-read', selectedHandle: 'AB', isWrite: false }),
    )).toEqual({ target_handle: 'AB' })
  })

  it('SF2 row20b an invalid_seed_parent refusal reads the seed sentence', () => {
    expect(solarSettingsRunFeedback({ association: association(refused), context: C })).toEqual({ text: M, code: 'INVALID_SEED_PARENT' })
  })

  it('SF2 row21 an INVALID_SEED_PARENT failure reads the seed sentence', () => {
    expect(solarSettingsRunFeedback({ association: association({ ok: false, reason_code: 'INVALID_SEED_PARENT' }), context: C }))
      .toEqual({ text: M, code: 'INVALID_SEED_PARENT' })
  })

  it('SF2 row22 a result for another drawing version shows nothing', () => {
    expect(solarSettingsRunFeedback({ association: { ...association(refused), drawingVersion: 2 }, context: C })).toBeNull()
  })

  it('SF2 row25 another failure names its code', () => {
    expect(solarSettingsRunFeedback({ association: association({ ok: false, error: { error_code: 'BAD_PARAMS', message: 'STALE_GRAPH_REVISION' } }), context: C }))
      .toEqual({ text: 'Solar settings were not applied.', code: 'STALE_GRAPH_REVISION' })
  })

  it('SF2 row26 a successful run shows no run message', () => {
    expect(solarSettingsRunFeedback({ association: association({ ok: true }), context: C })).toBeNull()
  })

  it('SF2 row30 the controller scope is on for a standalone live Solar drawing', () => {
    expect(solarSettingsScope({ enabled: true, mock: false, profile: 'solar', context: C }))
      .toEqual({ drawingId: 'd1', drawingVersion: 3, solarSettingsFormEnabled: true })
  })

  it('SF2 row31 the controller scope resets outside it', () => {
    for (const override of [{ enabled: false }, { mock: true }, { profile: 'cad' }, { context: { ...C, projectId: 'p1' } }, { context: { ...C, drawingId: '' } }]) {
      const scope = solarSettingsScope({ enabled: true, mock: false, profile: 'solar', context: C, ...override })
      expect(scope).toEqual({ drawingId: undefined, drawingVersion: undefined, solarSettingsFormEnabled: false })
      expect(Object.hasOwn(scope, 'drawingId')).toBe(true)
      expect(Object.hasOwn(scope, 'drawingVersion')).toBe(true)
    }
  })

  it('SF2 row33 the loaders read with mock false', () => {
    const spyA = vi.fn()
    const spyB = vi.fn()
    const loaders = solarSettingsLoaders({ getDrawingIntake: spyA, getDrawingVersions: spyB })
    loaders.readIntake('d1', 3)
    loaders.readVersions('d1')
    expect(spyA).toHaveBeenCalledTimes(1)
    expect(spyA).toHaveBeenCalledWith(false, 'd1', 3)
    expect(spyB).toHaveBeenCalledTimes(1)
    expect(spyB).toHaveBeenCalledWith(false, 'd1')
    expect(Object.isFrozen(loaders)).toBe(true)
  })

  it('SF2 row34 the wire module imports nothing and carries no fenced text', () => {
    const source = readFileSync(resolve(process.cwd(), 'src', 'solar', 'solarSettingsWire.js'), 'utf8')
    expect(source).not.toMatch(/^\s*import\b/m)
    expect(source).not.toMatch(/\bimport\s*\(/)
    expect(source).not.toMatch(/\brequire\s*\(/)
    expect(source).not.toContain('solar-settings-form')
    expect(source).not.toContain('Choose the drawing units before starting a Solar design.')
  })

  it('SF2 row37 canOpenSolarSettingsForm accepts only the two seed singletons', () => {
    expect([
      canOpenSolarSettingsForm('solar-settings', A),
      canOpenSolarSettingsForm('solar-settings', { ...A, refusal_reasons: ['persisted_graph_unavailable'] }),
      canOpenSolarSettingsForm('solar-settings', { ...A, refusal_reasons: ['not_current_head'] }),
      canOpenSolarSettingsForm('solar-settings', { ...A, refusal_reasons: [] }),
      canOpenSolarSettingsForm('solar-settings', { ...A, implemented: 'true' }),
      canOpenSolarSettingsForm('solar-settings', null),
      canOpenSolarSettingsForm('solar-size-strings', A),
    ]).toEqual([true, true, false, false, false, false, false])
  })

  it('SF2 row38 canInitializeSolarSettings needs an own initialize key on a plain object', () => {
    class Seed { initialize = I }
    const array = []
    array.initialize = I
    expect([P, Object.create({ initialize: I }), { initialize: undefined }, array, null, new Seed()]
      .map((params) => canInitializeSolarSettings('solar-settings', A, params)))
      .toEqual([true, false, true, false, false, false])
    expect(canInitializeSolarSettings('solar-settings', A, Object.assign(Object.create(null), { initialize: I }))).toBe(true)
  })
})
