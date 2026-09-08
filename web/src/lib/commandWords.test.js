import { describe, expect, it } from 'vitest'

import { DEFERRED_REASONS } from './actionRegistry.js'
import { COMMAND_WORDS, MAX_COMMAND_CHARS, parseDrawingCommand } from './commandWords.js'

describe('commandWords (W4f slice B): typed CAD words on the command line', () => {
  it('matches the reference vocabulary and its aliases, case-insensitively, with the optional > marker', () => {
    expect(parseDrawingCommand('line')).toMatchObject({ group: 'draw', op: 'createLine', verb: 'LINE', word: 'line' })
    expect(parseDrawingCommand('L')).toMatchObject({ op: 'createLine' })
    expect(parseDrawingCommand('  >CIRCLE ')).toMatchObject({ op: 'createCircle', verb: 'CIRCLE', word: 'CIRCLE' })
    expect(parseDrawingCommand('> c')).toMatchObject({ op: 'createCircle' })
    expect(parseDrawingCommand('pl')).toMatchObject({ op: 'createPolyline' })
    expect(parseDrawingCommand('Arc')).toMatchObject({ op: 'createArc', group: 'draw' })
    expect(parseDrawingCommand('m')).toMatchObject({ group: 'modify', op: 'move', verb: 'MOVE' })
    expect(parseDrawingCommand('erase')).toMatchObject({ group: 'modify', op: 'delete', verb: 'ERASE' })
    expect(parseDrawingCommand('DEL')).toMatchObject({ op: 'delete' })
    expect(parseDrawingCommand('u')).toMatchObject({ group: 'modify', op: 'undo', verb: 'UNDO' })
    expect(parseDrawingCommand('UNDO')).toMatchObject({ op: 'undo' })
    expect(parseDrawingCommand('redo')).toMatchObject({ group: 'modify', op: 'redo', verb: 'REDO' })
    expect(Object.isFrozen(parseDrawingCommand('line'))).toBe(true)
  })

  it('W4g-5: offset and its one-letter form arm the OFFSET prompt', () => {
    expect(parseDrawingCommand('offset')).toMatchObject({ group: 'modify', op: 'offset', verb: 'OFFSET', word: 'offset' })
    expect(parseDrawingCommand('o')).toMatchObject({ group: 'modify', op: 'offset', verb: 'OFFSET', word: 'o' })
    expect(parseDrawingCommand('  OFFSET  ')).toMatchObject({ group: 'modify', op: 'offset', verb: 'OFFSET', word: 'OFFSET' })
  })

  it('never claims a sentence, a slash tool, an empty or oversized text, or an unknown word', () => {
    expect(parseDrawingCommand('draw a line from the inverter to the panel')).toBeNull()
    expect(parseDrawingCommand('line 0,0 100,0')).toBeNull()
    expect(parseDrawingCommand('/count-by-layer')).toBeNull()
    expect(parseDrawingCommand('')).toBeNull()
    expect(parseDrawingCommand('   ')).toBeNull()
    expect(parseDrawingCommand('>')).toBeNull()
    // W4g-4 made `rectangle` a real word (RECTANG); `triangle` is the unknown one now.
    expect(parseDrawingCommand('triangle')).toBeNull()
    // W4g-5 took `o` and `offset`; `offsets` is still nobody's word.
    expect(parseDrawingCommand('offsets')).toBeNull()
    expect(parseDrawingCommand('lines')).toBeNull()
    expect(parseDrawingCommand(null)).toBeNull()
    expect(parseDrawingCommand(42)).toBeNull()
    expect(parseDrawingCommand('l'.repeat(MAX_COMMAND_CHARS + 1))).toBeNull()
  })

  it('W4g-7b-02c: insert and its one-letter form arm the INSERT prompt', () => {
    expect(parseDrawingCommand('insert')).toMatchObject({ group: 'draw', op: 'createInsert', verb: 'INSERT', word: 'insert' })
    expect(parseDrawingCommand('i')).toMatchObject({ group: 'draw', op: 'createInsert', verb: 'INSERT', word: 'i' })
    expect(parseDrawingCommand('INSERT')).toMatchObject({ op: 'createInsert' })
  })

  it('W4g-7b-03c: colour, linetype and lineweight, and their one/two-letter forms', () => {
    expect(parseDrawingCommand('color')).toMatchObject({ group: 'modify', op: 'setColor', verb: 'COLOR', word: 'color' })
    expect(parseDrawingCommand('col')).toMatchObject({ op: 'setColor' })
    expect(parseDrawingCommand('linetype')).toMatchObject({ group: 'modify', op: 'setLinetype', verb: 'LINETYPE', word: 'linetype' })
    expect(parseDrawingCommand('lt')).toMatchObject({ op: 'setLinetype' })
    expect(parseDrawingCommand('lweight')).toMatchObject({ group: 'modify', op: 'setLineweight', verb: 'LWEIGHT', word: 'lweight' })
    expect(parseDrawingCommand('lw')).toMatchObject({ op: 'setLineweight' })
  })

  it('W4g-7b-04c: DIMLINEAR/DIMALIGNED and their one-word forms DLI/DAL', () => {
    expect(parseDrawingCommand('dimlinear')).toMatchObject({ group: 'draw', op: 'dimLinear', verb: 'DIMLINEAR', word: 'dimlinear' })
    expect(parseDrawingCommand('dli')).toMatchObject({ group: 'draw', op: 'dimLinear', verb: 'DIMLINEAR', word: 'dli' })
    expect(parseDrawingCommand('dimaligned')).toMatchObject({ group: 'draw', op: 'dimAligned', verb: 'DIMALIGNED', word: 'dimaligned' })
    expect(parseDrawingCommand('dal')).toMatchObject({ group: 'draw', op: 'dimAligned', verb: 'DIMALIGNED', word: 'dal' })
  })

  it('exposes the word list for the help surface, every entry parseable', () => {
    expect(COMMAND_WORDS.length).toBeGreaterThan(10)
    for (const word of COMMAND_WORDS) expect(parseDrawingCommand(word)).not.toBeNull()
  })

  // W4g-7b-05c: LEADER/LE, BLOCK/B, GROUP/G, UNGROUP are real command words —
  // never null — but parse to the 'deferred' group with the control's own
  // frozen reason, never an armable op.
  it('parses the four deferred controls to group "deferred" with their exact frozen reason', () => {
    expect(parseDrawingCommand('leader')).toMatchObject({ group: 'deferred', op: 'leader', verb: 'LEADER', word: 'leader', reason: DEFERRED_REASONS.leader })
    expect(parseDrawingCommand('LE')).toMatchObject({ group: 'deferred', op: 'leader', verb: 'LEADER', word: 'LE', reason: DEFERRED_REASONS.leader })
    expect(parseDrawingCommand('block')).toMatchObject({ group: 'draw', op: 'createBlock', verb: 'BLOCK', word: 'block' })
    expect(parseDrawingCommand('b')).toMatchObject({ group: 'draw', op: 'createBlock', verb: 'BLOCK', word: 'b' })
    expect(parseDrawingCommand('group')).toMatchObject({ group: 'groups', op: 'group', verb: 'GROUP', word: 'group' })
    expect(parseDrawingCommand('g')).toMatchObject({ group: 'groups', op: 'group', verb: 'GROUP', word: 'g' })
    expect(parseDrawingCommand('ungroup')).toMatchObject({ group: 'groups', op: 'ungroup', verb: 'UNGROUP', word: 'ungroup' })
    expect(Object.isFrozen(parseDrawingCommand('leader'))).toBe(true)
    // Every existing word is unchanged: b and g named no prior word.
    expect(parseDrawingCommand('line')).toMatchObject({ group: 'draw', op: 'createLine' })
  })
})
