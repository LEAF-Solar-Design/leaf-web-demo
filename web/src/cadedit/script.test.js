// W4g-7a SCRIPT: the parser, pure rows. Every line maps onto the ribbon's
// own prompt grammar (PROMPTS) through the command line's own word table.
import { describe, expect, it } from 'vitest'

import { DEFERRED_REASONS } from '../lib/actionRegistry.js'
import { PROMPTS } from './EngineRibbonClusters.jsx'
import { parseDrawingCommand } from '../lib/commandWords.js'
import { BARE_OPS, MAX_SCRIPT_CHARS, MAX_SCRIPT_LINES, MAX_TOKEN_CHARS, parseScript, promptSlots, tokenize } from './script.js'

const parse = (text) => parseScript(text, parseDrawingCommand, PROMPTS)

it('MLEADER slots are two points, quoted text, then optional style and layer', () => {
  expect(promptSlots(PROMPTS.createMleader).map((s) => [s.kind, s.keys.join(',')])).toEqual([
    ['point', 'x,y'], ['point', 'x2,y2'], ['text', 'text'], ['text', 'style'], ['text', 'layer'],
  ])
  expect(parse('MLEADER 30,23 35,26 "Valve"').lines[0]).toMatchObject({
    group: 'draw', op: 'createMleader', verb: 'MLEADER', inputs: { x: '30,23', y: '', x2: '35,26', y2: '', text: 'Valve' },
  })
  expect(parse('ML 0,0 3,4 "Valve A" Notes Leaders').lines[0].inputs).toEqual({
    x: '0,0', y: '', x2: '3,4', y2: '', text: 'Valve A', style: 'Notes', layer: 'Leaders',
  })
  expect(parse('MLEADER 0,0 bad "Valve"').refusal).toMatch(/operand 2 must be a point/)
})

describe('tokenize', () => {
  it('splits on whitespace and keeps a double-quoted run as one token', () => {
    expect(tokenize('text 0,0 2.5 0 "Panel A" Notes').tokens).toEqual(['text', '0,0', '2.5', '0', 'Panel A', 'Notes'])
    expect(tokenize('  line\t0,0   10,10 ').tokens).toEqual(['line', '0,0', '10,10'])
    expect(tokenize('text 0,0 2.5 0 "open').refusal).toMatch(/no closing quote/)
    expect(tokenize(`line ${'9'.repeat(MAX_TOKEN_CHARS + 1)}`).refusal).toMatch(new RegExp(`longer than ${MAX_TOKEN_CHARS}`))
  })
})

describe('promptSlots', () => {
  it('GROUP reads a name before repeated hexadecimal member handles', () => {
    expect(promptSlots(PROMPTS.group)).toEqual([{ keys: ['groupName'], kind: 'text' }, { keys: ['members'], kind: 'edge', repeat: true }])
    expect(parse('GROUP "Rack one" A 20').lines[0].inputs).toEqual({ groupName: 'Rack one', members: '10 32' })
    expect(parse('GROUP RACK A invalid').refusal).toContain('hexadecimal')
    expect(parse('GROUP RACK A').refusal).toContain('at least two')
  })
  it('folds a point pair into one slot and keeps every other field its own', () => {
    expect(promptSlots(PROMPTS.createLine).map((s) => s.kind)).toEqual(['point', 'point', 'text'])
    expect(promptSlots(PROMPTS.createCircle).map((s) => [s.kind, s.keys.join(',')])).toEqual([['point', 'x,y'], ['number', 'r'], ['text', 'layer']])
    expect(promptSlots(PROMPTS.createPolyline).map((s) => s.kind)).toEqual(['text', 'checkbox', 'text'])
    expect(promptSlots(PROMPTS.move).map((s) => s.keys)).toEqual([['dx', 'dy']])
    // FILLET: radius, then the edge id and the point on it, then the point on the first line.
    expect(promptSlots(PROMPTS.fillet).map((s) => [s.kind, s.keys.join(',')])).toEqual([['number', 'r'], ['edge', 'edge'], ['point', 'ex,ey'], ['point', 'x,y']])
    expect(promptSlots(PROMPTS.arrayRect).map((s) => s.kind)).toEqual(['number', 'number', 'number', 'number'])
    // W4g-7b-02c: INSERT's name, point, then its scale and rotation defaults.
    expect(promptSlots(PROMPTS.createInsert).map((s) => [s.kind, s.keys.join(',')])).toEqual([
      ['text', 'name'], ['point', 'x,y'], ['number', 'sx'], ['number', 'sy'], ['number', 'rot'], ['text', 'layer'],
    ])
    // W4g-7b-04c: DIMLINEAR's three points, then its own rotation, style, layer;
    // DIMALIGNED the same without the rotation slot.
    expect(promptSlots(PROMPTS.dimLinear).map((s) => [s.kind, s.keys.join(',')])).toEqual([
      ['point', 'x,y'], ['point', 'x2,y2'], ['point', 'dx,dy'], ['number', 'rot'], ['text', 'style'], ['text', 'layer'],
    ])
    expect(promptSlots(PROMPTS.dimAligned).map((s) => [s.kind, s.keys.join(',')])).toEqual([
      ['point', 'x,y'], ['point', 'x2,y2'], ['point', 'dx,dy'], ['text', 'style'], ['text', 'layer'],
    ])
  })
})

describe('W4g-7b-04c: a scripted DIMLINEAR/DIMALIGNED', () => {
  it('dal takes three points then style/layer; dli additionally takes a rotation', () => {
    expect(parse('dal 0,0 3,4 1.5,6').lines[0]).toMatchObject({
      op: 'dimAligned', verb: 'DIMALIGNED', inputs: { x: '0,0', y: '', x2: '3,4', y2: '', dx: '1.5,6', dy: '' },
    })
    expect(parse('dli 0,0 3,4 1.5,6 0').lines[0]).toMatchObject({
      op: 'dimLinear', verb: 'DIMLINEAR', inputs: { x: '0,0', y: '', x2: '3,4', y2: '', dx: '1.5,6', dy: '', rot: '0' },
    })
    expect(parse('dimlinear 0,0 3,4 1.5,6 90 Standard L1').lines[0].inputs).toEqual({
      x: '0,0', y: '', x2: '3,4', y2: '', dx: '1.5,6', dy: '', rot: '90', style: 'Standard', layer: 'L1',
    })
  })
})

describe('W4g-7b-02c: a scripted INSERT', () => {
  it('reads the name, point, scale and rotation in the prompt\'s order; an omitted operand keeps its default', () => {
    expect(parse('insert Fixture 10,20 2 3 90').lines[0].inputs).toEqual({ name: 'Fixture', x: '10,20', y: '', sx: '2', sy: '3', rot: '90' })
    expect(parse('insert Fixture 10,20 2 3 90').lines[0].verb).toBe('INSERT')
    // A quoted name may carry spaces; everything after it keeps the prompt's default.
    expect(parse('insert "My Block" 10,20').lines[0].inputs).toEqual({ name: 'My Block', x: '10,20', y: '' })
    // The one-letter form.
    expect(parse('i Fixture 10,20').lines[0].op).toBe('createInsert')
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

  // W4g-7c: every former deferred word is live (LEADER / MLEADER in 3c, BLOCK in
  // 2c), so a script line with one of them parses as a draw command and a
  // missing operand refuses by operand, never as an unknown word.
  it('LEADER and BLOCK parse as live commands, refusing by operand and never as unknown words', () => {
    const out = parse('line 0,0 3,4\nmleader 0,0 5,4 "Valve"')
    expect(out.refusal).toBeUndefined()
    expect(out.lines.map((l) => [l.line, l.group, l.op, l.verb])).toEqual([
      [1, 'draw', 'createLine', 'LINE'],
      [2, 'draw', 'createMleader', 'MLEADER'],
    ])
    expect(out.lines[1].reason).toBeUndefined()
    expect(parse('block').refusal).toMatch(/^line 1: /)
    expect(parse('block').refusal).not.toMatch(/not a command word/)
    expect(parse('leader').lines[0]).toMatchObject({ group: 'draw', op: 'createMleader', verb: 'MLEADER' })
  })

  it('is bounded before any line is read', () => {
    expect(parse('x'.repeat(MAX_SCRIPT_CHARS + 1)).refusal).toMatch(/longer than/)
    expect(parse(Array.from({ length: MAX_SCRIPT_LINES + 1 }, () => 'e').join('\n')).refusal).toMatch(/more than 5000 lines/)
    expect(parse(Array.from({ length: MAX_SCRIPT_LINES }, () => 'e').join('\n')).lines).toHaveLength(MAX_SCRIPT_LINES)
    // A file's trailing line terminator is not a line: 5000 lines plus it still parse.
    expect(parse(Array.from({ length: MAX_SCRIPT_LINES }, () => 'e').join('\r\n') + '\r\n').lines).toHaveLength(MAX_SCRIPT_LINES)
    expect(parse('line 0,0 1,1\n').lines).toHaveLength(1)
    expect(parse(42).refusal).toBe('the script is not text')
    expect(parse('').lines).toEqual([])
  })
})
