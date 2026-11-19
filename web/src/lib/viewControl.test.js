import { describe, expect, it, vi } from 'vitest'
import { announceViewRequest, findLayerName, viewRequestFromEvent, VIEW_CONTROL_EVENT } from './viewControl.js'

const ts = '2026-11-19T20:30:00.000Z'
const now = Date.parse(ts)
const event = (request = { op: 'fit' }) => ({
  type: 'tool_result', seq: 1, ts,
  data: { tool: 'view_control', ok: true, result: { contract: 'leaf.view-request.v1', ...request } },
})

describe('viewRequestFromEvent', () => {
  it.each(['fit', 'zoom_in', 'zoom_out'])('accepts %s without a layer', op => {
    expect(viewRequestFromEvent(event({ op }), now)).toEqual({ op })
    expect(viewRequestFromEvent(event({ op, layer: 'Roof' }), now)).toBeNull()
    expect(viewRequestFromEvent(event({ op, layer: undefined }), now)).toBeNull()
  })

  it('accepts a named layer and returns only the request fields', () => {
    for (const layer of ['R', 'a'.repeat(255)]) {
      const env = event({ op: 'zoom_to_layer', layer, applied_by: 'client' })
      delete env.data.ok
      expect(viewRequestFromEvent(env, now)).toEqual({ op: 'zoom_to_layer', layer })
    }
    for (const layer of [undefined, null, 123, '', 'a'.repeat(256), 'r\u0000', '\n', 'r\u001f', '\u007f']) {
      expect(viewRequestFromEvent(event({ op: 'zoom_to_layer', layer }), now)).toBeNull()
    }
  })

  it('requires a successful view tool result with a known contract and operation', () => {
    const env = event()
    for (const invalid of [
      null, {}, { ...env, type: 'tool_call' }, { ...env, data: null },
      { ...env, data: { ...env.data, tool: 'drawing_state' } },
      { ...env, data: { ...env.data, ok: false } },
      { ...env, data: { ...env.data, result: null } },
      event({ contract: 'unknown' }), event({ op: 'pan' }),
    ]) expect(viewRequestFromEvent(invalid, now)).toBeNull()
  })

  it('accepts events up to sixty seconds old and rejects missing, invalid or future timestamps', () => {
    expect(viewRequestFromEvent(event(), now + 60000)).toEqual({ op: 'fit' })
    expect(viewRequestFromEvent(event(), now + 60001)).toBeNull()
    expect(viewRequestFromEvent(event(), now - 1)).toBeNull()
    for (const invalid of [undefined, null, 'bad']) {
      expect(viewRequestFromEvent({ ...event(), ts: invalid }, now)).toBeNull()
    }
  })
})

describe('findLayerName', () => {
  it('prefers exact spelling, then a unique case-insensitive match, then a unique substring', () => {
    expect(findLayerName(['Roof edge', 'ROOF', 'Roof'], 'Roof')).toBe('Roof')
    expect(findLayerName(['Roof edge', 'ROOF'], 'roof')).toBe('ROOF')
    expect(findLayerName(['Walls', 'A-Roof'], 'ROO')).toBe('A-Roof')
  })

  it('rejects ambiguous and missing layers and inspects only the first 10,000', () => {
    expect(findLayerName(['Roof', 'ROOF'], 'roof')).toBeNull()
    expect(findLayerName(['Roof edge', 'Roof line'], 'roof')).toBeNull()
    expect(findLayerName(['Wall'], 'roof')).toBeNull()
    expect(findLayerName(null, 'roof')).toBeNull()
    const names = [...Array(9999).fill('Other'), 'Near', 'Far']
    expect(findLayerName(names, 'near')).toBe('Near')
    expect(findLayerName(names, 'far')).toBeNull()
  })
})

describe('announceViewRequest', () => {
  it('delivers once across streams per session and sequence, ignoring stale events', () => {
    const target = new EventTarget()
    const listener = vi.fn()
    target.addEventListener('cockpit:view', listener)
    const env = event({ op: 'zoom_to_layer', layer: 'Roof' })
    announceViewRequest(env, 'a', now + 60001, target)
    expect(listener).not.toHaveBeenCalled()
    announceViewRequest(env, 'a', now, target)
    announceViewRequest(env, 'a', now, target)
    announceViewRequest(env, 'b', now, target)
    expect(listener).toHaveBeenCalledTimes(2)
    expect(listener.mock.calls[0][0].detail).toEqual({ op: 'zoom_to_layer', layer: 'Roof' })
  })

  it('evicts the oldest of 64 keys', () => {
    const target = new EventTarget()
    const listener = vi.fn()
    target.addEventListener(VIEW_CONTROL_EVENT, listener)
    const send = seq => announceViewRequest({ ...event(), seq }, 'eviction', now, target)
    for (let seq = 1; seq <= 64; seq++) send(seq)
    send(1)
    expect(listener).toHaveBeenCalledTimes(64)
    send(65)
    send(2)
    expect(listener).toHaveBeenCalledTimes(65)
    send(1)
    expect(listener).toHaveBeenCalledTimes(66)
  })
})
