// W4g-7a: the prompt's effective inputs, extracted from the ribbon so the
// script runner shares it. These rows pin the behaviour the ribbon had.
import { describe, expect, it } from 'vitest'

import { PROMPTS } from './EngineRibbonClusters.jsx'
import { isPointStep, resolvePromptInputs } from './promptInputs.js'

describe('resolvePromptInputs', () => {
  it('resolves a point expression in a step\'s first field into both fields, chaining the next point off the first', () => {
    const out = resolvePromptInputs(PROMPTS.createLine, { x: '10,5', y: '', x2: '@5,5', y2: '', layer: '0' })
    expect(out.effective).toEqual({ x: '10', y: '5', x2: '15', y2: '10', layer: '0' })
    expect(out.expressionRefusal).toBe('')
    expect(out.waitingStep).toBeNull()
    expect(out.pointSteps).toHaveLength(2)
    expect(out.failedExpression.size).toBe(0)
  })

  it('a first point resolves against the chain point; a displacement against the origin', () => {
    // "@20<0" is relative to the first point; a bare "20<0" is absolute polar from the origin.
    expect(resolvePromptInputs(PROMPTS.createLine, { x: '@0,0', y: '', x2: '@20<0', y2: '' }, [30, 40]).effective).toMatchObject({ x: '30', y: '40', x2: '50', y2: '40' })
    expect(resolvePromptInputs(PROMPTS.createLine, { x: '@0,0', y: '', x2: '20<0', y2: '' }, [30, 40]).effective).toMatchObject({ x: '30', y: '40', x2: '20', y2: '0' })
    expect(resolvePromptInputs(PROMPTS.move, { dx: '@3,4', dy: '' }).effective).toEqual({ dx: '3', dy: '4' })
    expect(resolvePromptInputs(PROMPTS.move, { dx: '10<90', dy: '' }).effective).toEqual({ dx: '0', dy: '10' })
  })

  it('names the field whose expression failed, and a step whose operand is still empty', () => {
    const bad = resolvePromptInputs(PROMPTS.createLine, { x: '1,2,3', y: '', x2: '5,5', y2: '' })
    expect(bad.expressionRefusal).toMatch(/^LINE refused: /)
    expect(bad.failedExpression.has('x')).toBe(true)
    expect(bad.waitingStep).toBeNull()
    const waiting = resolvePromptInputs(PROMPTS.createCircle, { x: '1', y: '2', r: '' })
    expect(waiting.waitingStep.ask).toBe('Specify radius:')
    const edge = resolvePromptInputs(PROMPTS.trim, { edge: '', x: '1', y: '1' })
    expect(edge.waitingStep.ask).toBe('Select cutting edge:')
    // A relative first point with nothing to anchor on is a refusal, not a wait.
    expect(resolvePromptInputs(PROMPTS.createLine, { x: '@1,1', y: '', x2: '5,5', y2: '' }).expressionRefusal).toMatch(/LINE refused/)
  })

  it('plain numbers pass through untouched; no prompt yields an empty answer', () => {
    expect(resolvePromptInputs(PROMPTS.createCircle, { x: '1', y: '2', r: '3' }).effective).toEqual({ x: '1', y: '2', r: '3' })
    expect(resolvePromptInputs(null, { x: '1' })).toEqual({ effective: { x: '1' }, expressionRefusal: '', failedExpression: new Set(), waitingStep: null, pointSteps: [] })
    expect(isPointStep(PROMPTS.createLine.steps[0])).toBe(true)
    expect(isPointStep(PROMPTS.createCircle.steps[1])).toBe(false)
    expect(isPointStep(PROMPTS.fillet.steps[1])).toBe(false)
  })
})
