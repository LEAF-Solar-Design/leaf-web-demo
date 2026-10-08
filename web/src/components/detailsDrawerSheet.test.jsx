// @vitest-environment jsdom
import { readFileSync } from 'node:fs'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import DetailsDrawer from './DetailsDrawer.jsx'
const sheetCSS = readFileSync(`${process.cwd()}/src/components/detailsDrawerSheet.css`, 'utf8')

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

function viewport(width) {
  const listeners = new Set()
  const media = {
    matches: width < 640,
    media: '(max-width: 639px)',
    addEventListener: vi.fn((_, listener) => listeners.add(listener)),
    removeEventListener: vi.fn((_, listener) => listeners.delete(listener)),
  }
  vi.stubGlobal('matchMedia', vi.fn(() => media))
  // jsdom does not provide PointerEvent or layout rectangles.
  vi.stubGlobal('PointerEvent', class extends MouseEvent {
    constructor(type, options = {}) {
      super(type, options)
      this.pointerId = options.pointerId ?? 1
      this.isPrimary = options.isPrimary ?? true
    }
  })
  vi.spyOn(HTMLElement.prototype, 'getClientRects').mockReturnValue([{ width: 10, height: 10 }])
  return {
    media,
    resize(nextWidth) {
      act(() => {
        media.matches = nextWidth < 640
        listeners.forEach(listener => listener({ matches: media.matches }))
      })
    },
  }
}

const payload = { title: 'Session details', rows: ['Session provenance'], action: { label: 'Quiet action', onClick: vi.fn() } }

function mount() {
  const onClose = vi.fn()
  const result = render(<><button type="button">Background action</button><DetailsDrawer data={payload} onClose={onClose} /></>)
  return { ...result, onClose, sheet: screen.getByRole('dialog', { name: 'Session details' }) }
}

function drag(sheet, from, to) {
  const header = sheet.querySelector('.drawer-head')
  fireEvent.pointerDown(header, { button: 0, clientY: from, pointerId: 1 })
  fireEvent.pointerUp(header, { button: 0, clientY: to, pointerId: 1 })
}

function tab(target, shiftKey = false) {
  const event = new KeyboardEvent('keydown', { key: 'Tab', shiftKey, bubbles: true, cancelable: true })
  fireEvent(target, event)
  return event.defaultPrevented
}

describe('DetailsDrawer phone sheet', () => {
  it('opens at medium with the Esc cap focused and leaves background controls usable', () => {
    viewport(390)
    const { sheet, onClose } = mount()
    expect(window.matchMedia).toHaveBeenCalledWith('(max-width: 639px)')
    expect(sheet.classList.contains('drawer-sheet')).toBe(true)
    expect(sheet.parentElement.classList.contains('drawer-sheet-layer')).toBe(true)
    expect(sheet.dataset.detent).toBe('medium')
    expect(sheet.getAttribute('aria-modal')).toBe('false')
    const close = screen.getByRole('button', { name: 'Close details' })
    expect(close.textContent).toBe('Esc')
    expect(document.activeElement).toBe(close)
    const action = screen.getByRole('button', { name: 'Quiet action' })
    action.focus()
    expect(tab(action)).toBe(false)
    const background = screen.getByRole('button', { name: 'Background action' })
    background.focus()
    expect(document.activeElement).toBe(background)
    fireEvent.keyDown(close, { key: 'Escape' })
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('snaps on header drags and traps Tab only at large', () => {
    viewport(390)
    const { sheet } = mount()
    drag(sheet, 500, 400)
    expect(sheet.dataset.detent).toBe('large')
    expect(sheet.getAttribute('aria-modal')).toBe('true')
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Close details' }))
    const first = screen.getByRole('button', { name: 'Condense details' })
    const last = screen.getByRole('button', { name: 'Quiet action' })
    last.focus()
    expect(tab(last)).toBe(true)
    expect(document.activeElement).toBe(first)
    expect(tab(first, true)).toBe(true)
    expect(document.activeElement).toBe(last)
    drag(sheet, 400, 500)
    expect(sheet.dataset.detent).toBe('medium')
    expect(sheet.getAttribute('aria-modal')).toBe('false')
    last.focus()
    expect(tab(last)).toBe(false)
  })

  it('ignores short and canceled drags and supports a keyboard detent control', () => {
    viewport(390)
    const { sheet } = mount()
    drag(sheet, 500, 480)
    expect(sheet.dataset.detent).toBe('medium')
    const header = sheet.querySelector('.drawer-head')
    fireEvent.pointerDown(header, { button: 0, clientY: 500 })
    fireEvent.pointerCancel(header)
    fireEvent.pointerUp(header, { clientY: 400 })
    expect(sheet.dataset.detent).toBe('medium')
    fireEvent.click(screen.getByRole('button', { name: 'Expand details' }))
    expect(sheet.dataset.detent).toBe('large')
    fireEvent.click(screen.getByRole('button', { name: 'Condense details' }))
    expect(sheet.dataset.detent).toBe('medium')
  })

  it('keeps a header drag owned by its primary pointer', () => {
    viewport(390)
    const { sheet } = mount()
    const header = sheet.querySelector('.drawer-head')
    fireEvent.pointerDown(header, { button: 0, clientY: 500, pointerId: 2, isPrimary: false })
    fireEvent.pointerUp(header, { clientY: 400, pointerId: 2 })
    expect(sheet.dataset.detent).toBe('medium')
    fireEvent.pointerDown(header, { button: 0, clientY: 500, pointerId: 1 })
    fireEvent.pointerUp(header, { clientY: 400, pointerId: 2 })
    expect(sheet.dataset.detent).toBe('medium')
    fireEvent.pointerUp(header, { clientY: 400, pointerId: 1 })
    expect(sheet.dataset.detent).toBe('large')
  })

  it('keeps transitions and detent animations confined to opacity', () => {
    viewport(390)
    const { sheet } = mount()
    drag(sheet, 500, 400)
    const transitions = [...sheetCSS.matchAll(/transition\s*:\s*([^;]+);/g)].map(match => match[1].trim())
    expect(transitions.length).toBeGreaterThan(0)
    expect(transitions.every(value => value === 'none' || /^opacity\s/.test(value))).toBe(true)
    expect(transitions.join(' ')).not.toMatch(/transform|top|height|all/)
    const frames = [...sheetCSS.matchAll(/@keyframes details-sheet-\w+\s*\{ from \{([^}]+)\} to \{([^}]+)\} \}/g)]
    expect(frames).toHaveLength(2)
    for (const frame of frames) expect(frame[1] + frame[2]).not.toMatch(/transform|top|height/)
    expect(sheetCSS).toContain('transform: none;')
    expect(sheetCSS).toContain('prefers-reduced-motion: reduce')
  })

  it.each([640, 1024])('preserves the right drawer and modal trap at %i px', width => {
    viewport(width)
    const { sheet } = mount()
    expect(sheet.className).toBe('drawer enter')
    expect(sheet.parentElement.className).toBe('drawer-layer')
    expect(sheet.hasAttribute('data-detent')).toBe(false)
    expect(sheet.getAttribute('aria-modal')).toBe('true')
    expect(screen.queryByRole('button', { name: 'Expand details' })).toBeNull()
    drag(sheet, 500, 400)
    expect(sheet.hasAttribute('data-detent')).toBe(false)
    const last = screen.getByRole('button', { name: 'Quiet action' })
    last.focus()
    expect(tab(last)).toBe(true)
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Close details' }))
  })

  it('responds to breakpoint changes and removes its media listener', () => {
    const { media, resize } = viewport(390)
    const { sheet, unmount } = mount()
    resize(1024)
    expect(sheet.className).toBe('drawer enter')
    expect(sheet.getAttribute('aria-modal')).toBe('true')
    resize(390)
    expect(sheet.dataset.detent).toBe('medium')
    unmount()
    expect(media.removeEventListener).toHaveBeenCalledWith('change', expect.any(Function))
  })
})
