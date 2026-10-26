// @vitest-environment jsdom
import { useEffect, useState } from 'react'
import { readFileSync } from 'node:fs'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import JobInbox, { peekNoticeVersionView, requestNoticeVersionView, takeNoticeVersionView } from './JobInbox.jsx'
import ProjectSwitcher from './ProjectSwitcher.jsx'
import ToolsPanel from './ToolsPanel.jsx'
import SchemaForm from './SchemaForm.jsx'
import ShortcutSheet from './ShortcutSheet.jsx'
import { createNotificationBus } from '../lib/notifications.js'
import { moveRovingTab } from '../lib/roving.js'
import { DrawingIdentityProvider, useDrawingIdentity } from '../drawing/DrawingIdentityProvider.jsx'

afterEach(cleanup)

// Exercise the production handoff with the real per-mode identity owner.
const appSource = readFileSync(`${process.cwd()}/src/App.jsx`, 'utf8').replace(/\r\n/g, '\n')
const noticeEffectStart = appSource.indexOf('  useEffect(() => {', appSource.indexOf('  const versionViewAction ='))
  + '  useEffect(() => {\n'.length
const noticeEffectEnd = appSource.indexOf('\n  }, [', noticeEffectStart)
const runNoticeEffect = new Function('context', 'with (context) {\n'
  + appSource.slice(noticeEffectStart, noticeEffectEnd) + '\n}')
const noticeActionStart = appSource.indexOf('useCallback(', appSource.indexOf('  const versionViewAction ='))
  + 'useCallback('.length
const noticeActionEnd = appSource.indexOf('\n  }, [', noticeActionStart) + '\n  }'.length
const makeNoticeViewAction = new Function('context', 'with (context) { return ('
  + appSource.slice(noticeActionStart, noticeActionEnd) + ') }')

it('S1-68 retained View stays enabled on another console drawing and opens its recorded version', () => {
  const before = window.location.href
  const preview = vi.fn(), show = vi.fn()
  const bus = createNotificationBus()
  let identity
  const action = makeNoticeViewAction({
    noticeMountedRef: { current: true }, noticeTargetRef: { current: { drawingId: 'demo' } },
    noticeViewRef: { current: { preview, show } },
    isScopeCurrent: () => identity.isScopeCurrent(), requestNoticeVersionView,
    selectNoticeDrawing: id => identity.selectNoticeDrawing(id),
  })('roof', 2)
  bus.push({ text: 'Roof version saved', action })
  function Probe({ drawingState, versionBusy }) {
    identity = useDrawingIdentity()
    useEffect(() => {
      runNoticeEffect({ peekNoticeVersionView, takeNoticeVersionView,
        REQUESTED_DRAWING_ID: identity.drawingId, selectNoticeDrawing: identity.selectNoticeDrawing,
        drawingState, versionBusy, onPreviewVersion: preview, viewViewer: show })
    }, [identity, drawingState, versionBusy])
    return <JobInbox bus={bus} />
  }
  const tree = (drawingState = { drawing_id: 'demo' }, versionBusy = false) => (
    <DrawingIdentityProvider mode="console" search="" publicDemo={false} liveDemo={false}
      readAuthToken={() => null}>
      <Probe drawingState={drawingState} versionBusy={versionBusy} />
    </DrawingIdentityProvider>
  )
  try {
    const view = render(tree())
    expect(identity.drawingId).toBe('demo')
    const button = screen.getByRole('button', { name: 'View' })
    expect(button).toBeEnabled()
    fireEvent.click(button)
    expect(identity).toMatchObject({ drawingId: 'roof', source: 'roof', origin: 'stored' })
    expect(preview).not.toHaveBeenCalled()
    expect(peekNoticeVersionView()).toMatchObject({ drawingId: 'roof', version: 2 })
    view.rerender(tree({ drawing_id: 'roof' }, true))
    expect(preview).not.toHaveBeenCalled()
    view.rerender(tree({ drawing_id: 'roof' }))
    expect(preview).toHaveBeenCalledExactlyOnceWith(2)
    expect(show).toHaveBeenCalledOnce()
    expect(peekNoticeVersionView()).toBeNull()
  } finally {
    takeNoticeVersionView('roof')
    window.history.replaceState({}, '', before)
  }
})

it('S1-68 View selects the operator roof from the demo console and previews v2 once ready', () => {
  const before = window.location.href
  const preview = vi.fn(), show = vi.fn(), consoleIds = []
  let identity
  function Probe({ mode, drawingState, versionBusy }) {
    identity = useDrawingIdentity()
    if (mode === 'console') consoleIds.push(identity.drawingId)
    useEffect(() => {
      if (mode !== 'console') return
      runNoticeEffect({ peekNoticeVersionView, takeNoticeVersionView,
        REQUESTED_DRAWING_ID: identity.drawingId, selectNoticeDrawing: identity.selectNoticeDrawing,
        drawingState, versionBusy, onPreviewVersion: preview, viewViewer: show })
    }, [mode, identity, drawingState, versionBusy])
    return null
  }
  const tree = (mode, drawingState = { drawing_id: 'demo' }, versionBusy = false) => (
    <DrawingIdentityProvider mode={mode} search="" publicDemo={false} liveDemo={false}
      readLiveDrawingId={() => 'roof'} readAuthToken={() => null}>
      <Probe mode={mode} drawingState={drawingState} versionBusy={versionBusy} />
    </DrawingIdentityProvider>
  )
  try {
    const view = render(tree('operator'))
    expect(identity.drawingId).toBe('roof')
    requestNoticeVersionView('roof', 2, identity.isScopeCurrent)
    view.rerender(tree('console'))
    expect(consoleIds[0]).toBe('demo')
    expect(identity).toMatchObject({ drawingId: 'roof', source: 'roof', origin: 'stored' })
    expect(preview).not.toHaveBeenCalled()
    expect(peekNoticeVersionView()).toMatchObject({ drawingId: 'roof', version: 2 })
    view.rerender(tree('console', { drawing_id: 'roof' }, true))
    expect(preview).not.toHaveBeenCalled()
    view.rerender(tree('console', { drawing_id: 'roof' }))
    expect(preview).toHaveBeenCalledExactlyOnceWith(2)
    expect(show).toHaveBeenCalledOnce()
    expect(peekNoticeVersionView()).toBeNull()
    view.rerender(tree('console', { drawing_id: 'roof' }))
    expect(preview).toHaveBeenCalledOnce()
  } finally {
    takeNoticeVersionView('roof')
    window.history.replaceState({}, '', before)
  }
})

it('S1-68 an expired selector cannot reopen a drawing after a scope reset', () => {
  let identity
  function Probe() {
    identity = useDrawingIdentity()
    return null
  }
  render(<DrawingIdentityProvider mode="console" search="" publicDemo={false} liveDemo={false}
    readAuthToken={() => null}><Probe /></DrawingIdentityProvider>)
  const select = identity.selectNoticeDrawing
  act(() => { identity.resetAll() })
  act(() => { expect(select('roof')).toBeNull() })
  expect(identity).toMatchObject({ drawingId: null, source: null, origin: 'reset' })
})

it('S1-68 carries View across scenes until the original drawing mounts', () => {
  const before = window.location.href
  try {
    requestNoticeVersionView('roof', 2)
    expect(window.location.pathname).toBe('/app')
    expect(peekNoticeVersionView()).toMatchObject({ drawingId: 'roof', version: 2 })
    expect(peekNoticeVersionView()).toMatchObject({ drawingId: 'roof', version: 2 })
    expect(takeNoticeVersionView('other')).toBeNull()
    expect(takeNoticeVersionView('roof')).toMatchObject({ drawingId: 'roof', version: 2 })
    expect(takeNoticeVersionView('roof')).toBeNull()
  } finally { window.history.replaceState({}, '', before) }
})

it('S1-68 drops a pending View when its drawing scope expires', () => {
  const before = window.location.href
  let current = true
  try {
    requestNoticeVersionView('roof', 2, () => current)
    current = false
    expect(peekNoticeVersionView()).toBeNull()
    expect(takeNoticeVersionView('roof')).toBeNull()
  } finally { window.history.replaceState({}, '', before) }
})

it('S1-68 preserves the supplied action label', () => {
  const bus = createNotificationBus()
  bus.push({ text: 'Version saved', action: { label: 'Undo', onClick: vi.fn() } })
  render(<JobInbox bus={bus} />)
  expect(screen.getByRole('button', { name: 'Undo' })).toBeEnabled()
  expect(screen.queryByRole('button', { name: /Retry/ })).toBeNull()
})

it('S1-68 explains and disables a stale action', () => {
  const bus = createNotificationBus()
  const onClick = vi.fn()
  bus.push({ text: 'Version saved', action: { label: 'Undo', onClick,
    disabledReason: () => 'A newer version is open.' } })
  render(<JobInbox bus={bus} />)
  fireEvent.click(screen.getByRole('button', { name: 'Undo' }))
  expect(onClick).not.toHaveBeenCalled()
  expect(screen.getByRole('button', { name: 'Undo' })).toBeDisabled()
  expect(screen.getByText('A newer version is open.')).toBeInTheDocument()
})

const projects = [{ project_id: 'one', name: 'Roof one' }, { project_id: 'two', name: 'Roof two' }]

it('S1-69 selects the highlighted project from the focused trigger', () => {
  const onOpenProject = vi.fn()
  render(<ProjectSwitcher orgId="org" projects={projects} onOpenProject={onOpenProject} />)
  const trigger = screen.getByRole('button', { name: /Projects: change project/ })
  trigger.focus()
  fireEvent.click(trigger)
  fireEvent.keyDown(trigger, { key: 'ArrowDown' })
  fireEvent.keyDown(trigger, { key: 'Enter' })
  expect(onOpenProject).toHaveBeenCalledWith('two')
  expect(trigger).toHaveFocus()
})

it('S1-69 restores trigger focus after choosing a project row', () => {
  render(<ProjectSwitcher orgId="org" projects={projects} onOpenProject={vi.fn()} />)
  const trigger = screen.getByRole('button', { name: /Projects: change project/ })
  fireEvent.click(trigger)
  const row = screen.getByRole('menuitem', { name: /Roof two/ })
  row.focus()
  fireEvent.click(row)
  expect(trigger).toHaveFocus()
})

const tool = { name: 'Place panels', description: 'Place panels on the roof', capabilities: [],
  params: { type: 'object', required: ['count'], properties: { count: { type: 'integer' } } } }

it('S1-78 connects each tool disclosure to its form', () => {
  render(<ToolsPanel tools={[tool]} onRequestRun={vi.fn()} />)
  const header = screen.getByRole('button', { name: /Place panels/ })
  expect(header).toHaveAttribute('aria-expanded', 'false')
  fireEvent.click(header)
  expect(header).toHaveAttribute('aria-expanded', 'true')
  expect(document.getElementById(header.getAttribute('aria-controls'))).toContainElement(screen.getByRole('spinbutton'))
})

it('S1-79 requires a value before review and blocks clearing it again', () => {
  render(<ToolsPanel tools={[tool]} onRequestRun={vi.fn()} />)
  fireEvent.click(screen.getByRole('button', { name: /Place panels/ }))
  const run = screen.getByRole('button', { name: 'Review & run' })
  expect(run).toBeDisabled()
  expect(screen.getByText('Count is required.')).toBeInTheDocument()
  fireEvent.change(screen.getByRole('spinbutton'), { target: { value: '0' } })
  expect(run).toBeEnabled()
  fireEvent.change(screen.getByRole('spinbutton'), { target: { value: '' } })
  expect(run).toBeDisabled()
})

it('S1-79 reports an absent required selection inline', () => {
  const valid = vi.fn()
  render(<SchemaForm schema={{ required: ['side'], properties: { side: { type: 'string', enum: ['South'] } } }}
    values={{}} onChange={vi.fn()} onValidityChange={valid} />)
  expect(screen.getByRole('alert')).toHaveTextContent('Side is required.')
  expect(valid).toHaveBeenLastCalledWith(false)
})

it('S1-130 skips disabled and inert tabs in both directions and at the ends', () => {
  render(<div role="tablist" onKeyDown={moveRovingTab}>
    <button role="tab" disabled>Unavailable first</button>
    <button role="tab">Draw</button><button role="tab" disabled>Model</button>
    <button role="tab">Insert</button><button role="tab" aria-disabled="true">Unavailable last</button>
    <span inert=""><button role="tab">Inert</button></span>
  </div>)
  const draw = screen.getByRole('tab', { name: 'Draw' })
  const insert = screen.getByRole('tab', { name: 'Insert' })
  draw.focus()
  fireEvent.keyDown(draw, { key: 'ArrowRight' })
  expect(insert).toHaveFocus()
  fireEvent.keyDown(insert, { key: 'ArrowLeft' })
  expect(draw).toHaveFocus()
  fireEvent.keyDown(draw, { key: 'End' })
  expect(insert).toHaveFocus()
  fireEvent.keyDown(insert, { key: 'Home' })
  expect(draw).toHaveFocus()
})

it('S1-199 focuses the shortcut switch on open and restores the opener on Escape', () => {
  const onClose = vi.fn()
  const { rerender } = render(<><button>Help</button><ShortcutSheet open={false} onClose={onClose} /></>)
  const opener = screen.getByRole('button', { name: 'Help' })
  opener.focus()
  rerender(<><button>Help</button><ShortcutSheet open onClose={onClose} /></>)
  expect(screen.getByRole('switch')).toHaveFocus()
  fireEvent.keyDown(screen.getByRole('switch'), { key: 'Escape' })
  expect(opener).toHaveFocus()
  expect(onClose).toHaveBeenCalledOnce()
})

it('S1-199 restores the opener after clicking nonfocusable content outside the sheet', () => {
  function Harness() {
    const [open, setOpen] = useState(false)
    return <>
      <button onClick={() => setOpen(true)}>Help</button>
      <p>Drawing workspace</p>
      <ShortcutSheet open={open} onClose={() => setOpen(false)} />
    </>
  }
  render(<Harness />)
  const opener = screen.getByRole('button', { name: 'Help' })
  opener.focus()
  fireEvent.click(opener)
  expect(screen.getByRole('switch')).toHaveFocus()
  fireEvent.mouseDown(screen.getByText('Drawing workspace'))
  expect(screen.queryByRole('dialog', { name: 'Keyboard shortcuts' })).toBeNull()
  expect(opener).toHaveFocus()
})

it('S1-201 offers a live projects retry without demo reassurance', () => {
  const retry = vi.fn()
  render(<ProjectSwitcher orgId="org" unavailable="Projects are unavailable." projectName="Roof"
    onLoadProjects={retry} />)
  fireEvent.click(screen.getByRole('button', { name: /Projects: change project/ }))
  expect(screen.queryByText(/The demo keeps working/)).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: 'Retry projects' }))
  expect(retry).toHaveBeenCalledOnce()
})
