// W4f slice B: a typed command word, delivered as the ONE cockpit:command
// window event, arms the same prompt a ribbon click arms; ERASE runs on a live
// selection and does nothing without one; anything malformed is ignored.
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { StrictMode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { byId, CLIPBOARD_REASONS, DRAW_REASONS, MODIFY_REASONS, REASONS, REPEAT_REASONS } from '../lib/actionRegistry.js'
import RoutePanel from '../components/RoutePanel.jsx'
import { COCKPIT_COMMAND_EVENT, parseDrawingCommand } from '../lib/commandWords.js'

import CadEditSurface from './CadEditSurface.jsx'
import CommandLineArmer, { acceptsCommand } from './CommandLineArmer.jsx'
import CanvasPointPicker from './CanvasPointPicker.jsx'
import EngineRibbonClusters from './EngineRibbonClusters.jsx'
import EngineSessionProvider, { useEngineSessionContext } from './EngineSessionProvider.jsx'
import DraftingRibbon from '../site/DraftingRibbon.jsx'

class ScriptedWorker {
  constructor() { this.posted = []; this.listeners = new Map(); this.terminated = false }
  addEventListener(type, fn) { this.listeners.set(type, fn) }
  removeEventListener(type) { this.listeners.delete(type) }
  postMessage(message) { this.posted.push(message) }
  terminate() { this.terminated = true }
  emit(data) { act(() => { this.listeners.get('message')?.({ data }) }) }
}

const LINE = { id: 'e1', type: 'LINE', layer: 'Panels', vertices: [[0, 0], [1, 1]] }

function fileOf(name = 'one.dxf') {
  const bytes = new TextEncoder().encode('0\nEOF\n')
  const file = new File([bytes], name, { type: 'application/dxf' })
  file.arrayBuffer = async () => bytes.buffer.slice(0)
  Object.defineProperty(file, 'size', { value: bytes.length })
  return file
}

let workers
function mount(picker = null, { strict = false, onBeforeArm = null, saveTarget = null } = {}) {
  workers = []
  const handle = {}
  function Probe() {
    handle.context = useEngineSessionContext()
    return null
  }
  const createWorker = vi.fn(() => { const w = new ScriptedWorker(); workers.push(w); return w })
  const tree = (ribbon = true, armer = true) => (
    <EngineSessionProvider createWorker={createWorker} onBeforeArm={onBeforeArm} saveTarget={saveTarget}>
      <Probe />
      <DraftingRibbon clusters={[]}>
        {ribbon && <EngineRibbonClusters importOpen={false} onToggleImport={() => {}} />}
        {armer && <CommandLineArmer />}
      </DraftingRibbon>
      {picker && <CanvasPointPicker {...picker} />}
      <CadEditSurface enabled />
    </EngineSessionProvider>
  )
  handle.view = render(tree(), strict ? { wrapper: StrictMode } : undefined)
  handle.rerender = (ribbon = true, armer = true) => handle.view.rerender(tree(ribbon, armer))
  return handle
}

async function openAndLoad(entities = [LINE]) {
  await act(async () => {
    fireEvent.change(screen.getByLabelText('DXF file'), { target: { files: [fileOf()] } })
    await Promise.resolve()
    await Promise.resolve()
  })
  await waitFor(() => expect(workers.length).toBeGreaterThan(0))
  workers.at(-1).emit({ type: 'documentLoaded', documentId: 'one.dxf', entities, entityCount: entities.length, unsupported: [] })
}

const command = (detail) => act(() => { window.dispatchEvent(new CustomEvent(COCKPIT_COMMAND_EVENT, { detail })) })
const promptEl = () => screen.queryByTestId('cockpit-prompt')
const point = (text) => {
  const detail = { text, handled: false }
  act(() => window.dispatchEvent(new CustomEvent('cockpit:point', { detail })))
  return detail.handled
}

beforeEach(() => {
  globalThis.URL.createObjectURL = vi.fn(() => 'blob:cad-edit-test')
  globalThis.URL.revokeObjectURL = vi.fn()
})

const B2_KEYS = [
  { key: 'Delete' },
  ...['ctrlKey', 'metaKey'].flatMap((modifier) => [
    ...['z', 'y', 'c', 'x', 'v'].map((key) => ({ key, [modifier]: true })),
    { key: 'z', [modifier]: true, shiftKey: true },
  ]),
  { key: 'Enter' }, { key: ' ' },
]
const b2Key = (spec, target = window) => {
  const event = new KeyboardEvent('keydown', { ...spec, bubbles: true, cancelable: true })
  act(() => target.dispatchEvent(event))
  return event
}
const bodyFocus = () => act(() => {
  document.activeElement?.blur?.()
  expect(document.activeElement).toBe(document.body)
})
const edits = () => workers.at(-1).posted.filter((message) => message.type === 'applyEdit')
const loaded = (entities = [LINE], documentId = 'one.dxf') => workers.at(-1).emit({
  type: 'documentLoaded', documentId, entities, entityCount: entities.length, unsupported: [],
})
const applied = (op = 'delete', entities = []) => workers.at(-1).emit({
  type: 'editApplied', op, ok: true, entities, entityCount: entities.length,
  bytes: new Uint8Array([48, 10]), byteLength: 2,
})
async function b2Studio(entities = [LINE], options = {}) {
  const studio = mount(null, options)
  await openAndLoad(entities)
  const dom = render(<div className="app" data-drawer="none">
    <div data-testid="b2-canvas" data-engine-document="one.dxf" />
    <input data-testid="command-bar" aria-label="Command bar" role="combobox" defaultValue="" />
  </div>)
  studio.dom = dom
  studio.canvas = screen.getByTestId('b2-canvas')
  studio.bar = screen.getByTestId('command-bar')
  bodyFocus()
  return studio
}
function selectLine(studio) {
  act(() => studio.context.session.actions.select('e1'))
  bodyFocus()
}
function rememberLine(studio) {
  act(() => studio.context.setArmed({ group: 'draw', op: 'createLine', from: [12, 34] }, { rearm: true }))
  act(() => studio.context.setArmed(null))
  bodyFocus()
}
async function reloadDrawing(studio, name) {
  await act(async () => { await studio.context.session.actions.open(fileOf(name)) })
  loaded([LINE], name)
  studio.canvas.dataset.engineDocument = name
  bodyFocus()
}
function assertUnchanged(studio, specs = B2_KEYS, target = window) {
  const before = [...workers.at(-1).posted]
  const clipboard = studio.context.session.clipboard
  const armed = studio.context.armed
  const history = [studio.context.session.undoDepth, studio.context.session.redoDepth]
  for (const spec of specs) expect(b2Key(spec, target).defaultPrevented).toBe(false)
  expect(workers.at(-1).posted).toEqual(before)
  expect(studio.context.session.clipboard).toBe(clipboard)
  expect(studio.context.armed).toBe(armed)
  expect([studio.context.session.undoDepth, studio.context.session.redoDepth]).toEqual(history)
}

describe('canvas drafting shortcuts', () => {
  it('B2-01 Delete dispatches the registry erase once to the worker', async () => {
    const studio = await b2Studio()
    selectLine(studio)
    expect(b2Key({ key: 'Delete' }).defaultPrevented).toBe(true)
    expect(edits()).toEqual([{ type: 'applyEdit', op: 'delete', payload: { entityId: 'e1' } }])
    expect(studio.context.session.busy).toBe(true)
  })

  it.each(['ctrlKey', 'metaKey'])('B2-02 %s undo reaches the engine snapshot stack for dirty and clean drawings', async (modifier) => {
    const save = vi.fn(async () => ({ new_version: { version: 2, parent: 1 }, head: 2 }))
    const studio = await b2Studio([LINE], { saveTarget: { drawingId: 'drawing', headVersion: 1, save } })
    selectLine(studio)
    b2Key({ key: 'Delete' })
    applied()
    expect(studio.context.session.dirty).toBe(true)
    expect(studio.context.session.undoDepth).toBe(1)
    const before = workers.at(-1).posted.length
    b2Key({ key: 'z', [modifier]: true })
    expect(workers.at(-1).posted.slice(before)).toMatchObject([{ type: 'loadDocument', documentId: 'one.dxf' }])
    expect(studio.context.session.redoDepth).toBe(1)
    loaded()
    expect(studio.context.session.dirty).toBe(false)
    selectLine(studio)
    b2Key({ key: 'Delete' })
    applied()
    await act(async () => { await studio.context.session.actions.save() })
    expect(save).toHaveBeenCalledTimes(1)
    expect(studio.context.session.dirty).toBe(false)
    expect(studio.context.session.undoDepth).toBe(1)
    bodyFocus()
    b2Key({ key: 'z', [modifier]: true })
    expect(workers.at(-1).posted.at(-1).type).toBe('loadDocument')
    expect(edits()).toHaveLength(2)
  })

  it.each(['ctrlKey', 'metaKey'])('B2-03 %s redo aliases each load exactly one engine snapshot', async (modifier) => {
    const studio = await b2Studio()
    selectLine(studio)
    b2Key({ key: 'Delete' })
    applied()
    for (const alias of [{ key: 'y' }, { key: 'z', shiftKey: true }]) {
      b2Key({ key: 'z', [modifier]: true })
      loaded()
      const before = workers.at(-1).posted.length
      b2Key({ ...alias, [modifier]: true })
      expect(workers.at(-1).posted.slice(before)).toMatchObject([{ type: 'loadDocument', documentId: 'one.dxf' }])
      loaded([])
    }
    expect(edits()).toHaveLength(1)
    expect(studio.context.session.redoDepth).toBe(0)
  })

  it.each(['ctrlKey', 'metaKey'])('B2-04 %s Copy uses the session clipboard without geometry copy or history', async (modifier) => {
    const studio = await b2Studio()
    selectLine(studio)
    const before = [...workers.at(-1).posted]
    b2Key({ key: 'c', [modifier]: true })
    expect(studio.context.session.clipboard).not.toBeNull()
    expect(studio.context.session.entities).toContainEqual(expect.objectContaining({ id: 'e1' }))
    expect(workers.at(-1).posted).toEqual(before)
    expect(studio.context.session.undoDepth).toBe(0)
    expect(studio.context.session.dirty).toBe(false)
  })

  it.each(['ctrlKey', 'metaKey'])('B2-05 %s Cut captures geometry before posting one delete', async (modifier) => {
    const studio = await b2Studio()
    selectLine(studio)
    b2Key({ key: 'x', [modifier]: true })
    const clipboard = studio.context.session.clipboard
    expect(clipboard).not.toBeNull()
    expect(edits()).toEqual([{ type: 'applyEdit', op: 'delete', payload: { entityId: 'e1' } }])
    applied()
    expect(studio.context.session.entities).toEqual([])
    expect(studio.context.session.clipboard).toBe(clipboard)
    expect(studio.context.session.undoDepth).toBe(1)
  })

  it.each(['ctrlKey', 'metaKey'])('B2-06 %s Paste arms a point prompt and Run posts one paste', async (modifier) => {
    const studio = await b2Studio()
    selectLine(studio)
    b2Key({ key: 'c', [modifier]: true })
    act(() => studio.context.session.actions.selectClear())
    b2Key({ key: 'v', [modifier]: true })
    expect(studio.context.armed).toEqual({ group: 'clipboard', op: 'pasteClip' })
    expect(edits()).toHaveLength(0)
    point('10,20')
    fireEvent.click(screen.getByTestId('cockpit-prompt-run'))
    expect(edits()).toHaveLength(1)
    expect(edits()[0]).toMatchObject({ type: 'applyEdit', op: 'createLine', payload: { x1: 10, y1: 20, x2: 11, y2: 21 } })
    workers.at(-1).emit({ type: 'editApplied', op: 'createLine', ok: true, createdId: 'e2', entities: [LINE, { ...LINE, id: 'e2' }], entityCount: 2, bytes: new Uint8Array([48, 10]), byteLength: 2 })
    expect(studio.context.session.undoDepth).toBe(1)
  })

  it('B2-07 Enter rearms the accepted LINE without a chain point or immediate edit', async () => {
    const studio = await b2Studio()
    rememberLine(studio)
    expect(studio.context.lastArmedCommand).toEqual({ group: 'draw', op: 'createLine' })
    studio.rerender(false)
    studio.rerender(true)
    bodyFocus()
    expect(b2Key({ key: 'Enter' }).defaultPrevented).toBe(true)
    expect(studio.context.armed).toEqual({ group: 'draw', op: 'createLine' })
    expect(screen.getByTestId('cockpit-active-ask').textContent).toBe('LINE  Specify first point:')
    expect(edits()).toHaveLength(0)
  })

  it('B2-08 Space repeats without page scroll or an immediate edit', async () => {
    const studio = await b2Studio()
    rememberLine(studio)
    expect(b2Key({ key: ' ' }).defaultPrevented).toBe(true)
    expect(studio.context.armed).toEqual({ group: 'draw', op: 'createLine' })
    expect(edits()).toHaveLength(0)
  })

  it.each([
    ['B2-09', 'INSERT', MODIFY_REASONS.unsupportedInsert],
    ['B2-10', 'DIMENSION', MODIFY_REASONS.unsupportedDimension],
  ])('%s Copy and Cut announce the exact placed entity registry reason', async (id, type, reason) => {
    const studio = await b2Studio([{ ...LINE, type, editable: false }])
    selectLine(studio)
    for (const modifier of ['ctrlKey', 'metaKey']) {
      for (const key of ['c', 'x']) {
        expect(b2Key({ key, [modifier]: true }).defaultPrevented).toBe(true)
        expect(screen.getByRole('status').textContent).toBe(reason)
        expect(studio.context.session.clipboard).toBeNull()
        expect(edits()).toHaveLength(0)
      }
    }
  })

  it.each([
    ['B2-11', ['e1', 'e2'], MODIFY_REASONS.multiSelection],
    ['B2-12', [], MODIFY_REASONS.noSelection],
  ])('%s erase and clipboard shortcuts retain the ribbon selection gate', async (id, selection, reason) => {
    const studio = await b2Studio([LINE, { ...LINE, id: 'e2' }])
    act(() => studio.context.session.actions.selectReplace(selection))
    for (const spec of B2_KEYS.filter(({ key }) => ['Delete', 'c', 'x'].includes(key))) {
      expect(b2Key(spec).defaultPrevented).toBe(true)
      expect(screen.getByRole('status').textContent).toBe(reason)
    }
    expect(edits()).toHaveLength(0)
    expect(studio.context.session.clipboard).toBeNull()
  })

  it('B2-13 Paste with an empty clipboard announces its registry reason', async () => {
    const studio = await b2Studio()
    for (const modifier of ['ctrlKey', 'metaKey']) {
      b2Key({ key: 'v', [modifier]: true })
      expect(screen.getByRole('status').textContent).toBe(CLIPBOARD_REASONS.empty)
    }
    expect(studio.context.armed).toBeNull()
    expect(edits()).toHaveLength(0)
  })

  it('B2-14 empty engine history announces the matching reason for every alias', async () => {
    await b2Studio()
    const before = [...workers.at(-1).posted]
    for (const modifier of ['ctrlKey', 'metaKey']) {
      for (const spec of [{ key: 'z' }, { key: 'y' }, { key: 'z', shiftKey: true }]) {
        b2Key({ ...spec, [modifier]: true })
        expect(screen.getByRole('status').textContent).toBe(spec.key === 'z' && !spec.shiftKey ? REASONS.nothingToUndo : REASONS.nothingToRedo)
      }
    }
    expect(workers.at(-1).posted).toEqual(before)
  })

  it('B2-15 busy blocks every engine shortcut before clipboard or history changes', async () => {
    const studio = await b2Studio()
    rememberLine(studio)
    selectLine(studio)
    b2Key({ key: 'c', ctrlKey: true })
    b2Key({ key: 'Delete' })
    const before = [...workers.at(-1).posted]
    const clipboard = studio.context.session.clipboard
    for (const spec of B2_KEYS) {
      expect(b2Key(spec).defaultPrevented).toBe(true)
      expect(screen.getByRole('status').textContent).toBe(DRAW_REASONS.busy)
    }
    expect(workers.at(-1).posted).toEqual(before)
    expect(studio.context.session.clipboard).toBe(clipboard)
    expect(studio.context.armed).toBeNull()
  })

  it.each(['text', 'search', 'number', 'range', 'checkbox', 'radio', 'file', 'password', 'email', 'date', 'color', 'button', 'submit', 'reset', 'url', 'tel', 'time', 'datetime-local', 'month', 'week', 'hidden', 'image'])('B2-16 every shortcut yields to input type %s and the empty command bar', async (type) => {
    const studio = await b2Studio()
    rememberLine(studio)
    selectLine(studio)
    b2Key({ key: 'c', ctrlKey: true })
    const input = document.createElement('input')
    input.type = type
    studio.canvas.append(input)
    act(() => input.focus())
    assertUnchanged(studio, B2_KEYS, input)
    if (document.activeElement === input) assertUnchanged(studio)
    act(() => studio.bar.focus())
    assertUnchanged(studio, B2_KEYS, studio.bar)
    assertUnchanged(studio)
  })

  it('B2-17 textarea keeps text selection clipboard and history defaults', async () => {
    const studio = await b2Studio()
    rememberLine(studio)
    selectLine(studio)
    b2Key({ key: 'c', ctrlKey: true })
    const textarea = document.createElement('textarea')
    textarea.value = 'selected text'
    studio.canvas.append(textarea)
    act(() => { textarea.focus(); textarea.select() })
    assertUnchanged(studio, B2_KEYS, textarea)
    assertUnchanged(studio)
    expect(textarea.selectionStart).toBe(0)
    expect(textarea.selectionEnd).toBe(textarea.value.length)
  })

  it('B2-18 select retains its native keys', async () => {
    const studio = await b2Studio()
    rememberLine(studio)
    selectLine(studio)
    b2Key({ key: 'c', metaKey: true })
    const select = document.createElement('select')
    studio.canvas.append(select)
    act(() => select.focus())
    assertUnchanged(studio, B2_KEYS, select)
    assertUnchanged(studio)
  })

  it('B2-19 descendants of editors exclude both event and active focus', async () => {
    const studio = await b2Studio()
    rememberLine(studio)
    selectLine(studio)
    b2Key({ key: 'c', ctrlKey: true })
    for (const kind of ['contenteditable', 'textbox', 'searchbox', 'combobox', 'spinbutton']) {
      const owner = document.createElement('div')
      if (kind === 'contenteditable') owner.setAttribute('contenteditable', 'true')
      else owner.setAttribute('role', kind)
      const child = document.createElement('span')
      child.tabIndex = 0
      owner.append(child)
      studio.canvas.append(owner)
      act(() => child.focus())
      assertUnchanged(studio, B2_KEYS, child)
      assertUnchanged(studio)
      owner.remove()
    }
    const editor = document.createElement('div')
    editor.tabIndex = 0
    Object.defineProperty(editor, 'isContentEditable', { value: true })
    studio.canvas.append(editor)
    expect(editor.hasAttribute('contenteditable')).toBe(false)
    act(() => editor.focus())
    expect(document.activeElement).toBe(editor)
    assertUnchanged(studio, B2_KEYS, editor)
    assertUnchanged(studio)
  })

  it('B2-20 visible owners and the phone drawer block keys while focus stays outside', async () => {
    const studio = await b2Studio()
    selectLine(studio)
    for (const markup of ['<div role="dialog"></div>', '<div aria-modal="true"></div>', '<div class="drawer-layer"></div>', '<div data-escape-owner></div>', '<dialog open></dialog>', '<div class="resolver" role="listbox"></div>']) {
      const parent = document.createElement('div')
      parent.innerHTML = markup
      studio.canvas.append(parent)
      assertUnchanged(studio)
      parent.hidden = true
      expect(b2Key({ key: 'c', ctrlKey: true }).defaultPrevented).toBe(true)
      parent.remove()
    }
    const app = studio.canvas.closest('.app')
    app.dataset.drawer = 'nav'
    const opener = document.createElement('button')
    app.append(opener)
    act(() => opener.focus())
    assertUnchanged(studio)
    app.dataset.drawer = 'none'
    bodyFocus()
    for (const attribute of ['hidden', 'inert', 'aria-hidden']) {
      const parent = document.createElement('div')
      parent.setAttribute(attribute, attribute === 'aria-hidden' ? 'true' : '')
      parent.innerHTML = '<div role="dialog"></div>'
      app.append(parent)
      expect(b2Key({ key: 'c', metaKey: true }).defaultPrevented).toBe(true)
      parent.remove()
    }
    for (const style of ['display: none', 'visibility: hidden']) {
      const parent = document.createElement('div')
      parent.setAttribute('style', style)
      parent.innerHTML = '<div data-escape-owner></div>'
      app.append(parent)
      expect(b2Key({ key: 'c', ctrlKey: true }).defaultPrevented).toBe(true)
      parent.remove()
    }
    const closed = document.createElement('dialog')
    app.append(closed)
    expect(b2Key({ key: 'c', ctrlKey: true }).defaultPrevented).toBe(true)
    expect(edits()).toHaveLength(0)
  })

  it('B2-21 Alt yields every binding without dispatch or prevented defaults', async () => {
    const studio = await b2Studio()
    selectLine(studio)
    assertUnchanged(studio, B2_KEYS.map((spec) => ({ ...spec, altKey: true })))
  })

  it('B2-22 held Delete never dispatches a second erase after busy clears', async () => {
    const studio = await b2Studio()
    rememberLine(studio)
    selectLine(studio)
    const held = [
      { key: 'Delete', repeat: true },
      ...['ctrlKey', 'metaKey'].flatMap((modifier) => ['z', 'x'].map((key) => ({ key, [modifier]: true, repeat: true }))),
      { key: 'Enter', repeat: true },
    ]
    b2Key({ key: 'Delete' })
    assertUnchanged(studio, held)
    applied('delete', [LINE])
    selectLine(studio)
    expect(studio.context.session.busy).toBe(false)
    expect(studio.context.session.undoDepth).toBe(1)
    expect(studio.context.lastArmedCommand).toEqual({ group: 'draw', op: 'createLine' })
    assertUnchanged(studio, held)
    expect(edits()).toHaveLength(1)
  })

  it('B2-23 hidden mismatched closed crashed and unmounted documents produce no shortcut effect', async () => {
    const studio = await b2Studio()
    rememberLine(studio)
    selectLine(studio)
    for (const attribute of ['hidden', 'inert', 'aria-hidden']) {
      const parent = studio.canvas.parentElement
      parent.setAttribute(attribute, attribute === 'aria-hidden' ? 'true' : '')
      assertUnchanged(studio)
      parent.removeAttribute(attribute)
    }
    for (const style of ['display: none', 'visibility: hidden']) {
      studio.canvas.parentElement.setAttribute('style', style)
      assertUnchanged(studio)
      studio.canvas.parentElement.removeAttribute('style')
    }
    studio.canvas.dataset.engineDocument = 'another.dxf'
    assertUnchanged(studio)
    studio.canvas.dataset.engineDocument = 'one.dxf'
    const remove = vi.spyOn(window, 'removeEventListener')
    act(() => workers.at(-1).listeners.get('error')?.({ message: 'worker stopped', preventDefault() {} }))
    expect(studio.context.session.errorKind).toBe('crashed')
    expect(studio.context.lastArmedCommand).toBeNull()
    expect(remove.mock.calls.some(([type]) => type === 'keydown')).toBe(true)
    assertUnchanged(studio)
    act(() => studio.context.session.actions.reset())
    assertUnchanged(studio)
    studio.view.unmount()
    for (const spec of B2_KEYS) expect(b2Key(spec).defaultPrevented).toBe(false)
  })

  it('B2-24 nonempty command text or a live arm retains the current prompt and inputs', async () => {
    const studio = await b2Studio()
    rememberLine(studio)
    studio.bar.value = ' LINE '
    assertUnchanged(studio, [{ key: 'Enter' }, { key: ' ' }])
    studio.bar.value = ''
    act(() => studio.context.setArmed({ group: 'draw', op: 'createCircle' }))
    act(() => studio.context.setInput('r', '7'))
    bodyFocus()
    assertUnchanged(studio, [{ key: 'Enter' }, { key: ' ' }])
    expect(studio.context.inputs.r).toBe('7')
  })

  it('B2-25 no accepted prompted command gives the frozen bounded repeat reason', async () => {
    let permit = false
    const studio = await b2Studio([LINE], { onBeforeArm: () => permit })
    act(() => studio.context.setArmed({ group: 'draw', op: 'createLine' }))
    permit = true
    act(() => studio.context.setArmed({ group: 'draw', op: 'madeUp' }))
    act(() => studio.context.setArmed(null))
    selectLine(studio)
    b2Key({ key: 'c', ctrlKey: true })
    bodyFocus()
    for (const key of ['Enter', ' ']) {
      expect(b2Key({ key }).defaultPrevented).toBe(true)
      expect(screen.getByRole('status').textContent).toBe(REPEAT_REASONS.empty)
    }
    expect(studio.context.lastArmedCommand).toBeNull()
    expect(studio.context.armed).toBeNull()
    expect(edits()).toHaveLength(0)
  })

  it('B2-26 prompt focus after ribbon click and first point excludes synthetic window keys', async () => {
    const studio = await b2Studio()
    selectLine(studio)
    fireEvent.click(document.querySelector('[data-tool="draw:createLine"]'))
    const first = screen.getByLabelText('ribbon x')
    expect(document.activeElement).toBe(first)
    assertUnchanged(studio, B2_KEYS.filter(({ key }) => ['Delete', 'c', 'x', 'z'].includes(key)))
    point('0,0')
    act(() => window.dispatchEvent(new CustomEvent('cockpit:focus-step', { detail: {} })))
    expect(document.activeElement).toBe(screen.getByLabelText('ribbon x2'))
    assertUnchanged(studio, B2_KEYS.filter(({ key }) => ['Delete', 'c', 'x', 'z'].includes(key)))
    expect(document.activeElement).toBe(screen.getByLabelText('ribbon x2'))
  })

  it('B2-27 activation controls keep Enter and Space and Run performs only its existing edit', async () => {
    const studio = await b2Studio()
    rememberLine(studio)
    for (const markup of ['<button>Activate</button>', '<a href="#">Link</a>', '<details><summary>More</summary></details>', '<div role="tab" tabindex="0">Tab</div>', '<div role="option" tabindex="0">Option</div>', '<div role="menuitem" tabindex="0">Item</div>']) {
      const parent = document.createElement('div')
      parent.innerHTML = markup
      studio.canvas.append(parent)
      const control = parent.querySelector('summary') || parent.firstElementChild
      act(() => control.focus())
      expect(document.activeElement).toBe(control)
      assertUnchanged(studio, [{ key: 'Enter' }, { key: ' ' }], control)
      assertUnchanged(studio, [{ key: 'Enter' }, { key: ' ' }])
      parent.remove()
    }
    const ribbonButton = document.querySelector('[data-tool="draw:createLine"]')
    act(() => ribbonButton.focus())
    expect(document.activeElement).toBe(ribbonButton)
    assertUnchanged(studio, [{ key: 'Enter' }, { key: ' ' }], ribbonButton)
    assertUnchanged(studio, [{ key: 'Enter' }, { key: ' ' }])
    command(parseDrawingCommand('CIRCLE'))
    const cancel = screen.getByRole('button', { name: 'Cancel', exact: true })
    act(() => cancel.focus())
    expect(document.activeElement).toBe(cancel)
    for (const key of ['Enter', ' ']) expect(b2Key({ key }, cancel).defaultPrevented).toBe(false)
    fireEvent.click(cancel)
    expect(studio.context.armed).toBeNull()
    expect(edits()).toHaveLength(0)
    command(parseDrawingCommand('CIRCLE'))
    point('0,0')
    point('5')
    const run = screen.getByTestId('cockpit-prompt-run')
    act(() => run.focus())
    expect(document.activeElement).toBe(run)
    for (const key of ['Enter', ' ']) expect(b2Key({ key }, run).defaultPrevented).toBe(false)
    expect(edits()).toHaveLength(0)
    fireEvent.click(run)
    expect(edits()).toHaveLength(1)
    expect(edits()[0].op).toBe('createCircle')
  })

  it('B2-28 repeat rechecks the remembered command against the current selection', async () => {
    const studio = await b2Studio()
    selectLine(studio)
    act(() => studio.context.setArmed({ group: 'modify', op: 'move' }))
    act(() => { studio.context.setArmed(null); studio.context.session.actions.selectClear() })
    bodyFocus()
    for (const key of ['Enter', ' ']) {
      b2Key({ key })
      expect(screen.getByRole('status').textContent).toBe(MODIFY_REASONS.noSelection)
      expect(studio.context.armed).toBeNull()
    }
    expect(edits()).toHaveLength(0)
  })

  it('B2-29 replacement and reload discard the old remembered command', async () => {
    const studio = await b2Studio()
    rememberLine(studio)
    await reloadDrawing(studio, 'two.dxf')
    expect(studio.context.lastArmedCommand).toBeNull()
    b2Key({ key: 'Enter' })
    expect(screen.getByRole('status').textContent).toBe(REPEAT_REASONS.empty)
    rememberLine(studio)
    await reloadDrawing(studio, 'two.dxf')
    expect(studio.context.lastArmedCommand).toBeNull()
    b2Key({ key: ' ' })
    expect(screen.getByRole('status').textContent).toBe(REPEAT_REASONS.empty)
    expect(edits()).toHaveLength(0)
  })

  it('B2-30 StrictMode rerenders and ribbon remounts retain one balanced key subscription', async () => {
    const add = vi.spyOn(window, 'addEventListener')
    const remove = vi.spyOn(window, 'removeEventListener')
    const studio = await b2Studio([LINE], { strict: true })
    const keyAdds = () => add.mock.calls.filter(([type, , capture]) => type === 'keydown' && !capture)
    const keyRemoves = () => remove.mock.calls.filter(([type, , capture]) => type === 'keydown' && !capture)
    const liveKeys = () => keyAdds().length - keyRemoves().length
    const subscribed = keyAdds().length
    expect(liveKeys()).toBe(1)
    selectLine(studio)
    for (let i = 0; i < 3; i += 1) {
      act(() => studio.context.setInput('layer', String(i)))
      studio.rerender(false)
      expect(liveKeys()).toBe(1)
      studio.rerender(true)
      expect(liveKeys()).toBe(1)
    }
    expect(keyAdds()).toHaveLength(subscribed)
    bodyFocus()
    b2Key({ key: 'Delete' })
    expect(edits()).toHaveLength(1)
    studio.view.unmount()
    expect(liveKeys()).toBe(0)
    for (const [, listener] of keyAdds()) {
      expect(remove.mock.calls.some(([type, removed, capture]) => type === 'keydown' && removed === listener && !capture)).toBe(true)
    }
  })

  it('B2-35 visible menus own shortcuts with focus inside or outside and hidden menus yield', async () => {
    const studio = await b2Studio()
    rememberLine(studio)
    selectLine(studio)
    const actions = studio.context.session.actions
    const spies = ['applyEdit', 'undo', 'redo', 'copyToClipboard', 'pasteFromClipboard']
      .map((name) => vi.spyOn(actions, name))
    spies.push(vi.spyOn(studio.context, 'setArmed'))
    const menu = document.createElement('div')
    menu.setAttribute('role', 'menu')
    menu.tabIndex = 0
    studio.canvas.append(menu)
    const specs = [{ key: 'Delete' }, { key: 'x', ctrlKey: true }, { key: 'x', metaKey: true }, { key: 'Enter' }]
    for (const inside of [true, false]) {
      if (inside) {
        act(() => menu.focus())
        expect(document.activeElement).toBe(menu)
      } else bodyFocus()
      assertUnchanged(studio, specs, inside ? menu : document.body)
      assertUnchanged(studio, specs)
      for (const spy of spies) expect(spy).not.toHaveBeenCalled()
    }
    menu.hidden = true
    bodyFocus()
    expect(b2Key({ key: 'Delete' }).defaultPrevented).toBe(true)
    expect(spies[0]).toHaveBeenCalledTimes(1)
    expect(edits()).toEqual([{ type: 'applyEdit', op: 'delete', payload: { entityId: 'e1' } }])
  })

  it('B2-31 real route decision owns Enter while repeat yields and Delete still works', async () => {
    const studio = await b2Studio()
    rememberLine(studio)
    const onOpenAuthor = vi.fn()
    const route = render(<RoutePanel route={{ lane: 'build', confidence: 1 }} tools={[]} onOpenAuthor={onOpenAuthor} />)
    vi.spyOn(performance, 'now').mockReturnValue(Number.MAX_SAFE_INTEGER)
    b2Key({ key: 'Enter' })
    expect(onOpenAuthor).toHaveBeenCalledTimes(1)
    expect(studio.context.armed).toBeNull()
    expect(b2Key({ key: ' ' }).defaultPrevented).toBe(false)
    selectLine(studio)
    b2Key({ key: 'Delete' })
    expect(edits()).toHaveLength(1)
    route.unmount()
  })

  it('B2-32 prevented composing and composing keycode events yield all shortcuts', async () => {
    const studio = await b2Studio()
    selectLine(studio)
    assertUnchanged(studio, B2_KEYS.map((spec) => ({ ...spec, isComposing: true })))
    assertUnchanged(studio, B2_KEYS.map((spec) => ({ ...spec, keyCode: 229 })))
    const before = [...workers.at(-1).posted]
    for (const spec of B2_KEYS) {
      const event = new KeyboardEvent('keydown', { ...spec, cancelable: true })
      event.preventDefault()
      act(() => window.dispatchEvent(event))
    }
    expect(workers.at(-1).posted).toEqual(before)
    expect(studio.context.session.clipboard).toBeNull()
  })

  it('B2-33 Escape F3 F8 Mod+K and Ctrl+J remain outside the engine bindings', async () => {
    const studio = await b2Studio()
    assertUnchanged(studio, [
      { key: 'Escape' }, { key: 'F3' }, { key: 'F8' },
      { key: 'k', ctrlKey: true }, { key: 'k', metaKey: true }, { key: 'j', ctrlKey: true },
    ])
    command(parseDrawingCommand('LINE'))
    act(() => screen.getByLabelText('ribbon x').focus())
    b2Key({ key: 'Escape' }, screen.getByLabelText('ribbon x'))
    expect(studio.context.armed).toBeNull()
    command(parseDrawingCommand('LINE'))
    bodyFocus()
    b2Key({ key: 'Escape' })
    expect(studio.context.armed).toBeNull()
    expect(studio.context.lastArmedCommand).toEqual({ group: 'draw', op: 'createLine' })
    expect(edits()).toHaveLength(0)
  })
})
afterEach(() => { cleanup(); vi.restoreAllMocks() })

describe('CommandLineArmer (W4f slice B)', () => {
  it('typed UNDO clears a preceding REDO refusal when history is available', async () => {
    const studio = mount()
    await openAndLoad()
    command(parseDrawingCommand('LINE'))
    point('0,0')
    point('10,0')
    workers[0].emit({ type: 'editApplied', op: 'createLine', ok: true, createdId: 'e2', entities: [LINE, { ...LINE, id: 'e2' }], entityCount: 2, bytes: new Uint8Array([48, 10]), byteLength: 2 })
    expect(studio.context.session.undoDepth).toBe(1)
    expect(studio.context.session.redoDepth).toBe(0)
    const undo = vi.spyOn(studio.context.session.actions, 'undo')
    command(parseDrawingCommand('REDO'))
    expect(screen.getByRole('status').textContent).toBe('REDO is unavailable (nothing to redo).')
    command(parseDrawingCommand('UNDO'))
    expect(undo).toHaveBeenCalledTimes(1)
    expect(studio.context.session.busy).toBe(true)
    expect(screen.getByRole('status').textContent).not.toContain('REDO is unavailable')
  })

  it.each([
    ['U', 'undo', 'UNDO is unavailable (nothing to undo).'],
    ['REDO', 'redo', 'REDO is unavailable (nothing to redo).'],
  ])('typed %s reports an empty history without calling the action', async (word, op, sentence) => {
    const studio = mount()
    const action = vi.spyOn(studio.context.session.actions, op)
    await openAndLoad()
    expect(studio.context.session[`${op}Depth`]).toBe(0)
    command(parseDrawingCommand(word))
    expect(screen.getByRole('status').textContent).toBe(sentence)
    expect(action).not.toHaveBeenCalled()
  })

  it('typed undo without a parsed document reports the document reason', () => {
    const studio = mount()
    const undo = vi.spyOn(studio.context.session.actions, 'undo')
    act(() => studio.context.setInput('x', '1'))
    command(parseDrawingCommand('U'))
    expect(screen.getByRole('status').textContent).toBe('UNDO is unavailable (no drawing in the browser engine yet).')
    expect(undo).not.toHaveBeenCalled()
  })

  it('typed undo while busy reports the busy reason without calling the action', async () => {
    const studio = mount()
    const undo = vi.spyOn(studio.context.session.actions, 'undo')
    await openAndLoad()
    command(parseDrawingCommand('LINE'))
    point('0,0')
    point('10,0')
    expect(studio.context.session.busy).toBe(true)
    command(parseDrawingCommand('U'))
    expect(screen.getByRole('status').textContent).toBe('UNDO is unavailable (engine busy: wait for the current edit).')
    expect(undo).not.toHaveBeenCalled()
  })

  it.each([true, false])('typed undo with history handles action result %s', async (result) => {
    const studio = mount()
    const undo = vi.spyOn(studio.context.session.actions, 'undo').mockReturnValue(result)
    await openAndLoad()
    command(parseDrawingCommand('LINE'))
    point('0,0')
    point('10,0')
    workers[0].emit({ type: 'editApplied', op: 'createLine', ok: true, createdId: 'e2', entities: [LINE, { ...LINE, id: 'e2' }], entityCount: 2, bytes: new Uint8Array([48, 10]), byteLength: 2 })
    expect(studio.context.session.undoDepth).toBe(1)
    const status = screen.getByRole('status').textContent
    command(parseDrawingCommand('U'))
    expect(undo).toHaveBeenCalledTimes(1)
    if (result) {
      expect(screen.getByRole('status').textContent).toBe(status)
      expect(screen.getByRole('status').textContent).not.toContain('UNDO is unavailable')
    } else {
      expect(screen.getByRole('status').textContent).toBe('UNDO is unavailable (engine busy: wait for the current edit).')
    }
  })

  it.each([false, true])('keeps the endpoint cursor after a worker refusal from body focus with picker=%s', async (withPicker) => {
    vi.spyOn(window, 'requestAnimationFrame').mockImplementation((cb) => { cb(); return 0 })
    const ground = document.createElement('div')
    mount(withPicker ? { ground, viewerRef: { current: {} } } : null)
    await openAndLoad()
    command(parseDrawingCommand('LINE'))
    point('0,0')
    point('10,0')
    expect(workers[0].posted.at(-1)).toMatchObject({ type: 'applyEdit', payload: { x1: 0, y1: 0, x2: 10, y2: 0 } })
    act(() => {
      const tabIndex = document.body.getAttribute('tabindex')
      document.body.setAttribute('tabindex', '-1')
      document.body.focus()
      if (tabIndex === null) document.body.removeAttribute('tabindex')
      else document.body.setAttribute('tabindex', tabIndex)
    })
    expect(document.activeElement).toBe(document.body)
    workers[0].emit({ type: 'editApplied', op: 'createLine', ok: false, reason: 'worker rejected segment' })
    expect(screen.getByLabelText('ribbon x2')).toHaveFocus()
    let state
    window.addEventListener('cockpit:armed', (event) => { state = event.detail }, { once: true })
    act(() => window.dispatchEvent(new CustomEvent('cockpit:armed-request')))
    expect(state).toMatchObject({ op: 'createLine', step: 1 })
    expect(screen.getByTestId('cockpit-active-ask').textContent).toBe('LINE  Specify next point:')
    point('20,0')
    expect(screen.getByLabelText('ribbon x').value).toBe('0')
    expect(screen.getByLabelText('ribbon y').value).toBe('0')
    expect(workers[0].posted.filter((message) => message.type === 'applyEdit')).toHaveLength(2)
    expect(workers[0].posted.at(-1)).toMatchObject({ type: 'applyEdit', payload: { x1: 0, y1: 0, x2: 20, y2: 0 } })
  })

  it.each([false, true])('keeps a refused LINE endpoint correctable with picker=%s', async (withPicker) => {
    const ground = document.createElement('div')
    mount(withPicker ? { ground, viewerRef: { current: {} } } : null)
    await openAndLoad()
    command(parseDrawingCommand('LINE'))
    const layer = screen.getByLabelText('ribbon layer').value
    point('0,0')
    point('0,0')
    expect(workers[0].posted.filter((m) => m.type === 'applyEdit')).toHaveLength(0)
    expect(screen.getByTestId('cockpit-active-ask').textContent).toBe('LINE  Specify next point:')
    expect(screen.getByTestId('cockpit-prompt-note').textContent).toContain('refused')
    point('10,0')
    expect(screen.getByLabelText('ribbon layer').value).toBe(layer)
    expect(workers[0].posted.at(-1)).toMatchObject({ type: 'applyEdit', op: 'createLine', payload: { x1: 0, y1: 0, x2: 10, y2: 0 } })
  })

  it.each(['bar/click/bar', 'click/bar/bar'])('chains two segments through one point step: %s', async (order) => {
    const ground = document.createElement('div')
    const viewer = { unproject: (x, y) => ({ x, y }), setRubberBand: vi.fn() }
    vi.spyOn(window, 'requestAnimationFrame').mockImplementation((cb) => { cb(); return 0 })
    mount({ ground, viewerRef: { current: viewer } })
    await openAndLoad()
    act(() => window.dispatchEvent(new KeyboardEvent('keydown', { key: 'F3' })))
    const click = (x, y) => act(() => {
      ground.dispatchEvent(new MouseEvent('pointerdown', { clientX: x, clientY: y, button: 0 }))
      ground.dispatchEvent(new MouseEvent('pointerup', { clientX: x, clientY: y, button: 0 }))
    })
    command(parseDrawingCommand('LINE'))
    if (order === 'bar/click/bar') { point('5,5'); click(10, 0) }
    else { click(5, 5); point('10,0') }
    expect(workers[0].posted.filter((m) => m.type === 'applyEdit')).toHaveLength(1)
    expect(workers[0].posted.at(-1)).toMatchObject({ payload: { x1: 5, y1: 5, x2: 10, y2: 0 } })
    workers[0].emit({ type: 'editApplied', op: 'createLine', ok: true, createdId: 'e2', entities: [LINE, { ...LINE, id: 'e2', vertices: [[5, 5], [10, 0]] }], entityCount: 2 })
    point('20,0')
    expect(workers[0].posted.filter((m) => m.type === 'applyEdit')).toHaveLength(2)
    expect(workers[0].posted.at(-1)).toMatchObject({ payload: { x1: 10, y1: 0, x2: 20, y2: 0 } })
    expect(screen.getByLabelText('ribbon layer').value).toBe('')
  })

  it('rewinds a corrected CIRCLE centre through the picker and reanchors the radius ghost', async () => {
    const ground = document.createElement('div')
    const viewer = { unproject: (x, y) => ({ x, y }), setRubberBand: vi.fn() }
    vi.spyOn(window, 'requestAnimationFrame').mockImplementation((cb) => { cb(); return 0 })
    mount({ ground, viewerRef: { current: viewer } })
    await openAndLoad()
    act(() => window.dispatchEvent(new KeyboardEvent('keydown', { key: 'F3' })))
    const click = (x, y) => act(() => {
      ground.dispatchEvent(new MouseEvent('pointerdown', { clientX: x, clientY: y, button: 0 }))
      ground.dispatchEvent(new MouseEvent('pointerup', { clientX: x, clientY: y, button: 0 }))
    })
    command(parseDrawingCommand('CIRCLE'))
    click(5, 5)
    act(() => screen.getByLabelText('ribbon x').focus())
    point('10,0')
    expect(screen.getByLabelText('ribbon x').value).toBe('10')
    expect(screen.getByLabelText('ribbon y').value).toBe('0')
    expect(screen.getByTestId('cockpit-active-ask').textContent).toBe('CIRCLE  Specify radius:')
    act(() => ground.dispatchEvent(new MouseEvent('pointermove', { clientX: 10, clientY: 10 })))
    const [ghost, closed] = viewer.setRubberBand.mock.calls.at(-1)
    expect(closed).toBe(true)
    expect(ghost[0]).toEqual([20, 0])
    expect(ghost[24][0]).toBeCloseTo(0)
    expect(ghost[24][1]).toBeCloseTo(0)
    click(10, 10)
    expect(screen.getByLabelText('ribbon r').value).toBe('10')
  })

  it('refuses a bar point for a later picker step without writing its fields', async () => {
    mount({ ground: document.createElement('div'), viewerRef: { current: {} } })
    await openAndLoad()
    command(parseDrawingCommand('LINE'))
    const x2 = screen.getByLabelText('ribbon x2').value
    const y2 = screen.getByLabelText('ribbon y2').value
    act(() => screen.getByLabelText('ribbon x2').focus())
    point('20,0')
    expect(screen.getByLabelText('ribbon x2').value).toBe(x2)
    expect(screen.getByLabelText('ribbon y2').value).toBe(y2)
    expect(screen.getByRole('status').textContent).toContain('Start a drawing command before entering a point.')
    expect(workers[0].posted.filter((message) => message.type === 'applyEdit')).toHaveLength(0)
  })

  it.each([false, true])('bounds the typed polar text before resolving it from the anchor with picker=%s', async (withPicker) => {
    mount(withPicker ? { ground: document.createElement('div'), viewerRef: { current: {} } } : null)
    await openAndLoad()
    command(parseDrawingCommand('LINE'))
    point('0,0')
    const raw = `${'0'.repeat(60)}1<90`
    expect(raw).toHaveLength(64)
    point(`0${raw}`)
    expect(screen.getByRole('status').textContent).toContain('is not a point: use x,y, @dx,dy, dist<angle or @dist<angle.')
    expect(workers[0].posted.filter((message) => message.type === 'applyEdit')).toHaveLength(0)
    point(raw)
    expect(screen.getByLabelText('ribbon x2').value).toBe('0')
    expect(screen.getByLabelText('ribbon y2').value).toBe('1')
    expect(workers[0].posted.at(-1)).toMatchObject({ type: 'applyEdit', payload: { x1: 0, y1: 0, x2: 0, y2: 1 } })
  })

  it('takes absolute, relative and polar points through the armed operand and publishes its live ask', async () => {
    mount()
    await openAndLoad()
    expect(point('0,0')).toBe(false)
    command(parseDrawingCommand('LINE'))
    expect(screen.getByTestId('cockpit-active-ask').textContent).toBe('LINE  Specify first point:')
    expect(point('@10,0')).toBe(true)
    expect(screen.getByRole('status').textContent).toContain('needs a previous point')
    expect(screen.getByLabelText('ribbon x').value).toBe('@10,0')
    expect(point('0,0')).toBe(true)
    expect(screen.getByTestId('cockpit-active-ask').textContent).toBe('LINE  Specify next point:')
    expect(point('@10,0')).toBe(true)
    expect(screen.getByLabelText('ribbon x2').value).toBe('10')
    expect(screen.getByLabelText('ribbon y2').value).toBe('0')
    expect(workers[0].posted.at(-1)).toMatchObject({ type: 'applyEdit', op: 'createLine', payload: { x1: 0, y1: 0, x2: 10, y2: 0 } })
    workers[0].emit({ type: 'editApplied', op: 'createLine', ok: true, createdId: 'e2', entities: [LINE, { ...LINE, id: 'e2', vertices: [[0, 0], [10, 0]] }], entityCount: 2 })
    expect(screen.getByTestId('cockpit-active-ask').textContent).toBe('LINE  Specify next point:')
    expect(point('10<90')).toBe(true)
    expect(screen.getByLabelText('ribbon x2').value).toBe('10')
    expect(screen.getByLabelText('ribbon y2').value).toBe('10')
    expect(workers[0].posted.at(-1)).toMatchObject({ type: 'applyEdit', op: 'createLine', payload: { x1: 10, y1: 0, x2: 10, y2: 10 } })
  })

  it('keeps a point typed for a scalar and names the field without advancing', async () => {
    mount()
    await openAndLoad()
    command(parseDrawingCommand('CIRCLE'))
    point('0,0')
    expect(point('10,10')).toBe(true)
    expect(screen.getByLabelText('ribbon r').value).toBe('10,10')
    expect(screen.getByRole('status').textContent).toContain('r needs a scalar')
    expect(screen.getByTestId('cockpit-active-ask').textContent).toBe('CIRCLE  Specify radius:')
    expect(screen.getByTestId('cockpit-prompt-run')).toBeDisabled()
  })

  it('a draw word arms its prompt like the ribbon click; a second word re-arms; malformed details are ignored', async () => {
    mount()
    await openAndLoad()
    expect(promptEl()).toBeNull()
    command({ group: 'draw', op: 'createLine' })
    expect(promptEl().getAttribute('data-op')).toBe('createLine')
    expect(promptEl().textContent).toContain('LINE')
    command({ group: 'draw', op: 'createCircle' })
    expect(promptEl().getAttribute('data-op')).toBe('createCircle')
    for (const bad of [null, 'createLine', { group: 'draw' }, { group: 'nope', op: 'createLine' }, { group: 'draw', op: 'format' }, { group: 'draw', op: 'constructor' }]) {
      command(bad)
      expect(promptEl().getAttribute('data-op')).toBe('createCircle')
    }
    expect(workers[0].posted.filter((m) => m.type === 'applyEdit')).toHaveLength(0)
  })

  it('ERASE runs at once on a live selection and does nothing without one', async () => {
    mount()
    await openAndLoad()
    const before = workers[0].posted.length
    command({ group: 'modify', op: 'delete' })
    expect(workers[0].posted.length).toBe(before)
    expect(promptEl()).toBeNull()
    fireEvent.click(screen.getByRole('radio'))
    command({ group: 'modify', op: 'delete' })
    const posted = workers[0].posted
    expect(posted[posted.length - 1]).toEqual({ type: 'applyEdit', op: 'delete', payload: { entityId: 'e1' } })
  })

  it('C2: typed x runs EXPLODE at once on a selected LWPOLYLINE', async () => {
    mount()
    await openAndLoad([{ ...LINE, type: 'LWPOLYLINE', vertices: [[0, 0], [1, 0], [1, 1]] }])
    fireEvent.click(screen.getByRole('radio'))
    const before = workers[0].posted.length
    command(parseDrawingCommand('x'))
    expect(workers[0].posted.slice(before)).toEqual([{ type: 'applyEdit', op: 'explode', payload: { entityId: 'e1' } }])
    expect(promptEl()).toBeNull()
  })

  it.each([
    ['INSERT', 'an INSERT is placed, not edited, in this round'],
    ['DIMENSION', 'a dimension is placed, not edited, in this round'],
  ])('C2: typed explode on %s surfaces the placed-kind refusal without posting', async (type, sentence) => {
    mount()
    await openAndLoad([{ ...LINE, type, editable: false }])
    fireEvent.click(screen.getByRole('radio'))
    const before = workers[0].posted.length
    command(parseDrawingCommand('explode'))
    expect(screen.getByRole('status').textContent).toBe(sentence)
    expect(workers[0].posted).toHaveLength(before)
    expect(promptEl()).toBeNull()
  })

  it('C2: typed x without a selection surfaces the ladder sentence and arms nothing', async () => {
    mount()
    await openAndLoad()
    const before = workers[0].posted.length
    command(parseDrawingCommand('x'))
    expect(screen.getByRole('status').textContent).toBe('select an entity in the drawing')
    expect(workers[0].posted).toHaveLength(before)
    expect(promptEl()).toBeNull()
  })

  it('F4: typed m arms MOVE on INSERT and typed erase posts delete', async () => {
    mount()
    await openAndLoad([{ id: '11', type: 'INSERT', name: 'Fixture', ip: [0, 0, 0], rotationDeg: 0, scale: [1, 1, 1], layer: '0', editable: false }])
    fireEvent.click(screen.getByRole('radio'))
    const before = workers[0].posted.length
    command(parseDrawingCommand('m'))
    expect(promptEl().getAttribute('data-op')).toBe('move')
    expect(screen.getByTestId('cockpit-prompt-note').textContent).toBe('an INSERT is placed, not edited, in this round')
    expect(workers[0].posted).toHaveLength(before)
    command(parseDrawingCommand('erase'))
    expect(workers[0].posted.slice(before)).toEqual([{ type: 'applyEdit', op: 'delete', payload: { entityId: '11' } }])
  })

  // W4g-5c: the clipboard words. kimi on #1025 found them registered as words
  // and dropped here, because this gate knew two groups; the ribbon arm had
  // the same defect one layer down. Both layers are pinned now.
  it('PASTECLIP arms the PASTE prompt; COPYCLIP copies without touching the engine; CUTCLIP posts the delete', async () => {
    mount()
    await openAndLoad()
    command({ group: 'clipboard', op: 'pasteClip' })
    expect(promptEl()).not.toBeNull()
    expect(promptEl().getAttribute('data-op')).toBe('pasteClip')
    expect(promptEl().textContent).toContain('PASTE')
    // Nothing copied yet: the prompt reads the clipboard ladder and holds Run.
    expect(promptEl().textContent).toContain('nothing on the clipboard yet')
    fireEvent.click(screen.getByRole('radio'))
    const before = workers[0].posted.length
    command({ group: 'clipboard', op: 'copyClip' })
    expect(workers[0].posted.length).toBe(before)
    expect(screen.getByRole('status').textContent).toContain('is on the clipboard')
    command({ group: 'clipboard', op: 'cutClip' })
    const posted = workers[0].posted
    expect(posted[posted.length - 1]).toEqual({ type: 'applyEdit', op: 'delete', payload: { entityId: 'e1' } })
  })

  it.each(['g', 'GROUP', 'UNGROUP'])('%s arms the real Groups prompt without posting an edit', async (word) => {
    mount()
    await openAndLoad()
    const detail = parseDrawingCommand(word)
    const op = word === 'UNGROUP' ? 'ungroup' : 'group'
    expect(detail).toMatchObject({ group: 'groups', op })
    expect(byId(`groups:${op}`)).toMatchObject({ surface: 'engine', group: 'groups', panel: 'groups', op })
    const before = workers[0].posted.length
    command(detail)
    expect(promptEl().getAttribute('data-op')).toBe(op)
    expect(promptEl().textContent).toContain(op === 'group' ? 'Select objects to add:' : 'Enter group name:')
    expect(workers[0].posted).toHaveLength(before)
  })

  it('LEADER arms the live createMleader prompt', async () => {
    mount()
    await openAndLoad()
    expect(promptEl()).toBeNull()
    command(parseDrawingCommand('LEADER'))
    expect(promptEl().getAttribute('data-op')).toBe('createMleader')
    // A mismatched reason (never emitted by the real parser, but the gate
    // must fail closed against it anyway) is dropped, same as any malformed detail.
    command({ group: 'deferred', op: 'leader', reason: 'a made-up sentence' })
    expect(promptEl().getAttribute('data-op')).toBe('createMleader')
  })

  it('acceptsCommand is the fail-closed gate', () => {
    expect(acceptsCommand({ group: 'groups', op: 'group' })).toBe(true)
    expect(acceptsCommand({ group: 'groups', op: 'ungroup' })).toBe(true)
    expect(acceptsCommand({ group: 'deferred', op: 'group' })).toBe(false)
    expect(acceptsCommand({ group: 'clipboard', op: 'pasteClip' })).toBe(true)
    expect(acceptsCommand({ group: 'clipboard', op: 'copyClip' })).toBe(true)
    expect(acceptsCommand({ group: 'clipboard', op: 'cutClip' })).toBe(true)
    expect(acceptsCommand({ group: 'annotation', op: 'text' })).toBe(false)
    expect(acceptsCommand({ group: 'draw', op: 'createArc' })).toBe(true)
    expect(acceptsCommand({ group: 'modify', op: 'delete' })).toBe(true)
    expect(acceptsCommand({ group: 'modify', op: 'hasOwnProperty' })).toBe(false)
    expect(acceptsCommand({ group: 'draw', op: 'delete' })).toBe(true)
    expect(acceptsCommand({ group: 'modify', op: 'undo' })).toBe(true)
    expect(acceptsCommand({ group: 'modify', op: 'redo' })).toBe(true)
    expect(acceptsCommand({ group: 'view', op: 'fit' })).toBe(false)
    expect(acceptsCommand(undefined)).toBe(false)
  })
})
