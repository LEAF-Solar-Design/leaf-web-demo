import { describe, expect, it, vi } from 'vitest'
import { createSolarDrawingBinding, solarDrawingBindingFromRunContext, sameSolarDrawingBinding,
  sameSolarCheckoutBinding, solarDrawingBindingKey, solarCheckoutBindingKey } from './solarDrawingBinding.js'
import { createSolarProjectClient } from './solarProjectClient.js'
import { solarSettingsScope } from './solarSettingsWire.js'

const O = '11111111-1111-1111-1111-111111111111'
const P = '22222222-2222-2222-2222-222222222222'
const D = '33333333-3333-3333-3333-333333333333'
const V = '44444444-4444-4444-4444-444444444444'
const binding = { kind: 'project', organization_id: O, project_id: P, drawing_id: D, input_version_id: V }
const context = { orgId: O, projectId: P, drawingArtifactId: D, drawingId: V, drawingVersion: null }

describe('canonical Solar drawing binding', () => {
  it('R7-01 project settings scope documents the R13 boundary', () => {
    expect(solarSettingsScope({ enabled: true, mock: false, profile: 'solar', context })).toEqual({
      drawingId: undefined, drawingVersion: undefined, solarSettingsFormEnabled: false })
  })
  it('R7-04 binding is a frozen exact five-field value with full and checkout equality', () => {
    const value = createSolarDrawingBinding(binding)
    expect(value).not.toBe(binding)
    expect(Object.isFrozen(value)).toBe(true)
    expect(Object.keys(value)).toEqual(['kind', 'organization_id', 'project_id', 'drawing_id', 'input_version_id'])
    expect(sameSolarDrawingBinding(value, { ...binding })).toBe(true)
    const next = { ...binding, input_version_id: O }
    expect(sameSolarDrawingBinding(value, next)).toBe(false)
    expect(sameSolarCheckoutBinding(value, next)).toBe(true)
    expect(solarDrawingBindingKey(value)).toBe(`solar-binding|project|${O}|${P}|${D}|${V}`)
    expect(solarCheckoutBindingKey(value)).toBe(`solar-checkout|project|${O}|${P}|${D}`)
    expect(sameSolarDrawingBinding(value, null)).toBe(false)
  })
  it('R7-05 invalid binding and a swapped intent refuse without dispatch', async () => {
    const accessor = { ...binding }
    Object.defineProperty(accessor, 'drawing_id', { get: () => D })
    for (const value of [{ ...binding, input_version_id: 3 }, { ...binding, extra: true }, accessor,
      { ...binding, drawing_id: 'invalid' }, { ...binding, organization_id: O.toUpperCase().replace('1', 'A') }]) {
      expect(() => createSolarDrawingBinding(value)).toThrow(TypeError)
    }
    const fetchImpl = vi.fn()
    const client = createSolarProjectClient({ fetchImpl })
    expect((await client.readContext()).reason).toBe('SIP_R7_BINDING_INVALID')
    client.setScope(binding, 'principal')
    const intent = client.prepareRun({ tool: 'solar-settings', params: {}, catalogDigest: 'sha256:' + 'a'.repeat(64), idempotencyKey: 'one' })
    expect((await client.submitRun({ ...intent, binding: { ...binding, drawing_id: V, input_version_id: D } })).reason).toBe('SIP_R7_REQUEST_INVALID')
    expect(fetchImpl).not.toHaveBeenCalled()
    client.dispose()
  })
  it('R7-06 run context maps artifact to D and drawingId to V with no legacy version', () => {
    expect(solarDrawingBindingFromRunContext(context)).toEqual(binding)
    expect(() => solarDrawingBindingFromRunContext({ ...context, drawingVersion: 1 })).toThrow(TypeError)
    expect(() => solarDrawingBindingFromRunContext({ ...context, drawingArtifactId: undefined })).toThrow(TypeError)
  })
})
