// @vitest-environment jsdom
import { useState } from 'react'
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import ShortcutSheet from './ShortcutSheet.jsx'
import { keyboardTable } from '../lib/actionRegistry.js'
import { SINGLE_KEY_SHORTCUTS_KEY } from '../lib/singleKeyPreference.js'

vi.mock('../lib/actionRegistry.js', () => ({ keyboardTable: vi.fn() }))

if (!HTMLDialogElement.prototype.showModal) {
  HTMLDialogElement.prototype.showModal = function () { this.open = true }
}
if (!HTMLDialogElement.prototype.close) {
  HTMLDialogElement.prototype.close = function () { this.open = false }
}

const registryRows = [
  { id: 'fixture:command', label: 'Focus fixture command', kbd: 'Mod+K', context: 'Anywhere', singleKey: false },
  { id: 'fixture:retry', label: 'Retry fixture action', kbd: 'R', context: 'Outside text fields', singleKey: true },
  { id: 'fixture:help', label: 'Show fixture shortcuts', kbd: '?', context: 'Outside text fields', singleKey: true },
]

afterEach(() => {
  cleanup()
  localStorage.removeItem(SINGLE_KEY_SHORTCUTS_KEY)
})

function SheetHarness() {
  const [open, setOpen] = useState(false)
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>Open shortcuts</button>
      <ShortcutSheet open={open} onClose={() => setOpen(false)} />
    </>
  )
}

function openSheet() {
  keyboardTable.mockReturnValue(registryRows)
  render(<SheetHarness />)
  const opener = screen.getByRole('button', { name: 'Open shortcuts' })
  opener.focus()
  fireEvent.click(opener)
  return opener
}

describe('ShortcutSheet design parity', () => {
  it('renders exactly the registry rows in two groups with individual keycaps', () => {
    openSheet()
    const dialog = screen.getByRole('dialog', { name: 'Keyboard shortcuts' })
    expect(dialog.tagName).toBe('DIALOG')
    expect(dialog.open).toBe(true)
    const rows = within(dialog).getAllByTestId('shortcut-row')
    expect(rows).toHaveLength(registryRows.length)
    expect(rows.map((row) => row.querySelector('.label').textContent).sort())
      .toEqual(registryRows.map((row) => row.label).sort())
    expect(within(dialog).getAllByRole('heading').map((heading) => heading.textContent))
      .toEqual(['Shortcuts', 'Single keys'])

    const shortcuts = screen.getByRole('region', { name: 'Shortcuts' })
    const modRow = within(shortcuts).getByText(registryRows[0].label).closest('[data-shortcut-id]')
    expect(Array.from(modRow.querySelectorAll('kbd.dp-keycap'), (key) => key.textContent)).toEqual(['Mod', 'K'])
    const singleKeys = screen.getByRole('region', { name: 'Single keys' })
    expect(within(singleKeys).getByText(registryRows[1].label)).toBeTruthy()
    expect(within(singleKeys).getByText(registryRows[2].label)).toBeTruthy()
    expect(within(singleKeys).getByRole('switch', { name: 'Single-key shortcuts' })).toBeTruthy()
    expect(within(dialog).getByText('Sheet anchors and receipts: no shortcut index yet')).toBeTruthy()
  })

  it('disables only single-key rows while keeping modifier shortcuts enabled', () => {
    openSheet()
    const toggle = screen.getByRole('switch', { name: 'Single-key shortcuts' })
    fireEvent.click(toggle)
    expect(toggle.getAttribute('aria-checked')).toBe('false')
    expect(localStorage.getItem(SINGLE_KEY_SHORTCUTS_KEY)).toBe('off')
    const rows = screen.getAllByTestId('shortcut-row')
    expect(rows).toHaveLength(registryRows.length)
    for (const entry of registryRows) {
      const row = rows.find((element) => element.getAttribute('data-shortcut-id') === entry.id)
      expect(row.getAttribute('aria-disabled')).toBe(entry.singleKey ? 'true' : null)
    }
  })

  it('Escape closes the native dialog and restores focus after single keys are disabled', () => {
    const opener = openSheet()
    const dialog = screen.getByRole('dialog', { name: 'Keyboard shortcuts' })
    const toggle = screen.getByRole('switch', { name: 'Single-key shortcuts' })
    fireEvent.click(toggle)
    expect(document.activeElement).toBe(toggle)
    fireEvent.keyDown(toggle, { key: 'Escape' })
    expect(dialog.open).toBe(false)
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(document.activeElement).toBe(opener)
  })

  it('handles native cancellation and restores the opener on every opening', () => {
    const opener = openSheet()
    fireEvent(screen.getByRole('dialog'), new Event('cancel', { cancelable: true }))
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(document.activeElement).toBe(opener)
    fireEvent.click(opener)
    fireEvent.click(screen.getByRole('button', { name: 'Close' }))
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(document.activeElement).toBe(opener)
  })

  it.each(['Escape', 'outside mousedown', 'controlled close'])('supports %s without native dialog methods', (dismissal) => {
    const prototype = HTMLDialogElement.prototype
    const descriptors = Object.getOwnPropertyDescriptors(prototype)
    Object.defineProperties(prototype, {
      showModal: { configurable: true, value: undefined },
      close: { configurable: true, value: undefined },
    })
    try {
      keyboardTable.mockReturnValue(registryRows)
      const onClose = vi.fn()
      const view = (open) => <>
        <button type="button">Help</button>
        <p>Drawing workspace</p>
        <ShortcutSheet open={open} onClose={onClose} />
      </>
      const { rerender } = render(view(false))
      const opener = screen.getByRole('button', { name: 'Help' })
      opener.focus()
      rerender(view(true))
      const dialog = screen.getByRole('dialog', { name: 'Keyboard shortcuts' })
      const toggle = screen.getByRole('switch', { name: 'Single-key shortcuts' })
      expect(dialog.hasAttribute('open')).toBe(true)
      expect(document.activeElement).toBe(toggle)

      if (dismissal === 'Escape') fireEvent.keyDown(toggle, { key: 'Escape' })
      else if (dismissal === 'outside mousedown') fireEvent.mouseDown(screen.getByText('Drawing workspace'))
      else rerender(view(false))

      expect(dialog.hasAttribute('open')).toBe(false)
      expect(onClose).toHaveBeenCalledTimes(dismissal === 'controlled close' ? 0 : 1)
      expect(document.activeElement).toBe(opener)
    } finally {
      cleanup()
      Object.defineProperties(prototype, {
        showModal: descriptors.showModal,
        close: descriptors.close,
      })
    }
  })
})
