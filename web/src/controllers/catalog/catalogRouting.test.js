import { describe, expect, it } from 'vitest'
import { alternativeDecision, solveRunDecision } from './catalogRouting.js'

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

describe('solveRunDecision', () => {
  it('turns a bound solve decision into a run of that tool', () => {
    const bound = {
      lane: 'solve',
      tool: 'string-autofill-opt',
      params: { panelsPerString: 12 },
      confidence: 0.43,
      rationale: "Matched the 'string-autofill-opt' solver capability; confirm before it runs.",
      alternatives: [{ tool: 'solar-solve-proposal', confidence: 0.39 }],
    }

    const decision = solveRunDecision(bound)

    expect(decision).toEqual({ ...bound, lane: 'run', routedLane: 'solve' })
    expect(decision).not.toBe(bound)
    expect(bound.lane).toBe('solve')
    expect('routedLane' in bound).toBe(false)
  })

  it('trims the bound tool name', () => {
    expect(solveRunDecision({ lane: 'solve', tool: '  arlo-design  ', confidence: 0.5 }))
      .toEqual({ lane: 'run', tool: 'arlo-design', confidence: 0.5, routedLane: 'solve' })
  })

  it.each([null, undefined, '', '   ', 42])('leaves an unbound solve decision unchanged for tool %s', (tool) => {
    const decision = { lane: 'solve', tool, confidence: 0.8, alternatives: [] }
    expect(solveRunDecision(decision)).toBe(decision)
  })

  it.each(['run', 'build'])('leaves a %s decision unchanged', (lane) => {
    const decision = { lane, tool: 'count-by-layer', confidence: 0.9 }
    expect(solveRunDecision(decision)).toBe(decision)
  })

  it('passes a missing decision through', () => {
    expect(solveRunDecision(null)).toBeNull()
    expect(solveRunDecision(undefined)).toBeUndefined()
  })
})
