// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { byId, engineShortcutDecision, ladderDecision, ladderListener } from './actionRegistry.js'
import CockpitTopBand from '../site/CockpitTopBand.jsx'

afterEach(() => {
  document.getSelection()?.removeAllRanges()
  cleanup()
})

const canvas = {
  canvasShown: true,
  commandLineEmpty: true,
  session: { engineParsed: true, selected: { type: 'LINE', editable: true } },
}

describe('w3 keyboard regressions', () => {
  it('S1-131 leaves copy, cut and paste to selected page text for either modifier', () => {
    render(<p role="alert">The drawing could not be opened.</p>)
    const message = screen.getByRole('alert')
    const range = document.createRange()
    range.selectNodeContents(message)
    document.getSelection().addRange(range)
    for (const modifier of ['ctrlKey', 'metaKey']) {
      for (const key of ['c', 'x', 'v']) {
        expect(engineShortcutDecision({ key, [modifier]: true, target: message }, canvas)).toBeNull()
      }
    }
    document.getSelection().collapseToEnd()
    expect(engineShortcutDecision({ key: 'c', ctrlKey: true, target: message }, canvas)).toBe('clipboard:copyClip')
  })

  it('S1-137 routes Backspace to the same delete action as Delete outside fields', () => {
    const onActivate = vi.fn()
    const ctx = { ...canvas, onActivate }
    const id = engineShortcutDecision({ key: 'Backspace', target: document.body }, ctx)
    expect(id).toBe('modify:delete')
    expect(id).toBe(engineShortcutDecision({ key: 'Delete', target: document.body }, ctx))
    expect(byId(id).when(ctx)).toBe('')
    byId(id).run(ctx)
    expect(onActivate).toHaveBeenCalledWith('modify', 'delete')
    render(<input aria-label="Project name" />)
    expect(engineShortcutDecision({ key: 'Backspace', target: screen.getByRole('textbox') }, ctx)).toBeNull()
  })

  it('S1-196 protects projects and runs in fields, and selections in text editors', () => {
    render(<><input aria-label="Command" /><textarea aria-label="Notes" />
      <select aria-label="Layer"><option>Roof</option></select>
      <div contentEditable suppressContentEditableWarning role="textbox"><span>Editable note</span></div></>)
    const targets = [...screen.getAllByRole('textbox'), screen.getByRole('combobox'), screen.getByText('Editable note')]
    for (const shell of [{ openProjectId: 'project' }, { running: true }, { selectedHandle: 'object' }]) {
      const onCloseProject = vi.fn()
      const onInterruptRun = vi.fn()
      const onClearSelection = vi.fn()
      const onKey = ladderListener(shell, (state) => ({ ...state, onCloseProject, onInterruptRun, onClearSelection }))
      for (const target of targets) {
        // Native selects retain the existing Escape-to-clear-selection gesture.
        if (shell.selectedHandle && target.tagName === 'SELECT') continue
        target.focus()
        const preventDefault = vi.fn()
        expect(ladderDecision({ key: 'Escape', target }, shell)).toBeNull()
        onKey({ key: 'Escape', target, preventDefault })
        expect(preventDefault).not.toHaveBeenCalled()
      }
      expect(onCloseProject).not.toHaveBeenCalled()
      expect(onInterruptRun).not.toHaveBeenCalled()
      expect(onClearSelection).not.toHaveBeenCalled()
    }
    expect(ladderDecision({ key: 'Escape', target: targets[0] }, { drawer: 'tools', openProjectId: 'project' }).rung).toBe('drawer')
    expect(ladderDecision({ key: 'Escape', target: document.body }, { openProjectId: 'project' }).rung).toBe('project')
  })

  it('S1-196 lets an unclaimed Escape in a native property select clear only the selection', () => {
    render(<select aria-label="Color"><option>By layer</option></select>)
    const target = screen.getByRole('combobox')
    target.focus()
    const shell = { selectedHandle: 'object', openProjectId: 'project' }
    const onClearSelection = vi.fn()
    const onCloseProject = vi.fn()
    const onKey = ladderListener(shell, (state) => ({ ...state, onClearSelection, onCloseProject }))
    const event = { key: 'Escape', target, preventDefault: vi.fn() }
    expect(ladderDecision(event, shell)).toMatchObject({ rung: 'selection' })
    onKey(event)
    expect(onClearSelection).toHaveBeenCalledTimes(1)
    expect(onCloseProject).not.toHaveBeenCalled()
    expect(event.preventDefault).not.toHaveBeenCalled()
    onKey({ ...event, defaultPrevented: true })
    expect(onClearSelection).toHaveBeenCalledTimes(1)
    expect(ladderDecision(event, { openProjectId: 'project' })).toBeNull()
  })

  it('S1-200 keeps bare R and Shift+? with a focused native select', () => {
    render(<select aria-label="Layer"><option>Roof</option></select>)
    const target = screen.getByRole('combobox')
    target.focus()
    const ctx = { singleKeyShortcuts: true, rTarget: 'route' }
    for (const event of [{ key: 'r' }, { key: 'R', shiftKey: true }, { key: '?', shiftKey: true }]) {
      expect(ladderDecision({ ...event, target }, ctx)).toBeNull()
    }
  })

  it('S1-195 leaves forward Tab native so header and product controls remain reachable', () => {
    render(<>
      <CockpitTopBand selected="project" tabs={[{ id: 'project', label: 'Project' }]} />
      <button>Sign in</button><button>Details</button>
      <div role="tablist" aria-label="Surfaces"><button role="tab" aria-selected="true">Design</button></div>
      <div id="drafting-ribbon" tabIndex={-1}><button>Draw a line</button></div>
    </>)
    const tab = screen.getByRole('tab', { name: 'Project' })
    tab.focus()
    // jsdom does not perform native Tab navigation. Check the event is left
    // uncancelled and focus is not moved past the intervening controls.
    expect(fireEvent.keyDown(tab, { key: 'Tab' })).toBe(true)
    expect(document.activeElement).toBe(tab)
    expect(screen.getByRole('button', { name: 'Sign in' }).tabIndex).toBe(0)
    expect(screen.getByRole('tab', { name: 'Design' }).tabIndex).toBe(0)
  })
})
