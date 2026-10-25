// @vitest-environment jsdom
import { useState } from 'react'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import JobInbox, { requestNoticeVersionView, takeNoticeVersionView } from './JobInbox.jsx'
import ProjectSwitcher from './ProjectSwitcher.jsx'
import ToolsPanel from './ToolsPanel.jsx'
import SchemaForm from './SchemaForm.jsx'
import ShortcutSheet from './ShortcutSheet.jsx'
import { createNotificationBus } from '../lib/notifications.js'
import { moveRovingTab } from '../lib/roving.js'

afterEach(cleanup)

it('S1-68 carries View across scenes until the original drawing mounts', () => {
  const before = window.location.href
  try {
    requestNoticeVersionView('roof', 2)
    expect(window.location.pathname).toBe('/app')
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
