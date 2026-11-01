// @vitest-environment jsdom
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { useState } from 'react'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import Toast, { TOAST_MS } from './Toast.jsx'

let hidden

beforeEach(() => {
  vi.useFakeTimers()
  hidden = false
  vi.spyOn(document, 'hidden', 'get').mockImplementation(() => hidden)
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  vi.useRealTimers()
})

function advance(ms) {
  act(() => { vi.advanceTimersByTime(ms) })
}

function Host() {
  const [toast, setToast] = useState(null)
  return <>
    <button type="button" onClick={() => setToast({ id: 1, text: 'First' })}>Show first</button>
    <button type="button" onClick={() => setToast({ id: 2, text: 'Second' })}>Show second</button>
    <Toast toast={toast} onDone={() => setToast(null)} />
  </>
}

function mount(action) {
  const onDone = vi.fn()
  const view = render(<Toast toast={{ id: 1, text: 'Saved', action }} onDone={onDone} />)
  return { ...view, onDone, root: screen.getByRole('status') }
}

describe('Toast design parity', () => {
  it('encodes the C3 A lifetimes', () => {
    // Receipt: design-parity-forks-20261101/receipts/operator-forks-all-a-20261101.json
    expect(TOAST_MS).toEqual({ info: 5000, action: 8000 })
  })

  it('keeps one slot after two shows', () => {
    const { container } = render(<Host />)
    fireEvent.click(screen.getByRole('button', { name: 'Show first' }))
    expect(container.querySelectorAll('.leaf-toast')).toHaveLength(1)
    fireEvent.click(screen.getByRole('button', { name: 'Show second' }))
    expect(container.querySelectorAll('.leaf-toast')).toHaveLength(1)
    expect(screen.getByRole('status')).toHaveTextContent('Second')
    expect(screen.queryByText('First')).toBeNull()
  })

  it.each(['info', 'action'])('starts the %s exit at its exact lifetime', (kind) => {
    const action = kind === 'action' ? { label: 'View', onClick: vi.fn() } : undefined
    const { root, onDone } = mount(action)
    expect(root).toHaveClass('toast', 'leaf-toast', 'enter')
    if (action) expect(root).toHaveAttribute('data-has-action', 'true')
    else expect(root).not.toHaveAttribute('data-has-action')
    advance(TOAST_MS[kind] - 1)
    expect(root).not.toHaveClass('exit')
    expect(onDone).not.toHaveBeenCalled()
    advance(1)
    expect(root).toHaveClass('exit')
    advance(179)
    expect(onDone).not.toHaveBeenCalled()
    advance(1)
    expect(onDone).toHaveBeenCalledExactlyOnceWith(1)
  })

  it('extends the lifetime by the 3000 ms spent hovering', () => {
    const { root, onDone } = mount()
    advance(2000)
    fireEvent.mouseEnter(root)
    advance(3000)
    expect(root).toHaveClass('enter')
    fireEvent.mouseLeave(root)
    advance(TOAST_MS.info - 2000 - 1)
    expect(root).not.toHaveClass('exit')
    advance(1)
    expect(root).toHaveClass('exit')
    advance(180)
    expect(onDone).toHaveBeenCalledExactlyOnceWith(1)
  })

  it('does not consume remaining time while the document is hidden', () => {
    const { root, onDone } = mount()
    advance(1000)
    hidden = true
    fireEvent(document, new Event('visibilitychange'))
    advance(10_000)
    expect(root).toHaveClass('enter')
    expect(onDone).not.toHaveBeenCalled()
    hidden = false
    fireEvent(document, new Event('visibilitychange'))
    advance(TOAST_MS.info - 1000 - 1)
    expect(root).not.toHaveClass('exit')
    advance(1)
    expect(root).toHaveClass('exit')
    advance(180)
    expect(onDone).toHaveBeenCalledExactlyOnceWith(1)
  })

  it('retains the active element on same-id updates and returns F6 focus', () => {
    const onDone = vi.fn()
    const view = (text) => <>
      <button type="button">Command bar</button>
      <Toast toast={{ id: 1, text, action: { label: 'View', onClick: vi.fn() } }} onDone={onDone} />
    </>
    const { rerender } = render(view('Saved'))
    const origin = screen.getByRole('button', { name: 'Command bar' })
    act(() => { origin.focus() })
    fireEvent.keyDown(window, { key: 'F6' })
    const active = document.activeElement
    expect(active).toBe(screen.getByRole('button', { name: 'View' }))
    rerender(view('Updated'))
    expect(document.activeElement).toBe(active)
    advance(TOAST_MS.action + 180)
    expect(onDone).not.toHaveBeenCalled()
    fireEvent.click(active)
    expect(onDone).toHaveBeenCalledExactlyOnceWith(1)
    expect(document.activeElement).toBe(origin)
  })

  it('uses the geometry recipe while leaving placement to the Studio stylesheet', () => {
    const css = readFileSync(resolve(process.cwd(), 'src/components/toastParity.css'), 'utf8')
    expect(css).toContain('/* leaf-dp-v1 */')
    expect(css).toContain('.toast.leaf-toast{')
    for (const declaration of [
      'width:min(var(--leaf-metric-toast),calc(100vw - 32px))',
      'padding:var(--leaf-space-3) var(--leaf-space-4)',
      'border-radius:var(--leaf-radius-control)',
      'background:var(--leaf-raised)',
      'color:var(--leaf-ink)',
      'border:1px solid var(--leaf-line)',
      'box-shadow:var(--leaf-shadow-popover)',
      'font-size:var(--leaf-size-dense)',
      'line-height:var(--leaf-leading-dense)',
    ]) expect(css).toContain(declaration)
    expect(css).not.toMatch(/(?:^|[;{])\s*(?:position|left|right|bottom|transform|z-index)\s*:/)
  })
})
