// @vitest-environment jsdom
//
// Standardization slice 13d: the "?" bar row and Shift+? both resolve to this
// ONE anchored panel (no new modal grammar). ShortcutSheet itself is
// GENERATED from the action registry's keyboardTable(), so this file pins the
// rendered shape rather than re-deriving it; actionRegistry.test.js already
// pins the table's own contents and the Shift+? ladder decision.
import { useEffect, useState } from 'react'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import ShortcutSheet, { DOC_LINKS } from './ShortcutSheet.jsx'
import { keyboardTable, ladderListener } from '../lib/actionRegistry.js'

afterEach(cleanup)

describe('ShortcutSheet', () => {
  it('renders nothing when closed', () => {
    const { container } = render(<ShortcutSheet open={false} onClose={() => {}} />)
    expect(container.querySelector('.shortcut-sheet')).toBeNull()
  })

  it('lists every keyboardTable() row, generated from the registry, no invented row', () => {
    render(<ShortcutSheet open onClose={() => {}} />)
    const rows = screen.getAllByTestId('shortcut-row')
    expect(rows).toHaveLength(keyboardTable().length)
    keyboardTable().forEach((row, i) => {
      expect(rows[i].querySelector('.label').textContent).toBe(row.label)
      expect(rows[i].querySelector('.key').textContent).toBe(row.kbd)
    })
  })

  it('Close button, Escape and an outside click all call onClose', () => {
    const onCloseButton = vi.fn()
    const { unmount } = render(<ShortcutSheet open onClose={onCloseButton} />)
    fireEvent.click(screen.getByText('Close'))
    expect(onCloseButton).toHaveBeenCalledTimes(1)
    unmount()

    const onCloseEscape = vi.fn()
    render(<ShortcutSheet open onClose={onCloseEscape} />)
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(onCloseEscape).toHaveBeenCalledTimes(1)
    cleanup()

    const onCloseOutside = vi.fn()
    render(
      <div>
        <div data-testid="elsewhere" />
        <ShortcutSheet open onClose={onCloseOutside} />
      </div>,
    )
    fireEvent.mouseDown(screen.getByTestId('elsewhere'))
    expect(onCloseOutside).toHaveBeenCalledTimes(1)
  })

  it('the Docs section links only to real files this repo ships, off its own GitHub origin', () => {
    render(<ShortcutSheet open onClose={() => {}} />)
    const links = screen.getAllByTestId('shortcut-doc-link')
    expect(links).toHaveLength(DOC_LINKS.length)
    for (const doc of DOC_LINKS) {
      expect(doc.href).toMatch(/^https:\/\/github\.com\/LEAF-Solar-Design\/leaf-web-demo\/blob\/main\/docs\//)
      const link = screen.getByText(doc.label).closest('a')
      expect(link.getAttribute('href')).toBe(doc.href)
      expect(link.getAttribute('target')).toBe('_blank')
      expect(link.getAttribute('rel')).toBe('noreferrer')
    }
  })
})

// Slice 13d: Shift+? through the REAL global ladder (actionRegistry's own
// ladderListener, not a re-implemented keydown handler) opening the REAL
// sheet — the same wiring App.jsx mounts (onOpenShortcuts -> setState ->
// <ShortcutSheet open>), proved here without mounting all of App.jsx.
function ShiftQuestionMarkHarness() {
  const [open, setOpen] = useState(false)
  useEffect(() => {
    const listener = ladderListener({}, () => ({ onOpenShortcuts: () => setOpen(true) }), () => {})
    window.addEventListener('keydown', listener)
    return () => window.removeEventListener('keydown', listener)
  }, [])
  return <ShortcutSheet open={open} onClose={() => setOpen(false)} />
}

describe('Shift+? opens the sheet through the real ladder', () => {
  it('is closed until the key fires, then renders the same generated rows', () => {
    render(<ShiftQuestionMarkHarness />)
    expect(screen.queryByRole('dialog')).toBeNull()
    fireEvent.keyDown(window, { key: '?' })
    expect(screen.getByRole('dialog', { name: 'Keyboard shortcuts' })).toBeTruthy()
    expect(screen.getAllByTestId('shortcut-row')).toHaveLength(keyboardTable().length)
  })

  it('leaves a "?" typed in a text field alone (the same guard R uses)', () => {
    render(
      <div>
        <input data-testid="field" />
        <ShiftQuestionMarkHarness />
      </div>,
    )
    fireEvent.keyDown(screen.getByTestId('field'), { key: '?' })
    expect(screen.queryByRole('dialog')).toBeNull()
  })
})
