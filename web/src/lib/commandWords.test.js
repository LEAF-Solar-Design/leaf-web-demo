import { describe, expect, it } from 'vitest'

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

  it('never claims a sentence, a slash tool, an empty or oversized text, or an unknown word', () => {
    expect(parseDrawingCommand('draw a line from the inverter to the panel')).toBeNull()
    expect(parseDrawingCommand('line 0,0 100,0')).toBeNull()
    expect(parseDrawingCommand('/count-by-layer')).toBeNull()
    expect(parseDrawingCommand('')).toBeNull()
    expect(parseDrawingCommand('   ')).toBeNull()
    expect(parseDrawingCommand('>')).toBeNull()
    expect(parseDrawingCommand('rectangle')).toBeNull()
    expect(parseDrawingCommand('lines')).toBeNull()
    expect(parseDrawingCommand(null)).toBeNull()
    expect(parseDrawingCommand(42)).toBeNull()
    expect(parseDrawingCommand('l'.repeat(MAX_COMMAND_CHARS + 1))).toBeNull()
  })

  it('exposes the word list for the help surface, every entry parseable', () => {
    expect(COMMAND_WORDS.length).toBeGreaterThan(10)
    for (const word of COMMAND_WORDS) expect(parseDrawingCommand(word)).not.toBeNull()
  })
})
