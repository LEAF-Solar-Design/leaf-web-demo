// @vitest-environment jsdom
import { useState } from 'react'
import { readFileSync } from 'node:fs'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterAll, afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import DetailsDrawer from './DetailsDrawer.jsx'

// jsdom does not implement the native dialog lifecycle yet.
const polyfilled = []
for (const name of ['show', 'showModal', 'close']) {
  if (typeof HTMLDialogElement.prototype[name] === 'function') continue
  polyfilled.push(name)
  Object.defineProperty(HTMLDialogElement.prototype, name, {
    configurable: true,
    writable: true,
    value() {
      if (name === 'close') this.removeAttribute('open')
      else this.setAttribute('open', '')
    },
  })
}
afterAll(() => { polyfilled.forEach(name => { delete HTMLDialogElement.prototype[name] }) })

beforeEach(() => {
  vi.spyOn(HTMLDialogElement.prototype, 'show')
  vi.spyOn(HTMLDialogElement.prototype, 'showModal')
  vi.spyOn(HTMLDialogElement.prototype, 'close')
  vi.spyOn(HTMLElement.prototype, 'getClientRects').mockReturnValue([{ width: 10, height: 10 }])
})
afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

// Option A: design-parity-forks-20261101/receipts/operator-forks-all-a-20261101.json.
// The existing phone boundary stays at 600px; only the large phone detent is modal.
function viewport(width) {
  const listeners = new Set()
  const media = {
    matches: width < 600,
    addEventListener: (_, listener) => listeners.add(listener),
    removeEventListener: (_, listener) => listeners.delete(listener),
  }
  vi.stubGlobal('matchMedia', vi.fn(() => media))
  return nextWidth => act(() => {
    media.matches = nextWidth < 600
    listeners.forEach(listener => listener())
  })
}

const payload = { title: 'Activity details', rows: ['Provenance'], action: { label: 'Quiet action' } }

function Harness({ onClose, onKeyDown, invoker = true }) {
  const [open, setOpen] = useState(false)
  return <section aria-label="Events" onKeyDown={onKeyDown}>
    <h2>Events</h2>
    {invoker && <button onClick={() => setOpen(true)}>Open details</button>}
    <DetailsDrawer data={open ? payload : null} onClose={() => { onClose?.(); setOpen(false) }} />
  </section>
}

function openDetails() {
  const invoker = screen.getByRole('button', { name: 'Open details' })
  invoker.focus()
  fireEvent.click(invoker)
  return invoker
}

function tab(target, shiftKey = false) {
  const event = new KeyboardEvent('keydown', { key: 'Tab', shiftKey, bubbles: true, cancelable: true })
  fireEvent(target, event)
  return event.defaultPrevented
}

describe('DetailsDrawer native dialog parity', () => {
  it('uses show for medium, showModal for large, and show again when condensed', () => {
    viewport(390)
    render(<Harness />)
    openDetails()
    const dialog = screen.getByRole('dialog', { name: payload.title })
    expect(dialog.tagName).toBe('DIALOG')
    expect(dialog.open).toBe(true)
    expect(dialog.dataset.detent).toBe('medium')
    expect(dialog.hasAttribute('aria-modal')).toBe(false)
    expect(dialog.show).toHaveBeenCalledTimes(1)
    expect(dialog.showModal).not.toHaveBeenCalled()
    const last = screen.getByRole('button', { name: 'Quiet action' })
    last.focus()
    expect(tab(last)).toBe(false)

    fireEvent.click(screen.getByRole('button', { name: 'Expand details' }))
    expect(dialog.close).toHaveBeenCalledTimes(1)
    expect(dialog.showModal).toHaveBeenCalledTimes(1)
    expect(dialog.getAttribute('aria-modal')).toBe('true')
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Close details' }))
    last.focus()
    expect(tab(last)).toBe(true)
    const first = screen.getByRole('button', { name: 'Condense details' })
    expect(document.activeElement).toBe(first)
    expect(tab(first, true)).toBe(true)
    expect(document.activeElement).toBe(last)

    fireEvent.click(first)
    expect(dialog.close).toHaveBeenCalledTimes(2)
    expect(dialog.show).toHaveBeenCalledTimes(2)
    expect(dialog.hasAttribute('aria-modal')).toBe(false)
    last.focus()
    expect(tab(last)).toBe(false)
  })

  it.each([600, 1440])('opens desktop nonmodally without trapping Tab at %i px', width => {
    viewport(width)
    render(<Harness />)
    openDetails()
    const dialog = screen.getByRole('dialog', { name: payload.title })
    expect(dialog.show).toHaveBeenCalledTimes(1)
    expect(dialog.showModal).not.toHaveBeenCalled()
    expect(dialog.hasAttribute('aria-modal')).toBe(false)
    const last = screen.getByRole('button', { name: 'Quiet action' })
    last.focus()
    expect(tab(last)).toBe(false)
    const first = screen.getByRole('button', { name: 'Close details' })
    first.focus()
    expect(tab(first, true)).toBe(false)
  })

  it('switches a large phone dialog to nonmodal when entering desktop', () => {
    const resize = viewport(390)
    render(<Harness />)
    openDetails()
    const dialog = screen.getByRole('dialog', { name: payload.title })
    fireEvent.click(screen.getByRole('button', { name: 'Expand details' }))
    resize(1440)
    expect(dialog.show).toHaveBeenCalledTimes(2)
    expect(dialog.showModal).toHaveBeenCalledTimes(1)
    expect(dialog.hasAttribute('aria-modal')).toBe(false)
    const last = screen.getByRole('button', { name: 'Quiet action' })
    last.focus()
    expect(tab(last)).toBe(false)
  })

  it.each(['medium', 'large'])('Escape closes only Details at %s and restores the invoker', detent => {
    viewport(390)
    const onClose = vi.fn()
    const parentKeyDown = vi.fn()
    render(<Harness onClose={onClose} onKeyDown={parentKeyDown} />)
    const invoker = openDetails()
    const dialog = screen.getByRole('dialog', { name: payload.title })
    if (detent === 'large') fireEvent.click(screen.getByRole('button', { name: 'Expand details' }))
    fireEvent.keyDown(screen.getByRole('button', { name: 'Close details' }), { key: 'Escape' })
    expect(onClose).toHaveBeenCalledTimes(1)
    expect(parentKeyDown).not.toHaveBeenCalled()
    expect(dialog.open).toBe(false)
    expect(document.activeElement).toBe(invoker)
  })

  it('restores the owning region heading when the invoker was removed', () => {
    viewport(390)
    const { rerender } = render(<Harness />)
    const invoker = openDetails()
    fireEvent.click(screen.getByRole('button', { name: 'Expand details' }))
    rerender(<Harness invoker={false} />)
    expect(invoker.isConnected).toBe(false)
    fireEvent.click(screen.getByRole('button', { name: 'Close details' }))
    const heading = screen.getByRole('heading', { name: 'Events' })
    expect(heading.tabIndex).toBe(-1)
    expect(document.activeElement).toBe(heading)
  })

  it('prevents native cancel from dismissing behind React', () => {
    viewport(390)
    const onClose = vi.fn()
    render(<Harness onClose={onClose} />)
    openDetails()
    fireEvent.click(screen.getByRole('button', { name: 'Expand details' }))
    const dialog = screen.getByRole('dialog', { name: payload.title })
    const event = new Event('cancel', { cancelable: true })
    fireEvent(dialog, event)
    expect(event.defaultPrevented).toBe(true)
    expect(onClose).not.toHaveBeenCalled()
    expect(dialog.open).toBe(true)
  })

  it('closes the native dialog and restores focus on unmount', () => {
    viewport(1440)
    const { rerender } = render(<><button>Invoker</button><DetailsDrawer data={null} /></>)
    const invoker = screen.getByRole('button', { name: 'Invoker' })
    invoker.focus()
    rerender(<><button>Invoker</button><DetailsDrawer data={payload} /></>)
    const dialog = screen.getByRole('dialog', { name: payload.title })
    rerender(<><button>Invoker</button></>)
    expect(dialog.close).toHaveBeenCalledTimes(1)
    expect(document.activeElement).toBe(invoker)
  })

  it('keeps the amended B1 boundary and leaves surface and shadow styling to existing sheets', () => {
    const css = readFileSync(`${process.cwd()}/src/components/detailsTopLayer.css`, 'utf8')
    expect(css).toContain('/* leaf-dp-v1 */')
    expect(css).toContain('@media (max-width: 599px)')
    expect(css).toContain('token-lint-allow LS-T5-breakpoint: B1 exception')
    expect(css).toContain('rgba(8,8,9,.4)')
    expect(css).not.toMatch(/box-shadow\s*:/)
    expect(css.slice(0, css.indexOf('::backdrop'))).not.toMatch(/background\s*:/)
  })
})
