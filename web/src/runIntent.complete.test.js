import { describe, it, expect } from 'vitest'
import {
  prepareCatalogRunParams, createRunIntentState, createCatalogToolSnapshot,
  stageRunIntent, confirmRunIntent,
} from './runIntent.js'

const C = { tenantId: 't1', drawingId: 'd1', drawingVersion: 3 }
const N = { nullable_size: { type: ['number', 'null'], default: 3 } }
const S = { source_layer: { type: 'string', default: 'Panels' } }
const T = (properties, capabilities = []) => ({
  name: 'f', capabilities, params: { type: 'object', properties },
})
const complete = { complete: true }

describe('complete catalog submissions', () => {
  it('FORMB01 cleared nullable stays absent', () => {
    expect(prepareCatalogRunParams(T(N), {}, C, {}, complete)).toEqual({})
  })

  it('FORMB02 cleared string stays absent', () => {
    expect(prepareCatalogRunParams(T(S), {}, C, {}, complete)).toEqual({})
  })

  it('FORMB03 untouched defaults survive', () => {
    const params = { nullable_size: 3, source_layer: 'Panels' }
    expect(prepareCatalogRunParams(T({ ...N, ...S }), params, C, {}, complete)).toEqual(params)
  })

  it('FORMB04 explicit null survives', () => {
    expect(prepareCatalogRunParams(T(N), { nullable_size: null }, C, {}, complete))
      .toEqual({ nullable_size: null })
  })

  it('FORMB05 partial requests retain defaults', () => {
    const tool = T({ ...N, ...S })
    const expected = { nullable_size: 3, source_layer: 'Panels' }
    expect(prepareCatalogRunParams(tool, {}, C)).toEqual(expected)
    expect(prepareCatalogRunParams(tool, {}, C, {}, { complete: false })).toEqual(expected)
  })

  it('FORMB06 overlays still win', () => {
    expect(prepareCatalogRunParams(T({ ...N, ...S }), { source_layer: 'Roofs' }, C,
      { source_layer: 'Selected', handle: 'AB' }, complete))
      .toEqual({ handle: 'AB', source_layer: 'Selected' })
  })

  it('FORMB07 drawing binding still wins', () => {
    const tool = T({ ...N, drawing_id: { type: 'string', default: 'wrong' } }, ['drawing.write'])
    expect(prepareCatalogRunParams(tool, { drawing_id: 'other' },
      { ...C, drawingArtifactId: 'artifact-1' }, { drawing_id: 'overlay-wrong' }, complete))
      .toEqual({ drawing_id: 'artifact-1' })
  })

  it('FORMB08 typed payload survives preparation', () => {
    const tool = {
      name: 'typed-param-fixture', version: '1.0.0',
      description: 'Prove JSON Schema parameter types.', kind: 'script',
      capabilities: ['drawing.read'],
      params: { type: 'object', properties: {
        spheres: { type: 'array', items: { type: 'object' }, default: 1 },
        sphere_options: { type: 'object' },
        marker_size: { type: ['number', 'null'], default: 2 },
        ...N, dry_run: { type: 'boolean', default: false },
        count: { type: 'number', default: 1 }, ...S,
      } },
      provenance: { author: 'user' },
    }
    const params = {
      spheres: [{ center: [0, 0, 0], radius: 10 }], sphere_options: { segments: 24 },
      marker_size: 2.5, dry_run: true, count: 4, source_layer: 'Roofs',
    }
    expect(prepareCatalogRunParams(tool, params, C, {}, complete)).toEqual(params)
  })

  it('FORMB09 confirmation compares the omitted object', () => {
    const tool = T(N)
    const toolSnapshot = createCatalogToolSnapshot(tool)
    const staged = stageRunIntent(createRunIntentState('s1'), {
      intentId: 'i1', toolName: tool.name,
      params: prepareCatalogRunParams(tool, {}, C, {}, complete),
      context: C, toolSnapshot, createdAt: 1000,
    })
    const request = { ...staged.intent, context: C, toolSnapshot }
    const confirmed = confirmRunIntent(staged.state, { ...request, params: {} }, { now: 1001 })
    expect(confirmed.ok).toBe(true)
    expect(confirmed.execution.params).toEqual({})
    const changed = confirmRunIntent(staged.state,
      { ...request, params: { nullable_size: 3 } }, { now: 1001 })
    expect(changed.ok).toBe(false)
    expect(changed.code).toBe('changed_params')
  })
})
