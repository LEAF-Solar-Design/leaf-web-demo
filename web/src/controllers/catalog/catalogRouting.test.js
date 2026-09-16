import { describe, expect, it } from 'vitest'
import { alternativeDecision } from './catalogRouting.js'

describe('alternativeDecision', () => {
  it('keeps demo provenance and removes the picked tool from alternatives', () => {
    const picked = alternativeDecision({
      lane: 'run',
      tool: null,
      confidence: 0,
      stub: true,
      stubKind: 'demo',
      alternatives: [{ tool: 'count-by-layer' }, { tool: 'other' }],
    }, 'count-by-layer')

    expect(picked.stubKind).toBe('demo')
    expect(picked.stub).toBe(true)
    expect(picked.tool).toBe('count-by-layer')
    expect(picked.confidence).toBe(0.99)
    expect(picked.alternatives).toEqual([{ tool: 'other' }])
  })

  it('drops the outage kind after a client-side pick but preserves the outage reason', () => {
    const picked = alternativeDecision({
      lane: 'run',
      tool: null,
      confidence: 0,
      stub: true,
      stubKind: 'outage',
      stubReason: 'Connection lost',
    }, 'count-by-layer')

    expect(picked.stubKind).toBeUndefined()
    expect(picked.stubReason).toBe('Connection lost')
  })

  it('does not add stub provenance to a real route pick', () => {
    const picked = alternativeDecision({
      lane: 'run',
      tool: null,
      confidence: 0,
      alternatives: [{ tool: 'count-by-layer' }],
    }, 'count-by-layer')

    expect(picked.stub).toBeUndefined()
    expect(picked.stubKind).toBeUndefined()
    expect(picked.stubReason).toBeUndefined()
  })
})
