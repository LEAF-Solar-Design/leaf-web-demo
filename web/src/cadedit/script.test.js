// W4g-7a SCRIPT: the parser, pure rows. Every line maps onto the ribbon's
// own prompt grammar (PROMPTS) through the command line's own word table.
import { describe, expect, it } from 'vitest'

import { PROMPTS } from './EngineRibbonClusters.jsx'
import { parseDrawingCommand } from '../lib/commandWords.js'
import { BARE_OPS, MAX_SCRIPT_CHARS, MAX_SCRIPT_LINES, MAX_TOKEN_CHARS, parseScript, promptSlots, tokenize } from './script.js'

const parse = (text) => parseScript(text, parseDrawingCommand, PROMPTS)

describe('tokenize', () => {
  it('splits on whitespace and keeps a double-quoted run as one token', () => {
    expect(tokenize('text 0,0 2.5 0 "Panel A" Notes').tokens).toEqual(['text', '0,0', '2.5', '0', 'Panel A', 'Notes'])
    expect(tokenize('  line\t0,0   10,10 ').tokens).toEqual(['line', '0,0', '10,10'])
    expect(tokenize('text 0,0 2.5 0 "open').refusal).toMatch(/no closing quote/)
    expect(tokenize(`line ${'9'.repeat(MAX_TOKEN_CHARS + 1)}`).refusal).toMatch(new RegExp(`longer than ${MAX_TOKEN_CHARS}`))
  })
})

describe('promptSlots', () => {
  it('folds a point pair into one slot and keeps every other field its own', () => {
    expect(promptSlots(PROMPTS.createLine).map((s) => s.kind)).toEqual(['point', 'point', 'text'])
    expect(promptSlots(PROMPTS.createCircle).map((s) => [s.kind, s.keys.join(',')])).toEqual([['point', 'x,y'], ['number', 'r'], ['text', 'layer']])
    expect(promptSlots(PROMPTS.createPolyline).map((s) => s.kind)).toEqual(['text', 'checkbox', 'text'])
    expect(promptSlots(PROMPTS.move).map((s) => s.keys)).toEqual([['dx', 'dy']])
    // FILLET: radius, then the edge id and the point on it, then the point on the first line.
    expect(promptSlots(PROMPTS.fillet).map((s) => [s.kind, s.keys.join(',')])).toEqual([['number', 'r'], ['edge', 'edge'], ['point', 'ex,ey'], ['point', 'x,y']])
    expect(promptSlots(PROMPTS.arrayRect).map((s) => s.kind)).toEqual(['number', 'number', 'number', 'number'])
  })
})

describe('parseScript', () => {
  it('reads command words with operands in the prompt order, skipping blanks and ; comments', () => {
    const out = parse('; two lines and a circle\nline 0,0 10,10\n\nL @5,0 20<90\ncircle 10,10 5 Round\n')
    expect(out.refusal).toBeUndefined()
    expect(out.lines.map((l) => [l.line, l.op, l.inputs])).toEqual([
      [2, 'createLine', { x: '0,0', y: '', x2: '10,10', y2: '' }],
      [4, 'createLine', { x: '@5,0', y: '', x2: '20<90', y2: '' }],
      [5, 'createCircle', { x: '10,10', y: '', r: '5', layer: 'Round' }],
    ])
    expect(out.lines[0].verb).toBe('LINE')
    expect(Object.isFrozen(out.lines[0])).toBe(true)
  })

  it('an operand left off keeps the prompt default; a quoted text carries spaces; yes/no reads a checkbox', () => {
    expect(parse('text 0,0 2.5 0 "Panel A"').lines[0].inputs).toEqual({ x: '0,0', y: '', height: '2.5', rot: '0', text: 'Panel A' })
    expect(parse('pline "0,0 10,0 10,10" yes').lines[0].inputs).toEqual({ pts: '0,0 10,0 10,10', closed: 'true' })
    expect(parse('pline "0,0 10,0" NO').lines[0].inputs.closed).toBe('false')
    expect(parse('mirror 0,0 0,10 maybe').refusal).toBe('line 1: MIRROR operand 3 must be yes or no, got "maybe"')
  })

  it('bare words take no operand; vertex edits and the rest ride their prompts', () => {
    expect(parse('e\nx\nu\nredo\ncopyclip\ncutclip').lines.map((l) => l.op)).toEqual(['delete', 'explode', 'undo', 'redo', 'copyClip', 'cutClip'])
    expect(parse('erase 1').refusal).toBe('line 1: ERASE takes no operand')
    expect(BARE_OPS.has('delete')).toBe(true)
    expect(parse('tr 9 8,0').lines[0].inputs).toEqual({ edge: '9', x: '8,0', y: '' })
    expect(parse('f 2 9 10,8 2,0').lines[0].inputs).toEqual({ r: '2', edge: '9', ex: '10,8', ey: '', x: '2,0', y: '' })
  })

  it('refuses the FIRST unreadable line by number, before anything runs', () => {
    expect(parse('line 0,0 10,10\nfoo 1 2').refusal).toBe('line 2: "foo" is not a command word')
    expect(parse('line 0,0 10,10\nline 5 10,10').refusal).toBe('line 2: LINE operand 1 must be a point (x,y or @dx,dy or dist<angle), got "5"')
    expect(parse('circle 0,0 5 A extra').refusal).toBe('line 1: CIRCLE takes at most 3 operands')
    expect(parse('line 0,0 10,10\n\n\n  ; ok\nline "open').refusal).toBe('line 5: an opening quote has no closing quote')
    expect(parse('line 0,0 10,10\nfoo').line).toBe(2)
  })

  it('is bounded before any line is read', () => {
    expect(parse('x'.repeat(MAX_SCRIPT_CHARS + 1)).refusal).toMatch(/longer than/)
    expect(parse(Array.from({ length: MAX_SCRIPT_LINES + 1 }, () => 'e').join('\n')).refusal).toMatch(/more than 5000 lines/)
    expect(parse(Array.from({ length: MAX_SCRIPT_LINES }, () => 'e').join('\n')).lines).toHaveLength(MAX_SCRIPT_LINES)
    expect(parse(42).refusal).toBe('the script is not text')
    expect(parse('').lines).toEqual([])
  })
})
