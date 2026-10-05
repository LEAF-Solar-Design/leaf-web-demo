// @vitest-environment jsdom
import { useState } from 'react'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import Toast from './Toast.jsx'

let hidden

beforeEach(() => {
  vi.useFakeTimers()
  hidden = false
  vi.spyOn(document, 'hidden', 'get').mockImplementation(() => hidden)
  // jsdom has no PointerEvent constructor; retain real MouseEvent coordinates
  // while giving the handler the pointer identity browsers supply.
  vi.stubGlobal('PointerEvent', class extends MouseEvent {
    constructor(type, options = {}) {
      super(type, options)
      this.pointerId = options.pointerId ?? 1
      this.isPrimary = options.isPrimary ?? true
    }
  })
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  vi.useRealTimers()
})

function advance(ms) {
  act(() => { vi.advanceTimersByTime(ms) })
}

function visibility(value) {
  hidden = value
  fireEvent(document, new Event('visibilitychange'))
}

function mount(action = null) {
  const toast = { id: 1, text: 'Saved', action }
  const onDone = vi.fn()
  const view = render(<Toast toast={toast} onDone={onDone} />)
  return { ...view, toast, onDone, root: screen.getByRole('status') }
}

function Host({ notice }) {
  const [toast, setToast] = useState(notice)
  return (
    <>
      <button type="button">Command bar</button>
      <button type="button">Elsewhere</button>
      <Toast toast={toast} onDone={() => setToast(null)} />
    </>
  )
}

describe('Toast lifetime and interaction', () => {
  it('keeps the status markup and starts the exit at 5 s, then closes after the 180 ms fade', () => {
    const { root, onDone } = mount()
    expect(root).toHaveClass('toast', 'enter')
    expect(root).not.toHaveAttribute('aria-live')
    advance(4999)
    expect(root).not.toHaveClass('exit')
    expect(onDone).not.toHaveBeenCalled()
    advance(1)
    expect(root).toHaveClass('exit')
    advance(179)
    expect(onDone).not.toHaveBeenCalled()
    advance(1)
    expect(onDone).toHaveBeenCalledTimes(1)
    expect(onDone).toHaveBeenCalledWith(1)
  })

  it('gives action.undo === true an 8 s lifetime', () => {
    const { root, onDone } = mount({ label: 'Undo', undo: true, onClick: vi.fn() })
    advance(5000)
    expect(root).toHaveClass('enter')
    advance(2999)
    expect(onDone).not.toHaveBeenCalled()
    expect(root).not.toHaveClass('exit')
    advance(1)
    expect(root).toHaveClass('exit')
    advance(180)
    expect(onDone).toHaveBeenCalledTimes(1)
    expect(onDone).toHaveBeenCalledWith(1)
  })

  it('uses 5 s for an ordinary action even if its label is Undo', () => {
    const { onDone } = mount({ label: 'Undo', onClick: vi.fn() })
    advance(5180)
    expect(onDone).toHaveBeenCalledTimes(1)
    expect(onDone).toHaveBeenCalledWith(1)
  })

  it('pauses on hover and resumes with only the remaining 3 s', () => {
    const { root, onDone } = mount()
    advance(2000)
    fireEvent.mouseEnter(root)
    advance(7000)
    expect(root).toHaveClass('enter')
    expect(onDone).not.toHaveBeenCalled()
    fireEvent.mouseLeave(root)
    advance(2999)
    expect(root).not.toHaveClass('exit')
    advance(1)
    expect(root).toHaveClass('exit')
    advance(180)
    expect(onDone).toHaveBeenCalledTimes(1)
    expect(onDone).toHaveBeenCalledWith(1)
  })

  it('pauses while focus is within the toast and resumes after focus leaves', () => {
    const { root, onDone } = mount({ onClick: vi.fn() })
    advance(1700)
    act(() => { screen.getByRole('button').focus() })
    advance(9000)
    expect(root).toHaveClass('enter')
    expect(onDone).not.toHaveBeenCalled()
    act(() => { screen.getByRole('button').blur() })
    advance(3299)
    expect(root).not.toHaveClass('exit')
    advance(181)
    expect(onDone).toHaveBeenCalledTimes(1)
    expect(onDone).toHaveBeenCalledWith(1)
  })

  it('pauses while hidden and resumes with its remaining time when visible', () => {
    const { root, onDone } = mount()
    advance(3000)
    visibility(true)
    advance(20_000)
    expect(root).toHaveClass('enter')
    expect(onDone).not.toHaveBeenCalled()
    visibility(false)
    advance(1999)
    expect(root).not.toHaveClass('exit')
    advance(181)
    expect(onDone).toHaveBeenCalledTimes(1)
    expect(onDone).toHaveBeenCalledWith(1)
  })

  it('does not start a lifetime while the document is already hidden', () => {
    hidden = true
    const { onDone } = mount()
    advance(20_000)
    expect(onDone).not.toHaveBeenCalled()
    visibility(false)
    advance(5179)
    expect(onDone).not.toHaveBeenCalled()
    advance(1)
    expect(onDone).toHaveBeenCalledTimes(1)
    expect(onDone).toHaveBeenCalledWith(1)
  })

  it('resumes only after all overlapping pause reasons end', () => {
    const { root, onDone } = mount({ onClick: vi.fn() })
    advance(1000)
    fireEvent.mouseEnter(root)
    act(() => { screen.getByRole('button').focus() })
    visibility(true)
    advance(7000)
    fireEvent.mouseLeave(root)
    advance(7000)
    visibility(false)
    advance(7000)
    expect(onDone).not.toHaveBeenCalled()
    act(() => { screen.getByRole('button').blur() })
    advance(3999)
    expect(root).not.toHaveClass('exit')
    advance(181)
    expect(onDone).toHaveBeenCalledTimes(1)
    expect(onDone).toHaveBeenCalledWith(1)
  })

  it('gives a replacement a fresh lifetime and cancels the old callback', () => {
    const { rerender, onDone } = mount()
    advance(4000)
    rerender(<Toast toast={{ id: 2, text: 'Updated' }} onDone={onDone} />)
    advance(1180)
    expect(onDone).not.toHaveBeenCalled()
    advance(4000)
    expect(onDone).toHaveBeenCalledTimes(1)
    expect(onDone).toHaveBeenCalledWith(2)
  })

  it('does not restart the lifetime when only onDone changes', () => {
    const { rerender, toast, onDone } = mount()
    advance(4000)
    const nextDone = vi.fn()
    rerender(<Toast toast={toast} onDone={nextDone} />)
    advance(1180)
    expect(onDone).not.toHaveBeenCalled()
    expect(nextDone).toHaveBeenCalledTimes(1)
    expect(nextDone).toHaveBeenCalledWith(1)
  })

  it.each([-49, 49])('dismisses a horizontal drag of %s px and cancels its timer', (dx) => {
    const { root, onDone } = mount()
    fireEvent.pointerDown(root, { clientX: 100, clientY: 20, button: 0, pointerId: 7 })
    fireEvent.pointerUp(root, { clientX: 100 + dx, clientY: 21, pointerId: 7 })
    expect(onDone).toHaveBeenCalledTimes(1)
    expect(onDone).toHaveBeenCalledWith(1)
    advance(20_000)
    expect(onDone).toHaveBeenCalledTimes(1)
  })

  it('ignores 48 px, vertical drags, other pointers, and cancelled gestures', () => {
    const { root, onDone } = mount()
    const start = () => fireEvent.pointerDown(root, { clientX: 0, clientY: 0, button: 0, pointerId: 7 })
    start()
    fireEvent.pointerUp(root, { clientX: 48, clientY: 0, pointerId: 7 })
    start()
    fireEvent.pointerUp(root, { clientX: 60, clientY: 100, pointerId: 7 })
    start()
    fireEvent.pointerUp(root, { clientX: 60, clientY: 0, pointerId: 8 })
    fireEvent.pointerCancel(root, { pointerId: 7 })
    fireEvent.pointerUp(root, { clientX: 60, clientY: 0, pointerId: 7 })
    expect(onDone).not.toHaveBeenCalled()
    advance(5180)
    expect(onDone).toHaveBeenCalledTimes(1)
    expect(onDone).toHaveBeenCalledWith(1)
  })

  it('does not activate the action via the click following a swipe', () => {
    const onClick = vi.fn()
    const { onDone } = mount({ onClick })
    const action = screen.getByRole('button')
    fireEvent.pointerDown(action, { clientX: 0, clientY: 0, button: 0 })
    fireEvent.pointerUp(action, { clientX: 60, clientY: 0 })
    fireEvent.click(action)
    expect(onClick).not.toHaveBeenCalled()
    expect(onDone).toHaveBeenCalledTimes(1)
    expect(onDone).toHaveBeenCalledWith(1)
  })

  it('F6 focuses the action, pauses, and returns to the origin when the action closes it', () => {
    const onClick = vi.fn()
    render(<Host notice={{ id: 1, text: 'Saved', action: { label: 'View', onClick } }} />)
    const origin = screen.getByRole('button', { name: 'Command bar' })
    act(() => { origin.focus() })
    fireEvent.keyDown(window, { key: 'F6' })
    expect(screen.getByRole('button', { name: 'View' })).toHaveFocus()
    advance(9000)
    expect(screen.getByRole('status')).toHaveClass('enter')
    fireEvent.click(screen.getByRole('button', { name: 'View' }))
    expect(onClick).toHaveBeenCalledTimes(1)
    expect(screen.queryByRole('status')).toBeNull()
    expect(origin).toHaveFocus()
  })

  it('F6 also reaches a toast without an action and restores focus on a swipe close', () => {
    render(<Host notice={{ id: 1, text: 'Saved' }} />)
    const origin = screen.getByRole('button', { name: 'Command bar' })
    act(() => { origin.focus() })
    fireEvent.keyDown(window, { key: 'F6' })
    const root = screen.getByRole('status')
    expect(root).toHaveFocus()
    fireEvent.pointerDown(root, { clientX: 0, clientY: 0, button: 0 })
    fireEvent.pointerUp(root, { clientX: 60, clientY: 0 })
    expect(screen.queryByRole('status')).toBeNull()
    expect(origin).toHaveFocus()
  })

  it('a keyed update retains F6 focus and its return destination', () => {
    const origin = document.createElement('button')
    document.body.append(origin)
    const { rerender, onDone } = mount({ onClick: vi.fn() })
    act(() => { origin.focus() })
    fireEvent.keyDown(window, { key: 'F6' })
    rerender(<Toast toast={{ id: 1, text: 'Updated', action: { label: 'Undo', undo: true, onClick: vi.fn() } }} onDone={onDone} />)
    expect(screen.getByRole('button', { name: 'Undo' })).toHaveFocus()
    advance(20_000)
    expect(onDone).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Undo' }))
    expect(origin).toHaveFocus()
    origin.remove()
  })

  it('F6 can return focus and let the remaining lifetime run', () => {
    const origin = document.createElement('button')
    document.body.append(origin)
    const { root, onDone } = mount({ onClick: vi.fn() })
    act(() => { origin.focus() })
    advance(2000)
    fireEvent.keyDown(window, { key: 'F6' })
    advance(7000)
    fireEvent.keyDown(window, { key: 'F6' })
    expect(origin).toHaveFocus()
    advance(2999)
    expect(root).not.toHaveClass('exit')
    advance(181)
    expect(onDone).toHaveBeenCalledTimes(1)
    expect(onDone).toHaveBeenCalledWith(1)
    origin.remove()
  })

  it('returns focus when a focused toast is removed by its host', () => {
    const origin = document.createElement('button')
    document.body.append(origin)
    const { rerender, onDone } = mount({ onClick: vi.fn() })
    act(() => { origin.focus() })
    fireEvent.keyDown(window, { key: 'F6' })
    rerender(<Toast toast={null} onDone={onDone} />)
    expect(origin).toHaveFocus()
    // Focus/DOM bookkeeping can leave unrelated fake timers queued. Prove
    // the removed toast's lifecycle stays inert beyond its full lifetime.
    advance(20_000)
    visibility(true)
    visibility(false)
    fireEvent.keyDown(window, { key: 'F6' })
    expect(origin).toHaveFocus()
    expect(onDone).not.toHaveBeenCalled()
    origin.remove()
  })

  it('does not steal focus back if the action intentionally moves it elsewhere', () => {
    render(<Host notice={{ id: 1, text: 'Saved', action: { label: 'View', onClick: () => screen.getByRole('button', { name: 'Elsewhere' }).focus() } }} />)
    act(() => { screen.getByRole('button', { name: 'Command bar' }).focus() })
    fireEvent.keyDown(window, { key: 'F6' })
    fireEvent.click(screen.getByRole('button', { name: 'View' }))
    expect(screen.getByRole('button', { name: 'Elsewhere' })).toHaveFocus()
    expect(screen.queryByRole('status')).toBeNull()
  })

  it('cleans timers and listeners on unmount', () => {
    const { unmount, onDone } = mount()
    unmount()
    expect(vi.getTimerCount()).toBe(0)
    visibility(true)
    visibility(false)
    fireEvent.keyDown(window, { key: 'F6' })
    advance(20_000)
    expect(onDone).not.toHaveBeenCalled()
    expect(vi.getTimerCount()).toBe(0)
  })
})
