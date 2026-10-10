// The action record, proved four ways (standardization slice 10a).
//
// 1. THE REGISTRY holds up: unique ids, bounded strings, a surface, a total
//    `when` that only ever names a registered reason, and triggers that agree
//    with the `kbd` cap.
// 2. EVERY DECLARED TRIGGER IS REAL. A record that says mouse:'click' has a
//    run its renderer wires to onClick; one that says keyboard:'kbd' carries a
//    cap the ladder actually fires; one that says touch:'tap' is a plain click
//    target (a <button>, or the "/" picker's div[role=option] row), so the tap
//    and the click are the same handler. Nothing claims a trigger it does not
//    have: that is the whole point of the slice.
// 3. THE LADDER TABLE EQUALS THE OLD LADDER. `OLD_LADDER` below is App.jsx's
//    if/else chain as it stood before this slice, copied as literals, and the
//    registry's pure `ladderDecision` is asserted to agree with it across a
//    matrix of key events and shell states. If the two ever disagree, this
//    test names the case.
// 4. THE ACCESSIBLE-NAME COMPOSER reproduces the strings draftingRibbon.test.jsx
//    and engineSessionProvider.test.jsx pin, byte for byte.
import { describe, expect, it, vi } from 'vitest'
import { createElement } from 'react'
import { fireEvent, render } from '@testing-library/react'

import {
  ACTIONS,
  CLIPBOARD_REASONS,
  DEFERRED_REASONS,
  DRAW_REASONS,
  ESCAPE_RUNGS,
  INTERACTIVE_TARGET_SELECTOR,
  KNOWN_REASON_VALUES,
  LADDER_REASONS,
  MODIFY_REASONS,
  PROPERTY_REASONS,
  PLACED_KINDS,
  MAX_ID_CHARS,
  MAX_LABEL_CHARS,
  REASONS,
  REPEAT_REASONS,
  ENGINE_SHORTCUTS,
  KBD_CONTEXTS,
  VERSION_SHORTCUTS,
  RETRY_RUNGS,
  SURFACES,
  accessibleName,
  reasonCode,
  byId,
  drawReason,
  engineDocumentVisible,
  engineShortcutDecision,
  historyStepReason,
  escapeRung,
  forCluster,
  forGroup,
  forSurface,
  keyboardTable,
  ladderDecision,
  ladderListener,
  modifyReason,
  modifyOpReason,
  propertyReason,
  propertyControlReason,
  retryRung,
  ribbonTool,
  slashCommandHandlers,
  slashStaticEntries,
  validateRegistry,
  versionShortcutDecision,
} from './actionRegistry.js'
import { SINGLE_KEY_SHORTCUTS_KEY, writeSingleKeyShortcuts } from './singleKeyPreference.js'
import useEscapeOwner from './useEscapeOwner.js'

// --- 1: the registry holds up ---------------------------------------------

describe('stable local reason codes', () => {
  const maps = { REASONS, DRAW_REASONS, MODIFY_REASONS, PROPERTY_REASONS, CLIPBOARD_REASONS, DEFERRED_REASONS, LADDER_REASONS, REPEAT_REASONS }
  it('resolves every known sentence back through its map and key', () => {
    for (const sentence of KNOWN_REASON_VALUES) {
      const code = reasonCode(sentence)
      expect(code).toMatch(/^[A-Z_]+\.[A-Za-z][A-Za-z0-9]*$/)
      const [name, key] = code.split('.')
      expect(maps[name][key]).toBe(sentence)
    }
    for (const [name, map] of Object.entries(maps)) {
      for (const [key, sentence] of Object.entries(map)) {
        const first = Object.entries(maps).find(([, values]) => Object.values(values).includes(sentence))
        expect(reasonCode(sentence)).toBe(`${first[0]}.${Object.keys(first[1]).find((k) => first[1][k] === sentence)}`)
        expect(maps[name][key]).toBe(sentence)
      }
    }
    expect(reasonCode(REASONS['unsavedEngineEdits'])).toBe('REASONS.unsavedEngineEdits')
  })
  it.each(['', 'not a reason', undefined])('returns no code for %s', (sentence) => {
    expect(reasonCode(sentence)).toBe('')
  })
})

describe('the registry', () => {
  it('explains Polyline commands and disabled property controls in the reason vocabulary', () => {
    expect(byId('draw:createPolyline').title({})).toContain('PLINE')
    expect(byId('draw:createPolyline').title({})).toMatch(/\bPL\b/)
    for (const op of ['matchprop', 'setColor', 'setLinetype', 'setLineweight']) {
      for (const [session, key] of [
        [null, 'noDocument'],
        [{ errorKind: 'crashed' }, 'crashed'],
        [{ engineParsed: true, busy: true }, 'busy'],
        [{ engineParsed: true }, 'noSelection'],
        [{ engineParsed: true, selected: { editable: false, type: 'DIMENSION' } }, 'readOnlyKind'],
      ]) {
        expect(propertyControlReason(session)).toBe(PROPERTY_REASONS[key])
        const reason = byId(`modify:${op}`).when({ session })
        expect(reason).toBe(PROPERTY_REASONS[key])
        expect(KNOWN_REASON_VALUES.has(reason)).toBe(true)
        expect(reason).toContain('Match copies')
        expect(reason).toContain('ByLayer inherits layer properties')
      }
      const editable = { engineParsed: true, selected: { editable: true } }
      expect(propertyControlReason(editable)).toBe('')
      expect(byId(`modify:${op}`).when({ session: editable })).toBe('')
    }
    expect(byId('modify:matchprop').when({ session: { engineParsed: true } })).toContain('select a source object first')
  })

  it('is frozen, and so is every record and every trigger table inside it', () => {
    expect(Object.isFrozen(ACTIONS)).toBe(true)
    for (const action of ACTIONS) {
      expect(Object.isFrozen(action)).toBe(true)
      expect(Object.isFrozen(action.triggers)).toBe(true)
    }
  })

  it('gives every action a unique, bounded id and a known surface', () => {
    const ids = ACTIONS.map((a) => a.id)
    expect(new Set(ids).size).toBe(ids.length)
    for (const action of ACTIONS) {
      expect(action.id.length).toBeGreaterThan(0)
      expect(action.id.length).toBeLessThanOrEqual(MAX_ID_CHARS)
      expect(action.label.length).toBeLessThanOrEqual(MAX_LABEL_CHARS)
      expect(SURFACES).toContain(action.surface)
    }
  })

  it('covers all four surfaces, and byId / forSurface agree with the list', () => {
    for (const surface of SURFACES) expect(forSurface(surface).length).toBeGreaterThan(0)
    expect(forSurface('ribbon').concat(forSurface('engine'), forSurface('slash'), forSurface('bar')))
      .toHaveLength(ACTIONS.length)
    for (const action of ACTIONS) expect(byId(action.id)).toBe(action)
    expect(byId('no-such-action')).toBeNull()
  })

  // The gate this slice exists to hold: a reason a renderer would show must be
  // one of the frozen maps, never free text a reader cannot act on.
  it('never names a reason outside the registered vocabulary, in any context', () => {
    const probes = [
      {},
      { hasDrawing: true, entitled: true, available: true, hasVersions: true, canUndo: true, canRedo: true },
      { hasVersions: true, versionBusy: true },
      { hasVersions: true, running: true },
      { hasVersions: true, previewing: true },
      { hasVersions: true, mutationsBlocked: true },
      { entitled: true, available: false },
      { session: null },
      { session: { engineParsed: true } },
      { session: { engineParsed: true, busy: true } },
      { session: { engineParsed: true, selected: { editable: false } } },
      { session: { engineParsed: true, selected: { editable: true } } },
      { session: { errorKind: 'crashed' } },
      { drawer: 'tools' },
      { rTarget: 'route' },
      { rTarget: 'result' },
    ]
    for (const action of ACTIONS) {
      for (const ctx of probes) {
        const why = action.when(ctx)
        expect(typeof why).toBe('string')
        if (why) expect(KNOWN_REASON_VALUES.has(why)).toBe(true)
        if (!action.gated) expect(why).toBe('')
      }
    }
  })

  it('refuses a malformed record at load rather than rendering it', () => {
    const good = ACTIONS[0]
    const bad = (patch) => () => validateRegistry([{ ...good, ...patch }])
    expect(bad({ id: '' })).toThrow(/bad id/)
    expect(bad({ label: '' })).toThrow(/bounded label/)
    expect(bad({ surface: 'nowhere' })).toThrow(/surface/)
    expect(bad({ when: null })).toThrow(/when/)
    expect(bad({ run: null })).toThrow(/run/)
    expect(bad({ triggers: { mouse: 'hover', keyboard: null, touch: null } })).toThrow(/mouse trigger/)
    // A cap nobody bound, and a key nobody can find: both are lies.
    expect(bad({ kbd: 'Q', triggers: { mouse: 'click', keyboard: null, touch: 'tap' } }))
      .toThrow(/kbd and keyboard trigger disagree/)
    // An unregistered sentence is exactly the greyed-with-no-reason control.
    expect(bad({ gated: true, when: () => 'nope' })).toThrow(/unregistered reason/)
    expect(bad({ gated: false, when: () => REASONS.noDrawing })).toThrow(/ungated but/)
    expect(() => validateRegistry([good, { ...good }])).toThrow(/duplicate id/)
  })
})

// --- 2: every declared trigger is real ------------------------------------

describe('honest triggers', () => {
  it('reaches every action by every trigger it declares, and by no other', () => {
    const caps = keyboardTable().map((row) => row.id)
    for (const action of ACTIONS) {
      const { mouse, keyboard, touch } = action.triggers

      // mouse:'click' — the renderer wires `run` to onClick, so calling run is
      // the click. Proved by observing the ctx handler the record names.
      if (mouse === 'click') {
        const seen = []
        action.run(handlerProbe(seen))
        expect(seen.length).toBe(1)
      } else {
        expect(mouse).toBeNull()
      }

      // touch:'tap': a tap IS a click on a plain click target (a <button>, or
      // the "/" picker's div[role=option] row): the SAME handler, no bespoke
      // touch path. 'pointer' belongs to CanvasPointPicker alone, which
      // registers no action, so no record may claim it.
      if (touch === 'tap') expect(mouse).toBe('click')
      else expect(touch).toBeNull()

      // keyboard:'kbd' — the cap is bound in the ladder and fires this id.
      if (keyboard === 'kbd') {
        expect(action.kbd).not.toBeNull()
        expect(caps).toContain(action.id)
      } else {
        expect(action.kbd).toBeNull()
      }

      // keyboard:'enter' — the "/" picker's Enter picks the highlighted row,
      // which runs the record's clientAction handler.
      if (keyboard === 'enter') expect(action.surface).toBe('slash')
    }
  })

  it('B2-34 advertises exactly the four shell, seven engine and two version shortcut rows', () => {
    expect(keyboardTable().filter((row) => row.surface === 'bar')).toEqual([
      { id: 'bar:focus', label: 'Command bar', kbd: 'Mod+K', surface: 'bar', context: KBD_CONTEXTS.anywhere, singleKey: false },
      { id: 'bar:escape', label: 'Close the topmost surface', kbd: 'Escape', surface: 'bar', context: KBD_CONTEXTS.anywhere, singleKey: false },
      { id: 'bar:retry', label: 'Retry the failed step', kbd: 'R', surface: 'bar', context: KBD_CONTEXTS.outsideFields, singleKey: true },
      // Slice 10b: the shortcut sheet's own cap, generated FROM this table
      // rather than hand-typed a second place.
      { id: 'bar:shortcuts', label: 'Keyboard shortcuts', kbd: 'Shift+?', surface: 'bar', context: KBD_CONTEXTS.outsideFields, singleKey: true },
    ])
    const byActionId = ([a], [b]) => a.localeCompare(b)
    expect(keyboardTable().filter((row) => row.surface === 'engine').map(({ id, kbd }) => [id, kbd]).sort(byActionId))
      .toEqual(Object.entries(ENGINE_SHORTCUTS).sort(byActionId))
    expect(keyboardTable().filter((row) => row.surface === 'ribbon').map(({ id, kbd }) => [id, kbd]).sort(byActionId))
      .toEqual(Object.entries(VERSION_SHORTCUTS).sort(byActionId))
    for (const action of ACTIONS) {
      if (action.surface === 'engine') expect(action.kbd).toBe(ENGINE_SHORTCUTS[action.id] || null)
      if (action.surface === 'ribbon') expect(action.kbd).toBe(VERSION_SHORTCUTS[action.id] || null)
      if (action.surface === 'slash') expect(action.kbd).toBeNull()
    }
    // Every printed cap says where it fires, and only R and Shift+? are single keys.
    for (const row of keyboardTable()) expect(Object.values(KBD_CONTEXTS)).toContain(row.context)
    expect(keyboardTable().filter((row) => row.singleKey).map((row) => row.id)).toEqual(['bar:retry', 'bar:shortcuts'])
  })

  // S25, re-read at the w21-b2 merge (17129296, #1865): the four undo / redo
  // rows the sheet prints, by id, each with its cap and where it fires. Two
  // rows share Mod+Z; the context label is what tells them apart.
  it('S25 prints engine and version undo / redo as four rows with their caps and contexts', () => {
    const undoRows = keyboardTable()
      .filter((row) => ['engine:undo', 'engine:redo', 'undo', 'redo'].includes(row.id))
      .map(({ id, kbd, context }) => ({ id, kbd, context }))
    expect(undoRows).toEqual([
      { id: 'engine:undo', kbd: 'Mod+Z', context: 'Drawing canvas' },
      { id: 'engine:redo', kbd: 'Mod+Y / Mod+Shift+Z', context: 'Drawing canvas' },
      { id: 'undo', kbd: 'Mod+Z', context: 'Off the drawing canvas' },
      { id: 'redo', kbd: 'Mod+Shift+Z', context: 'Off the drawing canvas' },
    ])
  })

  it('projects the ribbon clusters and the engine groups the builders seat, in registry order', () => {
    expect(forCluster('view').map((a) => a.id)).toEqual(['fit', 'zoom-in', 'zoom-out', 'properties-pane'])
    expect(forCluster('version').map((a) => a.id)).toEqual(['undo', 'redo', 'history'])
    // W4g-4: RECTANG joined Draw; COPY, MIRROR, ROTATE, SCALE, EXPLODE joined Modify.
    expect(forGroup('draw').map((a) => a.id)).toEqual([
      'draw:createLine', 'draw:createPolyline', 'draw:createCircle', 'draw:createArc', 'draw:createRectangle',
      'solar-panels:createRectangle',
      // W4g-4b: the rest of the reference's small Draw column.
      'draw:createEllipse', 'draw:createPoint',
      // W4g-5d: TEXT is a draw create seated in the Annotation panel.
      'draw:createText',
      // W4g-7b-04c-2: LINEAR / ALIGNED dimensions, seated in the Annotation panel.
      'draw:dimLinear', 'draw:dimAligned',
      'draw:createMleader',
      // W4g-7b-02c: INSERT, seated in the Block panel.
      'draw:createInsert',
      'draw:createBlock',
    ])
    expect(forGroup('modify').map((a) => a.id)).toEqual([
      'solar-panels:arrayRect', 'solar-panels:move', 'solar-panels:rotate',
      'modify:delete', 'modify:move', 'modify:moveVertex',
      'modify:addVertex', 'modify:deleteVertex', 'modify:setLayer',
      // W4g-5: OFFSET joined the row (a parallel copy, computed here and
      // drawn by the engine's own create).
      'modify:copy', 'modify:mirror', 'modify:rotate', 'modify:scale', 'modify:explode', 'modify:offset',
      // W4g-5b: ARRAY, as the reference's two forms, because they take
      // different operands.
      'modify:arrayRect', 'modify:arrayPolar',
      // W4g-6: the intersection verbs, planned in the browser and applied
      // by the engine as one batch.
      'modify:trim', 'modify:extend', 'modify:fillet', 'modify:chamfer',
      // W4g-4b: MATCHPROP, a Modify record seated in the Properties panel.
      'modify:matchprop',
      // W4g-7b-03c: the Properties panel's three combos, Modify records too.
      'modify:setColor', 'modify:setLinetype', 'modify:setLineweight',
    ])
  })

  // A ribbon cluster and an engine group are two fields. A future ribbon
  it('row10 seats four Solar geometry records without changing their arming groups or ladders', () => {
    const solar = ACTIONS.filter((action) => action.panel === 'solar-panels')
    expect(solar.map(({ group, op, text }) => [group, op, text])).toEqual([
      ['draw', 'createRectangle', 'Panel outline'], ['modify', 'arrayRect', 'Panel array'],
      ['modify', 'move', 'Move panel'], ['modify', 'rotate', 'Rotate panel'],
    ])
    for (const action of solar) {
      const original = byId(`${action.group}:${action.op}`)
      for (const session of [null, { errorKind: 'crashed' }, { engineParsed: true, busy: true }, { engineParsed: true }, { engineParsed: true, selected: { editable: true } }]) {
        expect(action.when({ session })).toBe(original.when({ session }))
      }
      const onActivate = vi.fn()
      action.run({ onActivate })
      expect(onActivate).toHaveBeenCalledWith(action.group, action.op)
    }
  })

  // cluster named draw or modify must not merge with the engine group.
  it('never conflates a ribbon cluster with an engine group of the same name', () => {
    expect(forCluster('draw')).toEqual([])
    expect(forCluster('modify')).toEqual([])
    expect(forGroup('view')).toEqual([])
    expect(forGroup('version')).toEqual([])
    expect(forCluster('no-such-cluster')).toEqual([])
    expect(forSurface('no-such-surface')).toEqual([])
  })

  // The paint path (every ribbon build, every engine render) reads these, so
  // a lookup is one Map read that hands back the SAME frozen list each call.
  it('answers forCluster / forGroup / forSurface from a load-time index: the same frozen list every call', () => {
    for (const [select, key] of [[forCluster, 'view'], [forGroup, 'draw'], [forSurface, 'ribbon']]) {
      const first = select(key)
      expect(select(key)).toBe(first)
      expect(Object.isFrozen(first)).toBe(true)
      expect(() => { first.push(first[0]) }).toThrow(TypeError)
    }
    // A miss is the one frozen empty list too, never a fresh allocation.
    expect(forCluster('no-such-cluster')).toBe(forGroup('no-such-group'))
  })

  it('omits disabled and reason for an ungated record, and carries both for a gated one', () => {
    const pane = ribbonTool(byId('properties-pane'), { paneOpen: true })
    expect(pane.disabled).toBeUndefined()
    expect(pane.reason).toBeUndefined()
    expect(pane.title).toBe('Close the properties pane')
    const fit = ribbonTool(byId('fit'), { hasDrawing: false })
    expect(fit.disabled).toBe(true)
    expect(fit.reason).toBe(REASONS.noDrawing)
  })

  it('hands the "/" picker one row and one handler per client command', () => {
    expect(slashStaticEntries()).toEqual([
      { kind: 'command', name: 'mcp', description: 'show mounted MCP servers', client_action: 'mcp' },
    ])
    const onOpenMcp = vi.fn()
    const actions = slashCommandHandlers(['slash:mcp', 'slash:help'], { onOpenMcp, onHelp: () => {} })
    expect(Object.keys(actions).sort()).toEqual(['help', 'mcp'])
    actions.mcp()
    expect(onOpenMcp).toHaveBeenCalledTimes(1)
    // A id that is not a slash record contributes nothing rather than a
    // handler the picker would then offer.
    expect(slashCommandHandlers(['fit', 'no-such-id'], {})).toEqual({})
  })
})

/** A ctx whose every handler records that it was the one the record named. */
function handlerProbe(seen) {
  return new Proxy({ session: { actions: {
    undo: () => seen.push(['engine:undo']), redo: () => seen.push(['engine:redo']),
  } }, rTarget: 'route', drawer: 'tools' }, {
    get(target, key) {
      if (key in target) return target[key]
      if (typeof key !== 'string') return undefined
      return (...args) => { seen.push([key, args]); return true }
    },
    has() { return true },
  })
}

// --- 3: the ladder table equals the old ladder -----------------------------

/**
 * App.jsx's global key ladder as it stood BEFORE slice 10a, copied here as
 * literals. It returns the same decision shape `ladderDecision` does, so the
 * two can be compared directly. Nothing imports it; it exists to be the
 * independent oracle a refactor of the real ladder has to keep agreeing with.
 */
function OLD_LADDER(e, ctx) {
  const tag = ((e.target && e.target.tagName) || '').toLowerCase()
  const typing = tag === 'input' || tag === 'textarea'
  if ((e.metaKey || e.ctrlKey) && (e.key === 'k' || e.key === 'K')) {
    return { id: 'bar:focus', rung: '', route: 'kbd', preventDefault: true, instant: true }
  }
  if (e.key === 'Escape') {
    if (ctx.drawer) return { id: 'bar:escape', rung: 'drawer', route: 'kbd', preventDefault: false, instant: true }
    if (ctx.historyOpen) return { id: 'bar:escape', rung: 'history', route: 'kbd', preventDefault: false, instant: true }
    if (ctx.route) return { id: 'bar:escape', rung: 'route', route: 'kbd', preventDefault: false, instant: true }
    if (ctx.routeErr || ctx.runErr) return { id: 'bar:escape', rung: 'errors', route: 'kbd', preventDefault: false, instant: true }
    if (ctx.running) return { id: 'bar:escape', rung: 'running', route: 'kbd', preventDefault: false, instant: true }
    if (ctx.selectedHandle) return { id: 'bar:escape', rung: 'selection', route: 'kbd', preventDefault: false, instant: true }
    if (ctx.openProjectId) return { id: 'bar:escape', rung: 'project', route: 'kbd', preventDefault: false, instant: true }
    return null
  }
  if (!typing && (e.key === 'r' || e.key === 'R')
      && !e.metaKey && !e.ctrlKey && !e.altKey
      && ctx.rTarget && ctx.rTarget !== 'result') {
    return { id: 'bar:retry', rung: ctx.rTarget, route: 'kbd', preventDefault: true, instant: true }
  }
  const editable = typing || tag === 'select' || (e.target && e.target.isContentEditable)
  const interactive = e.target instanceof Element
    && e.target.closest('button, a, summary, [role="button"], [role="option"], [role="menuitem"]')
  if (!editable && !interactive && !ctx.drawer && !ctx.historyOpen
      && !e.metaKey && !e.ctrlKey && !e.altKey && e.key.length === 1 && e.key !== ' ') {
    return { id: 'bar:focus', rung: '', route: 'type', preventDefault: false, instant: false }
  }
  return null
}

const el = (html) => {
  const host = document.createElement('div')
  host.innerHTML = html
  return host.firstElementChild
}

const TARGETS = [
  ['body', () => document.createElement('div')],
  ['input', () => el('<input />')],
  ['textarea', () => el('<textarea></textarea>')],
  ['select', () => el('<select></select>')],
  ['button', () => el('<button type="button">go</button>')],
  ['inside a button', () => el('<button type="button"><span>go</span></button>').firstElementChild],
  ['menuitem', () => el('<div role="menuitem">row</div>')],
  ['contenteditable', () => {
    const node = document.createElement('div')
    node.isContentEditable = true
    return node
  }],
]

const KEYS = [
  { key: 'k', metaKey: true },
  { key: 'K', ctrlKey: true },
  { key: 'k' },
  { key: 'Escape' },
  { key: 'r' },
  { key: 'R' },
  { key: 'r', ctrlKey: true },
  { key: 'r', altKey: true },
  { key: 'a' },
  { key: ' ' },
  { key: 'Tab' },
  { key: 'ArrowDown' },
  { key: '/' },
]

const SHELL_STATES = [
  {},
  { drawer: 'tools' },
  { historyOpen: true },
  { route: { tool: 'x' } },
  { routeErr: 'boom' },
  { runErr: 'boom' },
  { running: true },
  { selectedHandle: 'h1' },
  { openProjectId: 'p1' },
  { rTarget: 'route' },
  { rTarget: 'history' },
  { rTarget: 'tools' },
  { rTarget: 'catalog' },
  { rTarget: 'refresh' },
  { rTarget: 'result' },
  // Every rung at once: the ORDER is the whole assertion.
  {
    drawer: 'tools', historyOpen: true, route: { tool: 'x' }, routeErr: 'boom',
    running: true, selectedHandle: 'h1', openProjectId: 'p1', rTarget: 'route',
  },
  { historyOpen: true, route: { tool: 'x' }, running: true, openProjectId: 'p1' },
  { running: true, selectedHandle: 'h1', openProjectId: 'p1', rTarget: 'catalog' },
]

describe('the key ladder, table-driven', () => {
  it('returns from Start before touching a route, run, selection or project', () => {
    const onCloseStart = vi.fn()
    const onInterruptRun = vi.fn()
    const onCloseProject = vi.fn()
    const shell = { startOpen: true, running: true, openProjectId: 'p1' }
    const event = { key: 'Escape', preventDefault: vi.fn() }
    expect(ladderDecision(event, shell)).toEqual({ id: 'bar:escape', rung: 'start', route: 'kbd', preventDefault: true, instant: true })
    ladderListener(shell, (state) => ({ ...state, onCloseStart, onInterruptRun, onCloseProject }))(event)
    expect(onCloseStart).toHaveBeenCalledTimes(1)
    expect(onInterruptRun).not.toHaveBeenCalled()
    expect(onCloseProject).not.toHaveBeenCalled()
    expect(ladderDecision(event, { ...shell, drawer: 'tools' }).rung).toBe('drawer')
    expect(ladderDecision(event, { ...shell, historyOpen: true }).rung).toBe('history')
  })

  it.each(['focused picker', 'armed command', 'WorldSpace card fit'])('leaves an Escape consumed by %s with its owner', () => {
    const event = new KeyboardEvent('keydown', { key: 'Escape', cancelable: true })
    event.preventDefault()
    const handlers = vi.fn()
    expect(ladderDecision(event, { startOpen: true })).toBeNull()
    ladderListener({ startOpen: true }, handlers)(event)
    expect(handlers).not.toHaveBeenCalled()
  })

  it('agrees with the pre-slice if/else on every key x target x shell state', () => {
    let cases = 0
    for (const [name, make] of TARGETS) {
      for (const spec of KEYS) {
        for (const ctx of SHELL_STATES) {
          const event = { ...spec, target: make() }
          const label = `${name} + ${JSON.stringify(spec)} + ${JSON.stringify(ctx)}`
          expect({ label, got: ladderDecision(event, ctx) })
            .toEqual({ label, got: OLD_LADDER(event, ctx) })
          cases += 1
        }
      }
    }
    // A guard on the guard: the literal, not the product, so a matrix that
    // silently shrank (a target, key or state dropped) fails here by name.
    expect(cases).toBe(1872)
  })

  it('pops exactly one Esc rung, topmost first, and runs only that handler', () => {
    expect(ESCAPE_RUNGS.map((r) => r.id))
      .toEqual(['owner', 'drawer', 'history', 'start', 'route', 'errors', 'running', 'selection', 'project'])
    const seen = []
    const ctx = {
      drawer: 'tools', historyOpen: true, running: true, openProjectId: 'p1',
      onCloseDrawer: () => seen.push('drawer'),
      onCloseHistory: () => seen.push('history'),
      onInterruptRun: () => seen.push('running'),
      onCloseProject: () => seen.push('project'),
    }
    expect(escapeRung(ctx)).toBe('drawer')
    expect(byId('bar:escape').run(ctx)).toBe('drawer')
    expect(seen).toEqual(['drawer'])
    // With the drawer closed the next rung down takes the key, not the bottom.
    const next = { ...ctx, drawer: null }
    expect(byId('bar:escape').run(next)).toBe('history')
    expect(seen).toEqual(['drawer', 'history'])
    // Nothing open: no handler runs and the record says why.
    expect(byId('bar:escape').run({})).toBe('')
    expect(byId('bar:escape').when({})).toBe(LADDER_REASONS.nothingOpen)
  })

  it('S27 reads the Escape owner stack before every shell rung', () => {
    const onEscape = vi.fn()
    const onCloseDrawer = vi.fn()
    function Owner() {
      useEscapeOwner('sheet-under-test', true, onEscape, { layer: 'sheet' })
      return null
    }
    const ctx = { drawer: 'details', onCloseDrawer }
    expect(escapeRung(ctx)).toBe('drawer')
    const { unmount } = render(createElement(Owner))
    expect(escapeRung(ctx)).toBe('owner')
    expect(byId('bar:escape').when(ctx)).toBe('')
    expect(byId('bar:escape').run(ctx)).toBe('owner')
    expect(onEscape).toHaveBeenCalledTimes(1)
    expect(onCloseDrawer).not.toHaveBeenCalled()
    // A real keypress: the stack consumes it in the capture phase, so the
    // ladder's own listener sees a prevented key and runs no shell rung.
    const ladder = ladderListener(ctx, (state) => state)
    window.addEventListener('keydown', ladder)
    try {
      fireEvent.keyDown(window, { key: 'Escape' })
    } finally {
      window.removeEventListener('keydown', ladder)
    }
    expect(onEscape).toHaveBeenCalledTimes(2)
    expect(onCloseDrawer).not.toHaveBeenCalled()
    unmount()
    expect(escapeRung(ctx)).toBe('drawer')
  })

  it.each(['nav', 'jobs', 'result', 'plan'])('closes the phone Studio %s drawer before lower Escape rungs', (studioDrawer) => {
    const onCloseDrawer = vi.fn()
    const onCloseHistory = vi.fn()
    const onInterruptRun = vi.fn()
    const shell = { phoneViewport: true, studioDrawer, historyOpen: true, running: true }
    const event = { key: 'Escape', preventDefault: vi.fn() }
    expect(ladderDecision(event, shell).rung).toBe('drawer')
    ladderListener(shell, (state) => ({ ...state, onCloseDrawer, onCloseHistory, onInterruptRun }))(event)
    expect(onCloseDrawer).toHaveBeenCalledTimes(1)
    expect(onCloseHistory).not.toHaveBeenCalled()
    expect(onInterruptRun).not.toHaveBeenCalled()
  })

  it.each(['none', null, undefined, ''])('treats Studio drawer %s as closed on phone', (studioDrawer) => {
    expect(escapeRung({ phoneViewport: true, studioDrawer })).toBe('')
    expect(escapeRung({ phoneViewport: true, studioDrawer, historyOpen: true })).toBe('history')
  })

  it.each(['nav', 'jobs', 'result', 'plan'])('keeps desktop Studio %s rails outside the Escape ladder', (studioDrawer) => {
    const onCloseDrawer = vi.fn()
    const onCloseHistory = vi.fn()
    const shell = { phoneViewport: false, studioDrawer, historyOpen: true }
    ladderListener(shell, (state) => ({ ...state, onCloseDrawer, onCloseHistory }))({ key: 'Escape', preventDefault: vi.fn() })
    expect(onCloseHistory).toHaveBeenCalledTimes(1)
    expect(onCloseDrawer).not.toHaveBeenCalled()
    expect(escapeRung({ phoneViewport: false, studioDrawer })).toBe('')
    expect(escapeRung({ phoneViewport: false, studioDrawer, drawer: 'details' })).toBe('drawer')
  })

  it('leaves a phone drawer open when a higher-priority owner consumes Escape', () => {
    const event = new KeyboardEvent('keydown', { key: 'Escape', cancelable: true })
    event.preventDefault()
    const handlers = vi.fn()
    const shell = { phoneViewport: true, studioDrawer: 'nav' }
    expect(ladderDecision(event, shell)).toBeNull()
    ladderListener(shell, handlers)(event)
    expect(handlers).not.toHaveBeenCalled()
  })

  it('fires the R rung rTarget names, and never the one ResultPanel owns', () => {
    expect(Object.keys(RETRY_RUNGS)).toEqual(['route', 'history', 'tools', 'catalog', 'refresh'])
    const seen = []
    const handlers = {
      onRetryRoute: () => seen.push('route'),
      onRetryHistory: () => seen.push('history'),
      onRetryTools: () => seen.push('tools'),
      onRetryCatalog: () => seen.push('catalog'),
      onRetryRefresh: () => seen.push('refresh'),
    }
    for (const target of Object.keys(RETRY_RUNGS)) {
      expect(byId('bar:retry').run({ ...handlers, rTarget: target })).toBe(target)
    }
    expect(seen).toEqual(['route', 'history', 'tools', 'catalog', 'refresh'])
    // 'result' is ResultPanel's own listener: duplicating it double-fired the
    // retry (two POST /api/run from one keypress).
    expect(retryRung({ rTarget: 'result' })).toBe('')
    expect(byId('bar:retry').run({ ...handlers, rTarget: 'result' })).toBe('')
    expect(byId('bar:retry').when({ rTarget: 'result' })).toBe(LADDER_REASONS.retryOwnedByResult)
    expect(byId('bar:retry').when({})).toBe(LADDER_REASONS.nothingToRetry)
    expect(seen).toHaveLength(5)
  })

  it('Shift+? opens the shortcut sheet outside a text field, and a real "?" typed into one stays a character', () => {
    const seen = []
    const ctx = { onOpenShortcuts: () => seen.push('shortcuts') }
    const outside = ladderDecision({ key: '?', target: { tagName: 'DIV' } }, ctx)
    expect(outside).toEqual({ id: 'bar:shortcuts', rung: '', route: 'kbd', preventDefault: true, instant: false })
    byId(outside.id).run(ctx)
    expect(seen).toEqual(['shortcuts'])
    // Typing "?" into the bar itself is text, not a shortcut.
    const typing = ladderDecision({ key: '?', target: { tagName: 'TEXTAREA' } }, {})
    expect(typing).toBeNull()
  })

  it('keeps the interactive-target selector the shell obeys spelled once', () => {
    expect(INTERACTIVE_TARGET_SELECTOR)
      .toBe('button, a, summary, [role="button"], [role="option"], [role="menuitem"]')
  })

  it('ignores an event with no key rather than throwing', () => {
    expect(ladderDecision(null, {})).toBeNull()
    expect(ladderDecision({}, {})).toBeNull()
  })

  // The listener App.jsx mounts. The pre-slice if/else built nothing for a key
  // that was not its own; the handler context (thirteen closures in App) must
  // not be built per keystroke either, only once a decision came back.
  it('builds the handler context only for a key the ladder takes, never for the rest', () => {
    const shell = { drawer: 'tools', rTarget: 'route' }
    const closed = []
    const handlers = vi.fn((state) => ({ ...state, onCloseDrawer: () => closed.push('drawer') }))
    const markInstant = vi.fn()
    const preventDefault = vi.fn()
    const onKey = ladderListener(shell, handlers, markInstant)
    const body = document.createElement('div')
    // Not the ladder's: Tab, an arrow, Space, a modified letter, a printable
    // key typed into a field. No context, no instant stamp, no preventDefault.
    for (const spec of [{ key: 'Tab' }, { key: 'ArrowDown' }, { key: ' ' }, { key: 'a', altKey: true }]) {
      onKey({ ...spec, target: body, preventDefault })
    }
    onKey({ key: 'a', target: el('<input />'), preventDefault })
    expect(handlers).not.toHaveBeenCalled()
    expect(markInstant).not.toHaveBeenCalled()
    expect(preventDefault).not.toHaveBeenCalled()
    // The ladder's: ONE context, built from the same shell, and the rung ran.
    onKey({ key: 'Escape', target: body, preventDefault })
    expect(handlers).toHaveBeenCalledTimes(1)
    expect(handlers).toHaveBeenCalledWith(shell)
    expect(closed).toEqual(['drawer'])
    expect(markInstant).toHaveBeenCalledTimes(1)
    expect(preventDefault).not.toHaveBeenCalled()
    // R with a live rung: preventDefault, instant, and one more context.
    onKey({ key: 'r', target: body, preventDefault })
    expect(handlers).toHaveBeenCalledTimes(2)
    expect(preventDefault).toHaveBeenCalledTimes(1)
    expect(markInstant).toHaveBeenCalledTimes(2)
  })

  it('refuses a listener with no shell or no handler builder at construction, not on the first key', () => {
    expect(() => ladderListener({}, null, () => {})).toThrow(TypeError)
    expect(() => ladderListener(null, () => ({}), () => {})).toThrow(TypeError)
  })
})

// --- KEYS-c: a key pressed during a text composition is the input method's ---

describe('KEYS-C: a key pressed during a text composition is never the ladder\'s', () => {
  const field = () => el('<input />')
  const plainTarget = () => document.createElement('div')
  // The two ways an engine marks a composing keydown, and the keydown that carries both.
  const ALL_MODIFIERS = { ctrlKey: true, shiftKey: true, altKey: true, metaKey: true }
  const MARKS = [
    ['isComposing', { isComposing: true }],
    ['keyCode 229', { keyCode: 229 }],
    ['isComposing and keyCode 229', { isComposing: true, keyCode: 229 }],
    // A mark withholds the key whatever rides beside it: the key's ordinary code, or held modifiers.
    ['isComposing with keyCode 27', { isComposing: true, keyCode: 27 }],
    ['isComposing with every modifier held', { isComposing: true, ...ALL_MODIFIERS }],
    ['keyCode 229 with every modifier held', { keyCode: 229, ...ALL_MODIFIERS }],
  ]
  const OPEN_RUNGS = [
    ['drawer', { drawer: 'tools' }, 'drawer'],
    ['phone drawer', { phoneViewport: true, studioDrawer: 'nav' }, 'drawer'],
    ['history', { historyOpen: true }, 'history'],
    ['Start', { startOpen: true }, 'start'],
    ['route decision', { route: { tool: 'x' } }, 'route'],
    ['error', { routeErr: 'boom' }, 'errors'],
    ['running job', { running: true }, 'running'],
    ['selection', { selectedHandle: 'h1' }, 'selection'],
    ['project', { openProjectId: 'p1' }, 'project'],
  ]

  it.each(OPEN_RUNGS)('KEYS-C01 Escape during a composition leaves the open %s alone', (name, shell, rung) => {
    for (const [, mark] of MARKS) {
      expect(ladderDecision({ key: 'Escape', ...mark, target: field() }, shell)).toBeNull()
      expect(ladderDecision({ key: 'Escape', ...mark, target: plainTarget() }, shell)).toBeNull()
    }
    // The control: the same key outside a composition is the ladder's.
    const plain = ladderDecision({ key: 'Escape', keyCode: 27, isComposing: false, target: field() }, shell)
    expect(plain).toMatchObject({ id: 'bar:escape', rung, route: 'kbd' })
  })

  it.each(MARKS)('KEYS-C02 the listener runs nothing for an Escape marked %s, then closes the drawer for a plain one', (name, mark) => {
    const shell = { phoneViewport: true, studioDrawer: 'nav', historyOpen: true }
    const onCloseDrawer = vi.fn()
    const onCloseHistory = vi.fn()
    const handlers = vi.fn((state) => ({ ...state, onCloseDrawer, onCloseHistory }))
    const markInstant = vi.fn()
    const preventDefault = vi.fn()
    const onKey = ladderListener(shell, handlers, markInstant)
    onKey({ key: 'Escape', ...mark, target: field(), preventDefault })
    expect(handlers).not.toHaveBeenCalled()
    expect(markInstant).not.toHaveBeenCalled()
    expect(preventDefault).not.toHaveBeenCalled()
    expect(onCloseDrawer).not.toHaveBeenCalled()
    expect(onCloseHistory).not.toHaveBeenCalled()
    onKey({ key: 'Escape', keyCode: 27, target: field(), preventDefault })
    expect(handlers).toHaveBeenCalledTimes(1)
    expect(onCloseDrawer).toHaveBeenCalledTimes(1)
    expect(onCloseHistory).not.toHaveBeenCalled()
    expect(markInstant).toHaveBeenCalledTimes(1)
  })

  it('KEYS-C03 reads the flag a real composing keydown carries', () => {
    const shell = { drawer: 'tools' }
    const composing = new KeyboardEvent('keydown', { key: 'Escape', isComposing: true, cancelable: true })
    expect(composing.isComposing).toBe(true)
    expect(ladderDecision(composing, shell)).toBeNull()
    const plain = new KeyboardEvent('keydown', { key: 'Escape', cancelable: true })
    expect(plain.isComposing).toBe(false)
    expect(ladderDecision(plain, shell)).toMatchObject({ id: 'bar:escape', rung: 'drawer' })
  })

  it.each(MARKS)('KEYS-C04 Mod+K marked %s stays with the input method, and focuses the bar otherwise', (name, mark) => {
    for (const mod of [{ ctrlKey: true }, { metaKey: true }]) {
      for (const key of ['k', 'K']) {
        expect(ladderDecision({ key, ...mod, ...mark, target: field() }, {})).toBeNull()
        expect(ladderDecision({ key, ...mod, target: field() }, {}))
          .toEqual({ id: 'bar:focus', rung: '', route: 'kbd', preventDefault: true, instant: true })
      }
    }
  })

  it.each(MARKS)('KEYS-C05 R, Shift+? and a printable key marked %s decide nothing outside a text field', (name, mark) => {
    const shell = { rTarget: 'route' }
    for (const key of ['r', 'R', '?', 'a']) {
      expect(ladderDecision({ key, ...mark, target: plainTarget() }, shell)).toBeNull()
    }
    expect(ladderDecision({ key: 'r', target: plainTarget() }, shell))
      .toEqual({ id: 'bar:retry', rung: 'route', route: 'kbd', preventDefault: true, instant: true })
    expect(ladderDecision({ key: '?', target: plainTarget() }, shell))
      .toEqual({ id: 'bar:shortcuts', rung: '', route: 'kbd', preventDefault: true, instant: false })
    expect(ladderDecision({ key: 'a', target: plainTarget() }, shell))
      .toEqual({ id: 'bar:focus', rung: '', route: 'type', preventDefault: false, instant: false })
  })

  it('KEYS-C06 only the two composition marks withhold a key: every other keyCode and a false flag do not', () => {
    const shell = { drawer: 'tools' }
    for (const extra of [{}, { keyCode: 0 }, { keyCode: 27 }, { keyCode: 228 }, { keyCode: 230 }, { keyCode: '229' },
      { isComposing: false }, { isComposing: undefined, keyCode: undefined }]) {
      expect(ladderDecision({ key: 'Escape', ...extra, target: field() }, shell)).toMatchObject({ rung: 'drawer' })
    }
  })
})

// --- S25: version Mod+Z off the drafting surface, the single-key switch ----

describe('S25 version Mod+Z off the drafting surface', () => {
  const body = () => document.createElement('div')
  const offCanvas = { draftingVisible: () => false, activeElement: null }
  const onCanvas = { draftingVisible: () => true, activeElement: null }
  const versionDecision = (id) => ({ id, rung: '', route: 'kbd', preventDefault: true, instant: false })

  it('Mod+Z and Mod+Shift+Z run version undo / redo outside drafting, through the records the ribbon clicks', () => {
    for (const mod of [{ ctrlKey: true }, { metaKey: true }]) {
      expect(ladderDecision({ key: 'z', ...mod, target: body() }, offCanvas)).toEqual(versionDecision('undo'))
      expect(ladderDecision({ key: 'Z', shiftKey: true, ...mod, target: body() }, offCanvas)).toEqual(versionDecision('redo'))
      expect(ladderDecision({ key: 'z', shiftKey: true, ...mod, target: body() }, offCanvas)).toEqual(versionDecision('redo'))
    }
    const onUndo = vi.fn()
    const onRedo = vi.fn()
    const preventDefault = vi.fn()
    const shell = { ...offCanvas, hasVersions: true, canUndo: true, canRedo: true }
    const onKey = ladderListener(shell, (state) => ({ ...state, onUndo, onRedo }))
    onKey({ key: 'z', ctrlKey: true, target: body(), preventDefault })
    onKey({ key: 'Z', ctrlKey: true, shiftKey: true, target: body(), preventDefault })
    expect(onUndo).toHaveBeenCalledTimes(1)
    expect(onRedo).toHaveBeenCalledTimes(1)
    expect(preventDefault).toHaveBeenCalledTimes(2)
  })

  it.each([
    ['nothing to undo', { canUndo: false }],
    ['no versions', { hasVersions: false }],
    ['a run in flight', { running: true }],
    ['a preview', { previewing: true }],
    ['a version change in flight', { versionBusy: true }],
    ['blocked mutations', { mutationsBlocked: true }],
  ])('claims Mod+Z but runs nothing under the ribbon gate: %s', (_, blocked) => {
    const onUndo = vi.fn()
    const shell = { ...offCanvas, hasVersions: true, canUndo: true, ...blocked }
    expect(byId('undo').when(shell)).not.toBe('')
    ladderListener(shell, (state) => ({ ...state, onUndo }))({ key: 'z', ctrlKey: true, target: body(), preventDefault: vi.fn() })
    expect(onUndo).not.toHaveBeenCalled()
  })

  it('yields Mod+Z to the engine inside drafting (a visible engine document)', () => {
    expect(ladderDecision({ key: 'z', ctrlKey: true, target: body() }, onCanvas)).toBeNull()
    expect(ladderDecision({ key: 'Z', ctrlKey: true, shiftKey: true, target: body() }, onCanvas)).toBeNull()
    // The real DOM read: a visible [data-engine-document] yields, a hidden one does not.
    const host = document.createElement('div')
    host.setAttribute('data-engine-document', 'one.dxf')
    const target = document.createElement('div')
    document.body.append(host, target)
    try {
      expect(engineDocumentVisible()).toBe(true)
      expect(ladderDecision({ key: 'z', ctrlKey: true, target }, { activeElement: null })).toBeNull()
      host.hidden = true
      expect(engineDocumentVisible()).toBe(false)
      expect(ladderDecision({ key: 'z', ctrlKey: true, target }, { activeElement: null })).toEqual(versionDecision('undo'))
    } finally {
      host.remove()
      target.remove()
    }
  })

  it('leaves Mod+Z to a typing target, a focused editor, Alt, a consumed key and a repeat', () => {
    for (const html of ['<input />', '<textarea></textarea>', '<select></select>', '<div role="textbox"></div>', '<div contenteditable="true"></div>']) {
      expect(ladderDecision({ key: 'z', ctrlKey: true, target: el(html) }, offCanvas)).toBeNull()
    }
    expect(ladderDecision({ key: 'z', ctrlKey: true, target: body() }, { ...offCanvas, activeElement: el('<input />') })).toBeNull()
    expect(ladderDecision({ key: 'z', ctrlKey: true, altKey: true, target: body() }, offCanvas)).toBeNull()
    expect(ladderDecision({ key: 'z', ctrlKey: true, metaKey: true, target: body() }, offCanvas)).toBeNull()
    expect(ladderDecision({ key: 'z', ctrlKey: true, repeat: true, target: body() }, offCanvas)).toBeNull()
    expect(ladderDecision({ key: 'z', ctrlKey: true, defaultPrevented: true, target: body() }, offCanvas)).toBeNull()
    expect(versionShortcutDecision({ key: 'y', ctrlKey: true, target: body() }, offCanvas)).toBeNull()
    // A bare z is not a chord: it still falls into the bar like any letter.
    expect(ladderDecision({ key: 'z', target: body() }, offCanvas).route).toBe('type')
  })
})

describe('S25 the single-key shortcut switch', () => {
  const body = () => document.createElement('div')

  it('with the switch off, bare R and Shift+? are null and Mod chords still work', () => {
    const off = { singleKeyShortcuts: false, rTarget: 'route' }
    expect(ladderDecision({ key: 'r', target: body() }, off)).toBeNull()
    expect(ladderDecision({ key: 'R', target: body() }, off)).toBeNull()
    expect(ladderDecision({ key: '?', target: body() }, off)).toBeNull()
    expect(ladderDecision({ key: 'r', target: body() }, { ...off, singleKeyShortcuts: true }).id).toBe('bar:retry')
    expect(ladderDecision({ key: '?', target: body() }, { singleKeyShortcuts: true }).id).toBe('bar:shortcuts')
    expect(ladderDecision({ key: 'k', ctrlKey: true, target: body() }, off).id).toBe('bar:focus')
    expect(ladderDecision({ key: 'z', ctrlKey: true, target: body() }, { ...off, draftingVisible: () => false, activeElement: null }).id).toBe('undo')
    expect(ladderDecision({ key: 'Escape', target: body() }, { ...off, drawer: 'tools' }).rung).toBe('drawer')
    expect(ladderDecision({ key: 'a', target: body() }, off).route).toBe('type')
  })

  it('reads the stored switch at keystroke time when the context carries none', () => {
    try {
      expect(writeSingleKeyShortcuts(false)).toBe(true)
      expect(ladderDecision({ key: '?', target: body() }, {})).toBeNull()
      expect(ladderDecision({ key: 'r', target: body() }, { rTarget: 'route' })).toBeNull()
      expect(writeSingleKeyShortcuts(true)).toBe(true)
      expect(ladderDecision({ key: '?', target: body() }, {}).id).toBe('bar:shortcuts')
      expect(ladderDecision({ key: 'r', target: body() }, { rTarget: 'route' }).id).toBe('bar:retry')
    } finally {
      localStorage.removeItem(SINGLE_KEY_SHORTCUTS_KEY)
    }
  })
})

// --- 4: the accessible-name composer --------------------------------------

describe('the accessible name', () => {
  // The exact strings draftingRibbon.test.jsx:53,71 and
  // engineSessionProvider.test.jsx pin, composed from the registry's own
  // reasons. If this drifts, those suites go red — this test says so first.
  it('reproduces the pinned "(unavailable: <reason>)" strings byte for byte', () => {
    expect(accessibleName('count-by-layer', REASONS.running))
      .toBe('count-by-layer (unavailable: a run is in flight)')
    expect(accessibleName('delete-marked-panel', REASONS.writeLocked))
      .toBe('delete-marked-panel (unavailable: another session holds the edit lock)')
    expect(accessibleName('line', DRAW_REASONS.noDocument))
      .toBe('line (unavailable: no drawing in the browser engine yet)')
    expect(accessibleName('move', MODIFY_REASONS.noSelection))
      .toBe('move (unavailable: select an entity in the drawing)')
  })

  it('is the bare label when the action is live', () => {
    expect(accessibleName('fit', '')).toBe('fit')
    expect(accessibleName('fit')).toBe('fit')
  })

  // The template this replaced rendered `aria-label={label}` for a live
  // control, and React omits the attribute for undefined. A control with no
  // label keeps that omission; it never gains an aria-label="" a screen
  // reader announces as nothing. A reason with no subject is not a name
  // either. An empty-string label is a name, and stays one.
  it('omits the name (undefined) when there is no label, and keeps an empty one', () => {
    expect(accessibleName(undefined)).toBeUndefined()
    expect(accessibleName(null, '')).toBeUndefined()
    expect(accessibleName(undefined, REASONS.noDrawing)).toBeUndefined()
    expect(accessibleName('')).toBe('')
    expect(accessibleName('', '')).toBe('')
  })

  it('composes the same name for a record the ribbon would render disabled', () => {
    const tool = ribbonTool(byId('undo'), { hasVersions: false })
    expect(accessibleName(tool.label, tool.reason)).toBe('undo (unavailable: no versioned drawing)')
  })
})

// --- the reason ladders, still pure over the session record ----------------

describe('the engine reason ladders', () => {
  it('enables only the six selection-set edit records and keeps the generic gate single-only', () => {
    const entities = [
      { id: '16', type: 'LINE', editable: true },
      { id: '19', type: 'CIRCLE', editable: true },
    ]
    const session = { engineParsed: true, busy: false, selectedIds: ['16', '19'], selectedId: '', selected: null, entities }
    expect(modifyReason(session)).toBe(MODIFY_REASONS.multiSelection)
    for (const op of ['delete', 'move', 'copy', 'rotate', 'scale', 'mirror']) {
      expect(modifyOpReason(op, session)).toBe('')
      expect(byId(`modify:${op}`).when({ session })).toBe('')
      expect(modifyOpReason(op, { ...session, busy: true })).toBe(MODIFY_REASONS.busy)
      expect(modifyOpReason(op, { ...session, engineParsed: false })).toBe(MODIFY_REASONS.noDocument)
      expect(modifyOpReason(op, { ...session, selectedIds: ['16', '999'] })).toBe(MODIFY_REASONS.missingSelection)
      expect(modifyOpReason(op, { ...session, selectedIds: ['16', 19] })).toBe(MODIFY_REASONS.invalidSelection)
      const many = Array.from({ length: 257 }, (_, i) => String(i + 16))
      expect(modifyOpReason(op, { ...session, selectedIds: many })).toBe(MODIFY_REASONS.selectionLimit)
    }
    for (const op of ['explode', 'offset', 'trim', 'extend', 'fillet', 'chamfer', 'arrayRect', 'arrayPolar', 'copyClip', 'cutClip']) {
      expect(modifyOpReason(op, session)).toBe(MODIFY_REASONS.multiSelection)
    }
    for (const [type, reason] of [['INSERT', MODIFY_REASONS.placedInsert], ['DIMENSION', MODIFY_REASONS.placedDimension], ['MLEADER', MODIFY_REASONS.placedMleader], ['XLINE', MODIFY_REASONS.readOnlyKind]]) {
      const mixed = { ...session, entities: [entities[0], { ...entities[1], type, editable: false }] }
      for (const op of ['move', 'copy', 'rotate', 'scale', 'mirror']) expect(modifyOpReason(op, mixed)).toBe(reason)
      expect(modifyOpReason('delete', mixed)).toBe(type === 'XLINE' ? MODIFY_REASONS.readOnlyKind : '')
    }
  })
  it('answers the Draw ladder in resolution order', () => {
    expect(drawReason(null)).toBe(DRAW_REASONS.noDocument)
    expect(drawReason({ errorKind: 'crashed' })).toBe(DRAW_REASONS.crashed)
    expect(drawReason({ engineParsed: false })).toBe(DRAW_REASONS.noDocument)
    expect(drawReason({ engineParsed: true, busy: true })).toBe(DRAW_REASONS.busy)
    expect(drawReason({ engineParsed: true })).toBe('')
  })

  it('answers the Modify ladder in resolution order, selection last', () => {
    expect(modifyReason(null)).toBe(MODIFY_REASONS.noDocument)
    expect(modifyReason({ errorKind: 'crashed' })).toBe(MODIFY_REASONS.crashed)
    expect(modifyReason({ engineParsed: true, busy: true })).toBe(MODIFY_REASONS.busy)
    expect(modifyReason({ engineParsed: true })).toBe(MODIFY_REASONS.noSelection)
    expect(modifyReason({ engineParsed: true, selected: { editable: false } }))
      .toBe(MODIFY_REASONS.readOnlyKind)
    expect(modifyReason({ engineParsed: true, selected: { editable: true } })).toBe('')
  })

  // W4g-7b-03c-f (kimi, #1121 point 6): the Properties panel's ladder waives
  // the ONE rung that refuses an INSERT reference (a property is not
  // geometry), every other rung — including a non-INSERT read-only kind —
  // unchanged from the Modify ladder above.
  it('answers the Properties ladder identically to Modify, except an INSERT reference is live', () => {
    expect(propertyReason(null)).toBe(MODIFY_REASONS.noDocument)
    expect(propertyReason({ errorKind: 'crashed' })).toBe(MODIFY_REASONS.crashed)
    expect(propertyReason({ engineParsed: true, busy: true })).toBe(MODIFY_REASONS.busy)
    expect(propertyReason({ engineParsed: true })).toBe(MODIFY_REASONS.noSelection)
    expect(propertyReason({ engineParsed: true, selected: { editable: false, type: 'DIMENSION' } }))
      .toBe(MODIFY_REASONS.readOnlyKind)
    expect(propertyReason({ engineParsed: true, selected: { editable: false, type: 'INSERT' } })).toBe('')
    expect(propertyReason({ engineParsed: true, selected: { editable: true } })).toBe('')
  })

  it('the four Properties-panel records (matchprop, setColor, setLinetype, setLineweight) gate on propertyReason, not modifyReason', () => {
    const insertCtx = { session: { engineParsed: true, selected: { editable: false, type: 'INSERT' } }, reach: null }
    for (const op of ['matchprop', 'setColor', 'setLinetype', 'setLineweight']) {
      const record = byId(`modify:${op}`)
      expect(record.panel).toBe('properties')
      expect(record.when(insertCtx)).toBe('')
    }
    // Placed selections reach the builder's verb-specific gate.
    const geometryOps = forGroup('modify').filter((a) => a.panel !== 'properties')
    expect(geometryOps.length).toBeGreaterThan(0)
    for (const record of geometryOps) {
      expect(record.when(insertCtx)).toBe(record.op === 'explode' ? MODIFY_REASONS.unsupportedInsert : '')
    }
  })

  it('W21B1-placed-actions', () => {
    for (const id of ['clipboard:copyClip', 'clipboard:cutClip', 'modify:explode']) {
      for (const [type, reason] of [
        ['INSERT', 'Copy, Cut and Explode do not support INSERT block references yet'],
        ['DIMENSION', 'Copy, Cut and Explode do not support DIMENSION entities yet'],
      ]) {
        for (const editable of [false, true, undefined]) {
          const selected = editable === undefined ? { type } : { type, editable }
          expect(byId(id).when({ session: { engineParsed: true, selectedIds: ['one'], selected } })).toBe(reason)
        }
      }
    }
  })

  it('W21B1-priority', () => {
    for (const id of ['clipboard:copyClip', 'clipboard:cutClip', 'modify:explode']) {
      const action = byId(id)
      expect(action.when({ session: null })).toBe(MODIFY_REASONS.noDocument)
      expect(action.when({ session: null, reach: { state: 'opening', sentence: 'opening the drawing' } }))
        .toBe('opening the drawing')
      for (const type of ['LINE', 'INSERT', 'DIMENSION']) {
        const session = { engineParsed: true, selectedIds: ['one'], selected: { type, editable: true } }
        for (const [patch, reason] of [
          [{ errorKind: 'crashed', busy: true }, MODIFY_REASONS.crashed],
          [{ engineParsed: false, busy: true }, MODIFY_REASONS.noDocument],
          [{ busy: true }, MODIFY_REASONS.busy],
          [{ selectedIds: ['one', 'two'] }, MODIFY_REASONS.multiSelection],
          [{ selected: null, selectedIds: [] }, MODIFY_REASONS.noSelection],
          [{ selected: { type: 'HATCH', editable: false } }, MODIFY_REASONS.readOnlyKind],
        ]) expect(action.when({ session: { ...session, ...patch } })).toBe(reason)
      }
    }
  })

  it('W21B1-preserves-other-actions', () => {
    for (const id of ['clipboard:copyClip', 'clipboard:cutClip', 'modify:explode']) {
      for (const [type, editable] of [['LINE', true], ['LWPOLYLINE', true], ['MLEADER', false]]) {
        expect(byId(id).when({ session: { engineParsed: true, selectedIds: ['one'], selected: { type, editable } } }))
          .toBe('')
      }
    }
    for (const type of ['INSERT', 'DIMENSION']) {
      const ctx = { session: { engineParsed: true, selectedIds: ['one'], selected: { type, editable: false }, clipboard: { type: 'LINE' } } }
      for (const id of ['modify:move', 'modify:delete', 'modify:setColor', 'clipboard:pasteClip']) {
        expect(byId(id).when(ctx)).toBe(type === 'DIMENSION' && id === 'modify:setColor' ? PROPERTY_REASONS.readOnlyKind : '')
      }
    }
  })

  it('W21B1-projection-and-vocabulary', () => {
    for (const id of ['clipboard:copyClip', 'clipboard:cutClip', 'modify:explode']) {
      for (const [type, reason, code] of [
        ['INSERT', 'Copy, Cut and Explode do not support INSERT block references yet', 'MODIFY_REASONS.unsupportedInsert'],
        ['DIMENSION', 'Copy, Cut and Explode do not support DIMENSION entities yet', 'MODIFY_REASONS.unsupportedDimension'],
      ]) {
        const tool = ribbonTool(byId(id), { session: { engineParsed: true, selectedIds: ['one'], selected: { type, editable: false } } })
        expect(tool.disabled).toBe(true)
        expect(tool.reason).toBe(reason)
        expect(KNOWN_REASON_VALUES.has(reason)).toBe(true)
        expect(reasonCode(reason)).toBe(code)
        expect(accessibleName(tool.label, reason)).toBe(tool.label + ' (unavailable: ' + reason + ')')
      }
    }
  })
})

// W4g-7b-05c: the four deferred controls' frozen reasons, read by both ribbon
// panels (flag on and off), the typed words and the script runner.
describe('W4g-7b-05c-3 F4: placed selections reach the verb gate', () => {
  it.each(['INSERT', 'DIMENSION', 'MLEADER'])('Modify is live for a read-only %s projection', (type) => {
    expect(PLACED_KINDS.has(type)).toBe(true)
    expect(modifyReason({ engineParsed: true, selected: { editable: false, type } })).toBe('')
  })

  it('other read-only kinds keep the existing rung', () => {
    expect(modifyReason({ engineParsed: true, selected: { editable: false, type: 'HATCH' } })).toBe(MODIFY_REASONS.readOnlyKind)
  })
})

describe('DEFERRED_REASONS', () => {
  it('seats both named group operations as real document-gated tools', () => {
    const actions = forGroup('groups')
    expect(actions.map((action) => action.id)).toEqual(['groups:group', 'groups:ungroup'])
    for (const action of actions) {
      expect(action.panel).toBe('groups')
      expect(action.when({ session: { engineParsed: true, selected: null } })).toBe('')
    }
  })
  it('is frozen, with the remaining leader sentence', () => {
    expect(Object.isFrozen(DEFERRED_REASONS)).toBe(true)
    expect(DEFERRED_REASONS).toEqual({
      leader: "unavailable; a leader's annotation is an association the contract does not carry yet",
    })
  })
})

describe('engine shortcut records', () => {
  const eligible = {
    canvasShown: true, commandLineEmpty: true,
    session: { engineParsed: true, selected: { type: 'LINE', editable: true }, undoDepth: 1, redoDepth: 1 },
  }
  const bindings = [
    [{ key: 'Delete' }, 'modify:delete'],
    [{ key: 'z', ctrlKey: true }, 'engine:undo'],
    [{ key: 'y', ctrlKey: true }, 'engine:redo'],
    [{ key: 'z', ctrlKey: true, shiftKey: true }, 'engine:redo'],
    [{ key: 'c', ctrlKey: true }, 'clipboard:copyClip'],
    [{ key: 'x', ctrlKey: true }, 'clipboard:cutClip'],
    [{ key: 'v', ctrlKey: true }, 'clipboard:pasteClip'],
    [{ key: 'Enter' }, 'engine:repeat'],
    [{ key: ' ' }, 'engine:repeat'],
  ]
  it('B2-34 every advertised engine chord and alias reaches its declared record', () => {
    const reached = new Set()
    for (const [event, id] of bindings) {
      expect(engineShortcutDecision(event, eligible)).toBe(id)
      if (event.ctrlKey) expect(engineShortcutDecision({ ...event, ctrlKey: false, metaKey: true }, eligible)).toBe(id)
      expect(byId(id).triggers.keyboard).toBe('kbd')
      reached.add(id)
    }
    expect([...reached].sort()).toEqual(Object.keys(ENGINE_SHORTCUTS).sort())
    for (const event of [{ key: 'Backspace' }, { key: 'Delete', shiftKey: true }, { key: 'c' }, { key: 'F8' }]) {
      expect(engineShortcutDecision(event, eligible)).toBeNull()
    }
  })
  it('B2-02 engine undo uses history depth regardless of dirty or server version gates', () => {
    for (const dirty of [true, false]) {
      const ctx = { session: { ...eligible.session, dirty }, hasVersions: false, versionBusy: true }
      expect(byId('engine:undo').when(ctx)).toBe('')
      expect(byId('undo').when(ctx)).toBe(REASONS.noVersions)
    }
    expect(historyStepReason({ engineParsed: true }, 'undo')).toBe(REASONS.nothingToUndo)
  })
  it('B2-03 redo aliases share the engine record and preserve version cluster identity', () => {
    for (const modifier of ['ctrlKey', 'metaKey']) {
      expect(engineShortcutDecision({ key: 'Y', [modifier]: true }, eligible)).toBe('engine:redo')
      expect(engineShortcutDecision({ key: 'Z', [modifier]: true, shiftKey: true }, eligible)).toBe('engine:redo')
    }
    expect(forCluster('version').map((action) => action.id)).toEqual(['undo', 'redo', 'history'])
    expect(byId('engine:redo').when({ session: { engineParsed: true } })).toBe(REASONS.nothingToRedo)
  })
  it.each([
    ['B2-09', 'INSERT', MODIFY_REASONS.unsupportedInsert],
    ['B2-10', 'DIMENSION', MODIFY_REASONS.unsupportedDimension],
  ])('%s clipboard keyboard and ribbon gates give the same placed entity reason', (id, type, reason) => {
    const ctx = { session: { ...eligible.session, selected: { type, editable: false } } }
    for (const actionId of ['clipboard:copyClip', 'clipboard:cutClip']) {
      const action = byId(actionId)
      expect(action.when(ctx)).toBe(reason)
      expect(ribbonTool(action, ctx).reason).toBe(reason)
    }
  })
  it('B2-21 Alt excludes every engine binding and alias for both Mod variants', () => {
    for (const [event] of bindings) {
      expect(engineShortcutDecision({ ...event, altKey: true }, eligible)).toBeNull()
      if (event.ctrlKey) expect(engineShortcutDecision({ ...event, ctrlKey: false, metaKey: true, altKey: true }, eligible)).toBeNull()
    }
  })
  it('B2-36 Ctrl and Cmd together yield while either modifier alone resolves each action', () => {
    for (const [event, id] of [
      [{ key: 'x' }, 'clipboard:cutClip'],
      [{ key: 'c' }, 'clipboard:copyClip'],
      [{ key: 'v' }, 'clipboard:pasteClip'],
      [{ key: 'z' }, 'engine:undo'],
      [{ key: 'z', shiftKey: true }, 'engine:redo'],
    ]) {
      expect(engineShortcutDecision({ ...event, ctrlKey: true, metaKey: true }, eligible)).toBeNull()
      for (const modifier of ['ctrlKey', 'metaKey']) {
        expect(engineShortcutDecision({ ...event, [modifier]: true }, eligible)).toBe(id)
      }
    }
  })
})
