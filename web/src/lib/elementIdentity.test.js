// @vitest-environment jsdom
import { describe, expect, it } from 'vitest'

import {
  ELEMENT_KINDS, MAX_ELEMENT_ID_CHARS, closestElementIdentity,
  formatElementId, isValidElementKind, parseElementId,
} from './elementIdentity.js'

describe('elementIdentity', () => {
  it('freezes the exact kind vocabulary this slice uses', () => {
    expect(ELEMENT_KINDS).toEqual([
      'tool', 'version', 'job', 'family', 'rung', 'turn', 'approval', 'item', 'entity',
    ])
    expect(Object.isFrozen(ELEMENT_KINDS)).toBe(true)
  })

  it('formats one id per render-site table entry', () => {
    // A structured registry id (`<group>:<op>`) nests as the id half intact.
    expect(formatElementId('tool', 'draw:createLine')).toBe('tool:draw:createLine')
    expect(parseElementId('tool:draw:createLine')).toEqual({ kind: 'tool', id: 'draw:createLine' })
    expect(formatElementId('tool', 'author-tool')).toBe('tool:author-tool')
    expect(formatElementId('version', 'v-42')).toBe('version:v-42')
    expect(formatElementId('job', 'job-abc123')).toBe('job:job-abc123')
    expect(formatElementId('family', 'stringing')).toBe('family:stringing')
    expect(formatElementId('rung', 'revision')).toBe('rung:revision')
    expect(formatElementId('turn', 'turn-1')).toBe('turn:turn-1')
    expect(formatElementId('approval', 'confirm-9')).toBe('approval:confirm-9')
    expect(formatElementId('item', 'item-7')).toBe('item:item-7')
    expect(formatElementId('entity', 'AB12CD')).toBe('entity:AB12CD')
  })

  it('round-trips every well-formed id through parseElementId', () => {
    for (const kind of ELEMENT_KINDS) {
      const formatted = formatElementId(kind, 'ok-id.1')
      expect(formatted).toBe(`${kind}:ok-id.1`)
      expect(parseElementId(formatted)).toEqual({ kind, id: 'ok-id.1' })
    }
  })

  describe('fails closed on malformed input', () => {
    it('rejects an unregistered kind', () => {
      expect(formatElementId('mushy', 'x')).toBeNull()
      expect(parseElementId('mushy:x')).toBeNull()
    })
    it('rejects an empty id', () => {
      expect(formatElementId('tool', '')).toBeNull()
      expect(parseElementId('tool:')).toBeNull()
    })
    it('rejects a value with no colon', () => {
      expect(parseElementId('tool-x')).toBeNull()
    })
    it('rejects a value that opens with a colon (empty kind)', () => {
      expect(parseElementId(':x')).toBeNull()
    })
    it('rejects an id over the bounded char ceiling', () => {
      const long = 'a'.repeat(MAX_ELEMENT_ID_CHARS + 1)
      expect(formatElementId('tool', long)).toBeNull()
      expect(parseElementId(`tool:${long}`)).toBeNull()
    })
    it('accepts an id at the exact ceiling', () => {
      const max = 'a'.repeat(MAX_ELEMENT_ID_CHARS)
      expect(formatElementId('tool', max)).toBe(`tool:${max}`)
    })
    it('rejects a charset violation (slash, quote, space, angle brackets)', () => {
      for (const bad of ['a/b', 'a"b', 'a b', 'a<b>']) {
        expect(formatElementId('tool', bad)).toBeNull()
      }
    })
    it('rejects an id that does not open with an alphanumeric', () => {
      expect(formatElementId('tool', '-x')).toBeNull()
      expect(formatElementId('tool', '.x')).toBeNull()
    })
    it('never throws on non-string input', () => {
      expect(formatElementId(null, undefined)).toBeNull()
      expect(parseElementId(null)).toBeNull()
      expect(parseElementId(42)).toBeNull()
      expect(parseElementId('')).toBeNull()
    })
    it('isValidElementKind agrees with the format/parse gate', () => {
      expect(isValidElementKind('tool')).toBe(true)
      expect(isValidElementKind('entity')).toBe(true)
      expect(isValidElementKind('mushy')).toBe(false)
      expect(isValidElementKind(null)).toBe(false)
    })
  })

  describe('closestElementIdentity: the ContextMenu delegation lookup', () => {
    it('finds the nearest ancestor carrying a well-formed id', () => {
      document.body.innerHTML = '<div data-element-id="tool:author-tool"><span id="leaf">x</span></div>'
      const leaf = document.getElementById('leaf')
      expect(closestElementIdentity(leaf)).toMatchObject({ kind: 'tool', id: 'author-tool' })
    })
    it('returns null when no ancestor carries the attribute', () => {
      document.body.innerHTML = '<div><span id="leaf">x</span></div>'
      expect(closestElementIdentity(document.getElementById('leaf'))).toBeNull()
    })
    it('returns null on a malformed attribute rather than a partial match', () => {
      document.body.innerHTML = '<div data-element-id="mushy:x"><span id="leaf">x</span></div>'
      expect(closestElementIdentity(document.getElementById('leaf'))).toBeNull()
    })
    it('returns null on a non-element target', () => {
      expect(closestElementIdentity(null)).toBeNull()
      expect(closestElementIdentity({})).toBeNull()
    })
  })

})
