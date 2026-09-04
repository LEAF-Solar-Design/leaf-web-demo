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

import {
  ACTIONS,
  DRAW_REASONS,
  ESCAPE_RUNGS,
  INTERACTIVE_TARGET_SELECTOR,
  KNOWN_REASON_VALUES,
  LADDER_REASONS,
  MODIFY_REASONS,
  MAX_ID_CHARS,
  MAX_LABEL_CHARS,
  REASONS,
  RETRY_RUNGS,
  SURFACES,
  accessibleName,
  byId,
  drawReason,
  escapeRung,
  forCluster,
  forGroup,
  forSurface,
  keyboardTable,
  ladderDecision,
  ladderListener,
  modifyReason,
  retryRung,
  ribbonTool,
  slashCommandHandlers,
  slashStaticEntries,
  validateRegistry,
} from './actionRegistry.js'

// --- 1: the registry holds up ---------------------------------------------

describe('the registry', () => {
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

  it('advertises a cap for exactly the three keys bound today, and invents none', () => {
    expect(keyboardTable()).toEqual([
      { id: 'bar:focus', label: 'Command bar', kbd: 'Mod+K', surface: 'bar' },
      { id: 'bar:escape', label: 'Close the topmost surface', kbd: 'Escape', surface: 'bar' },
      { id: 'bar:retry', label: 'Retry the failed step', kbd: 'R', surface: 'bar' },
    ])
    // Slice 10b assigns the rest with the design session. Until then every
    // ribbon, engine and picker record records the absence honestly.
    for (const action of ACTIONS) {
      if (action.surface !== 'bar') expect(action.kbd).toBeNull()
    }
  })

  it('projects the ribbon clusters and the engine groups the builders seat, in registry order', () => {
    expect(forCluster('view').map((a) => a.id)).toEqual(['fit', 'zoom-in', 'zoom-out', 'properties-pane'])
    expect(forCluster('version').map((a) => a.id)).toEqual(['undo', 'redo', 'history'])
    // W4g-4: RECTANG joined Draw; COPY, MIRROR, ROTATE, SCALE, EXPLODE joined Modify.
    expect(forGroup('draw').map((a) => a.id)).toEqual([
      'draw:createLine', 'draw:createPolyline', 'draw:createCircle', 'draw:createArc', 'draw:createRectangle',
    ])
    expect(forGroup('modify').map((a) => a.id)).toEqual([
      'modify:delete', 'modify:move', 'modify:moveVertex',
      'modify:addVertex', 'modify:deleteVertex', 'modify:setLayer',
      // W4g-5: OFFSET joined the row (a parallel copy, computed here and
      // drawn by the engine's own create).
      'modify:copy', 'modify:mirror', 'modify:rotate', 'modify:scale', 'modify:explode', 'modify:offset',
      // W4g-5b: ARRAY, as the reference's two forms, because they take
      // different operands.
      'modify:arrayRect', 'modify:arrayPolar',
    ])
  })

  // A ribbon cluster and an engine group are two fields. A future ribbon
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
  return new Proxy({ session: null, rTarget: 'route', drawer: 'tools' }, {
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
      .toEqual(['drawer', 'history', 'route', 'errors', 'running', 'selection', 'project'])
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
})
