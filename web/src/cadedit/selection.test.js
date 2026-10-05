import { describe, expect, it } from 'vitest'
import { ACTIONS } from '../lib/actionRegistry.js'
import {
  NO_IDS, withSelection, surviveSelectionIds, addId, toggleId, replaceIds, EDIT_OP_LABELS, multiSelectionRefusal,
  rangeIds, isToggleKey, isTypingTarget, clickIntent, LONG_PRESS_MS,
} from './selection.js'

const entities = [{ id: '7' }, { id: '9' }, { id: '11' }]

describe('SSD1-24A pure selection', () => {
  it('derives only singleton identity and freezes the ids', () => {
    expect(Object.isFrozen(NO_IDS)).toBe(true)
    for (const [ids, selectedId] of [[[], ''], [['7'], '7'], [['7', '9'], '']]) {
      const selection = withSelection(ids)
      expect(selection.selectedId).toBe(selectedId)
      expect(Object.isFrozen(selection.selectedIds)).toBe(true)
    }
  })
  it('deduplicates strings in first occurrence order without mutating input', () => {
    const ids = ['9', '7', '9', 7, null, '11']
    expect(withSelection(ids).selectedIds).toEqual(['9', '7', '11'])
    expect(ids).toEqual(['9', '7', '9', 7, null, '11'])
  })
  it('keeps only survivors in order and replaces with valid unique ids', () => {
    expect(surviveSelectionIds(['11', '999', '7', 9], entities)).toEqual(['11', '7'])
    expect(replaceIds(['9', '9', '999', null, '7'], entities)).toEqual(['9', '7'])
  })
  it('adds valid ids and ignores duplicate, unknown and non-string ids', () => {
    const ids = Object.freeze(['7'])
    expect(addId(ids, '9', entities)).toEqual(['7', '9'])
    for (const id of ['7', '999', 7, null]) expect(addId(ids, id, entities)).toBe(ids)
  })
  it('toggles members and ignores invalid ids', () => {
    const ids = Object.freeze(['7', '9'])
    expect(toggleId(ids, '7', entities)).toEqual(['9'])
    expect(toggleId(ids, '11', entities)).toEqual(['7', '9', '11'])
    for (const id of ['999', 7, null]) expect(toggleId(ids, id, entities)).toBe(ids)
  })
  it('SSD1-24A-b adding a valid id drops a ghost', () => {
    expect(addId(['missing'], '7', entities)).toEqual(['7'])
  })
  it('SSD1-24A-b toggling a valid id in drops a ghost', () => {
    expect(toggleId(['missing'], '7', entities)).toEqual(['7'])
  })
  it('SSD1-24A-b toggling a valid id out drops a ghost', () => {
    expect(toggleId(['missing', '7'], '7', entities)).toEqual([])
  })
  it('SSD1-24A-b adding an existing id drops a ghost', () => {
    expect(addId(['missing', '7'], '7', entities)).toEqual(['7'])
  })
  it('SSD1-24A-b adding an unknown id still drops a ghost', () => {
    expect(addId(['missing', '7'], 'unknown', entities)).toEqual(['7'])
  })
  it('SSD1-24A-b no-ops preserve an all-valid array identity', () => {
    const ids = Object.freeze(['7', '9'])
    for (const id of ['7', 'unknown', 7, null]) expect(addId(ids, id, entities)).toBe(ids)
    for (const id of ['unknown', 7, null]) expect(toggleId(ids, id, entities)).toBe(ids)
  })
  it('matches every canonical Modify and Cut/Copy registry display label', () => {
    const records = ACTIONS.filter((action) => action.id.startsWith('modify:') || ['clipboard:cutClip', 'clipboard:copyClip'].includes(action.id))
    expect(records.length).toBeGreaterThan(20)
    for (const action of records) expect(EDIT_OP_LABELS[action.op]).toBe(action.text)
    expect(Object.isFrozen(EDIT_OP_LABELS)).toBe(true)
  })
  it('names the refused operation and falls back for unknown operations', () => {
    expect(multiSelectionRefusal('move', 2)).toBe('Move needs one object; 2 are selected.')
    expect(multiSelectionRefusal('unknown', 3)).toBe('This change needs one object; 3 are selected.')
  })
})

describe('S7 selection without a checkbox column', () => {
  const rows = [{ id: '7' }, { id: '9' }, { id: '10', editable: false }, { id: '11' }, { id: '13' }]
  it('S7-range selects anchor to target inclusive in list order, either direction, skipping read-only rows', () => {
    expect(rangeIds(rows, '9', '13')).toEqual(['9', '11', '13'])
    expect(rangeIds(rows, '13', '9')).toEqual(['9', '11', '13'])
    expect(rangeIds(rows, '7', '7')).toEqual(['7'])
    expect(Object.isFrozen(rangeIds(rows, '7', '11'))).toBe(true)
  })
  it('S7-range collapses to the target without an anchor and yields nothing without a target', () => {
    for (const anchor of [null, undefined, 'gone']) expect(rangeIds(rows, anchor, '11')).toEqual(['11'])
    expect(rangeIds(rows, '7', 'gone')).toBe(NO_IDS)
    expect(rangeIds(null, '7', '9')).toBe(NO_IDS)
    expect(rangeIds(rows, '7', '10')).toEqual(['7', '9'])
  })
  it('S7-range matches BigInt and decimal ids by identity and keeps their type', () => {
    const big = [{ id: 9007199254740992n }, { id: 9007199254740993n }, { id: 9007199254740994n }]
    expect(rangeIds(big, '9007199254740992', 9007199254740993n)).toEqual([9007199254740992n, 9007199254740993n])
  })
  it('S7-x-toggle accepts a bare x or X on a row control and refuses chords, repeats, composition and typing', () => {
    const radio = Object.assign(document.createElement('input'), { type: 'radio' })
    const button = document.createElement('button')
    expect(isToggleKey({ key: 'x', target: radio })).toBe(true)
    expect(isToggleKey({ key: 'X', shiftKey: true, target: button })).toBe(true)
    for (const extra of [{ ctrlKey: true }, { metaKey: true }, { altKey: true }, { repeat: true }, { isComposing: true }]) {
      expect(isToggleKey({ key: 'x', target: radio, ...extra })).toBe(false)
    }
    expect(isToggleKey({ key: 'c', target: radio })).toBe(false)
    expect(isToggleKey(null)).toBe(false)
    const text = document.createElement('input'), search = Object.assign(document.createElement('input'), { type: 'search' })
    const editable = document.createElement('div')
    editable.contentEditable = 'true'
    Object.defineProperty(editable, 'isContentEditable', { value: true })
    for (const target of [text, search, document.createElement('textarea'), document.createElement('select'), editable]) {
      expect(isTypingTarget(target)).toBe(true)
      expect(isToggleKey({ key: 'x', target })).toBe(false)
    }
    expect(isTypingTarget(radio)).toBe(false)
    expect(isTypingTarget(button)).toBe(false)
  })
  it('S7-modifier reads Shift as a range, Ctrl or Cmd as a toggle, both as an added range', () => {
    expect(clickIntent({})).toBeNull()
    expect(clickIntent({ shiftKey: true })).toBe('range')
    expect(clickIntent({ ctrlKey: true })).toBe('toggle')
    expect(clickIntent({ metaKey: true })).toBe('toggle')
    expect(clickIntent({ shiftKey: true, metaKey: true })).toBe('rangeAdd')
    expect(clickIntent(null)).toBeNull()
  })
  it('S7-long-press holds for 500 ms', () => {
    expect(LONG_PRESS_MS).toBe(500)
  })
})
