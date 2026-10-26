import { act, cleanup, render } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import EngineSessionProvider, { useEngineSessionContext } from './EngineSessionProvider.jsx'
import { clipboardRecord, pasteOp } from './clipboard.js'
import { buildCreatePayload } from './engineSession.js'

const curved = () => ({
  id: '10', handle: '10', type: 'LWPOLYLINE', layer: '0', editable: true,
  closed: true, vertices: [[0, 0], [4, 0], [4, 3]], bulges: [0.5, 0, -0.25],
})

let context
function Probe() { context = useEngineSessionContext(); return null }
class Worker {
  posted = []
  listeners = new Map()
  addEventListener(type, listener) { this.listeners.set(type, listener) }
  removeEventListener(type) { this.listeners.delete(type) }
  postMessage(message) { this.posted.push(message) }
  terminate() {}
  emit(message) { act(() => this.listeners.get('message')({ data: message })) }
}

function mount(entity) {
  const worker = new Worker()
  render(<EngineSessionProvider createWorker={() => worker}><Probe /></EngineSessionProvider>)
  act(() => context.session.actions.openBytes(new Uint8Array([48, 10]), 'curves.dxf'))
  worker.emit({ type: 'documentLoaded', documentId: 'curves.dxf', entities: [entity], entityCount: 1 })
  act(() => context.session.actions.select(entity.id))
  return worker
}

afterEach(() => { cleanup(); context = null })

describe('S1-107 curved polyline clipboard', () => {
  it('copies a frozen curve snapshot and preserves every segment when translated', () => {
    for (const type of ['LWPOLYLINE', 'POLYLINE']) {
      const source = { ...curved(), type }
      const { record } = clipboardRecord(source)
      expect(record.bulges).toEqual([0.5, 0, -0.25])
      expect(Object.isFrozen(record.bulges)).toBe(true)
      source.bulges[0] = 99
      const answer = pasteOp(record, 10, 20)
      expect(buildCreatePayload(answer.op, answer.inputs).payload).toEqual({
        points: [10, 20, 14, 20, 14, 23], closed: true, layer: '0', bulges: [0.5, 0, -0.25],
      })
      answer.inputs.bulges[0] = 88
      expect(pasteOp(record, 0, 0).inputs.bulges).toEqual([0.5, 0, -0.25])
    }
  })

  it('cuts the original only after retaining its curves and pastes them into the create payload', () => {
    const worker = mount(curved())
    act(() => context.session.actions.copyToClipboard(true))
    expect(context.session.clipboard.bulges).toEqual([0.5, 0, -0.25])
    expect(worker.posted.filter((message) => message.type === 'applyEdit').map((message) => message.op)).toEqual(['delete'])
    worker.emit({ type: 'editApplied', op: 'delete', ok: true, entities: [], entityCount: 0, bytes: new Uint8Array([1]), byteLength: 1 })
    act(() => context.session.actions.pasteFromClipboard({ x: '10', y: '20' }))
    expect(worker.posted.filter((message) => message.type === 'applyEdit').at(-1)).toMatchObject({
      op: 'createPolyline',
      payload: { points: [10, 20, 14, 20, 14, 23], closed: true, bulges: [0.5, 0, -0.25] },
    })
  })

  it('copies an open curve without deleting it and preserves its bends on paste', () => {
    const worker = mount({ ...curved(), closed: false, bulges: [-0.5, 0.25, 0] })
    act(() => context.session.actions.copyToClipboard())
    expect(worker.posted.filter((message) => message.type === 'applyEdit')).toEqual([])
    act(() => context.session.actions.pasteFromClipboard({ x: '-10', y: '5' }))
    expect(worker.posted.filter((message) => message.type === 'applyEdit')).toEqual([
      expect.objectContaining({
        op: 'createPolyline',
        payload: expect.objectContaining({
          points: [-10, 5, -6, 5, -6, 8], closed: false, bulges: [-0.5, 0.25, 0],
        }),
      }),
    ])
  })

  it('refuses malformed curve information before a cut deletes the source', () => {
    const worker = mount({ ...curved(), bulges: [0.5] })
    act(() => context.session.actions.copyToClipboard(true))
    expect(context.session.clipboard).toBeNull()
    expect(worker.posted.filter((message) => message.type === 'applyEdit')).toEqual([])
    expect(context.session.status).toBe('Cut refused: this polyline has unreadable curve information.')
    for (const bulges of [[0, NaN, 0], [0, Infinity, 0], new Array(3), '0.5']) {
      expect(clipboardRecord({ ...curved(), bulges }).refusal).toBeTruthy()
    }
  })
})
