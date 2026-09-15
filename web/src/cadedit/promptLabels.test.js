import { describe, expect, it, vi } from 'vitest'
import { PROMPTS, humanizeRefusal } from './promptKeys.js'

describe('prompt labels and refusals', () => {
  it('names the shown line fields in the exact store refusal', () => {
    const fields = PROMPTS.createLine.steps.slice(0, 2).flatMap((step) => step.fields)
    expect(fields.map(([key, label]) => [key, label]))
      .toEqual([['x', 'x'], ['y', 'y'], ['x2', 'x2'], ['y2', 'y2']])
    expect(fields.map((field) => field.shown))
      .toEqual(['first point x', 'first point y', 'next point x', 'next point y'])
    expect(humanizeRefusal('Line refused: x, y, x2 and y2 must all be numbers.', PROMPTS.createLine))
      .toBe('Line refused: first point x, first point y, next point x and next point y must all be numbers.')
  })

  it('uses the circle labels without replacing letters inside words', () => {
    expect(humanizeRefusal('Circle refused: x, y and r must all be numbers.', PROMPTS.createCircle))
      .toBe('Circle refused: center x, center y and radius must all be numbers.')
    expect(humanizeRefusal('Circle refused: radius must be positive.', PROMPTS.createCircle))
      .toBe('Circle refused: radius must be positive.')
    expect(humanizeRefusal('index dx dy dx2', PROMPTS.move)).toBe('index displacement x displacement y dx2')
  })

  it('preserves point-entry grammar while humanizing scalar field names', () => {
    const grammar = 'LINE refused: "10,5,6" is not a point: use x,y, @dx,dy, dist<angle or @dist<angle.'
    expect(humanizeRefusal(grammar, PROMPTS.createLine)).toBe(grammar)
    expect(humanizeRefusal(grammar, PROMPTS.move)).toBe(grammar)
    expect(PROMPTS.createCircle.steps[1].fields[0][1]).toBe('r')
    expect(humanizeRefusal('CIRCLE refused: r needs a scalar, not a point.', PROMPTS.createCircle))
      .toBe('CIRCLE refused: radius needs a scalar, not a point.')
  })

  it.each([
    ['existing positional label', 'createInsert', 'INSERT refused: x scale must be a number.', 'INSERT refused: x scale must be a number.'],
    ['quoted point and grammar', 'createLine', 'LINE refused: "x,y,z" is not a point: use x,y, @dx,dy, dist<angle or @dist<angle.', 'LINE refused: "x,y,z" is not a point: use x,y, @dx,dy, dist<angle or @dist<angle.'],
    ['quoted block name', 'createInsert', 'INSERT refused: block "x" was not found.', 'INSERT refused: block "x" was not found.'],
    ['quoted Unicode name', 'createLine', 'Layer "éx" is unavailable.', 'Layer "éx" is unavailable.'],
    ['Unicode token boundaries', 'createLine', 'éx xé 2x x2é _x x_', 'éx xé 2x x2é _x x_'],
    ['existing shown labels', 'createLine', 'first point x and next point y must be numbers.', 'first point x and next point y must be numbers.'],
    ['scale key', 'createInsert', 'sx must be a number.', 'x scale must be a number.'],
    ['line keys', 'createLine', 'Line refused: x, y, x2 and y2 must all be numbers.', 'Line refused: first point x, first point y, next point x and next point y must all be numbers.'],
    ['circle key', 'createCircle', 'CIRCLE refused: r needs a scalar, not a point.', 'CIRCLE refused: radius needs a scalar, not a point.'],
    ['move keys', 'move', 'Move refused: dx and dy must both be numbers.', 'Move refused: displacement x and displacement y must both be numbers.'],
    ['unmatched quote', 'createLine', 'LINE refused: "x must be a number.', 'LINE refused: "first point x must be a number.'],
  ])('preserves %s and is idempotent', (_reason, op, sentence, expected) => {
    const result = humanizeRefusal(sentence, PROMPTS[op])
    expect(result).toBe(expected)
    expect(humanizeRefusal(result, PROMPTS[op])).toBe(expected)
  })

  it('constructs no lookbehind pattern for any prompt', () => {
    const NativeRegExp = globalThis.RegExp
    const constructor = vi.spyOn(globalThis, 'RegExp').mockImplementation(function (source, flags) {
      return new NativeRegExp(source, flags)
    })
    let sources
    try {
      for (const prompt of Object.values(PROMPTS)) {
        humanizeRefusal('x r dx sx', { ...prompt })
      }
      sources = constructor.mock.calls.map(([source]) => String(source))
    } finally {
      constructor.mockRestore()
    }
    expect(sources).toHaveLength(Object.keys(PROMPTS).length)
    for (const source of sources) expect(source).not.toContain('(?<')
  })

  it('constructs the pattern once per prompt object', () => {
    const prompt = { ...PROMPTS.createLine }
    const NativeRegExp = globalThis.RegExp
    const constructor = vi.spyOn(globalThis, 'RegExp').mockImplementation(function (source, flags) {
      return new NativeRegExp(source, flags)
    })
    let constructions
    let first
    let second
    try {
      first = humanizeRefusal('x', prompt)
      second = humanizeRefusal('y', prompt)
      constructions = constructor.mock.calls.length
    } finally {
      constructor.mockRestore()
    }
    expect(first).toBe('first point x')
    expect(second).toBe('first point y')
    expect(constructions).toBe(1)
  })

  it('keeps the declared adjacent-label output', () => {
    expect(humanizeRefusal('x2first point x', PROMPTS.createLine))
      .toBe('x2first point first point x')
  })

  it('preserves sentences without key tokens and absent prompts', () => {
    const sentence = 'Line refused: the two points must differ.'
    expect(humanizeRefusal(sentence, PROMPTS.createLine)).toBe(sentence)
    expect(humanizeRefusal(sentence, null)).toBe(sentence)
    expect(humanizeRefusal('', PROMPTS.createLine)).toBe('')
  })
})
