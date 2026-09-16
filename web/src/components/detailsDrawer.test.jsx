// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import DetailsDrawer from './DetailsDrawer.jsx'

const clipboardDescriptor = Object.getOwnPropertyDescriptor(navigator, 'clipboard')
afterEach(() => {
  cleanup()
  vi.useRealTimers()
  if (clipboardDescriptor) Object.defineProperty(navigator, 'clipboard', clipboardDescriptor)
  else delete navigator.clipboard
})

const diagnostics = 'Leaf Automation diagnostics\nbuild abc123\n  Save = REASONS.writeLocked'
function clipboard(value) {
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value })
}

describe('DetailsDrawer diagnostics', () => {
  it('keeps ordinary payloads free of diagnostics controls', () => {
    render(<DetailsDrawer data={{ title: 'Details', rows: ['Row'] }} />)
    expect(screen.queryByTestId('diagnostics-block')).toBeNull()
    expect(screen.queryByTestId('copy-diagnostics')).toBeNull()
    expect(screen.getByText('Row')).toBeTruthy()
  })

  it('shows and copies the exact text, then restores the copy label', async () => {
    vi.useFakeTimers()
    const writeText = vi.fn().mockResolvedValue(undefined)
    clipboard({ writeText })
    const { unmount } = render(<DetailsDrawer data={{ title: 'Details', diagnostics }} />)
    expect(screen.getByTestId('diagnostics-block').textContent).toBe(diagnostics)
    await act(async () => { fireEvent.click(screen.getByTestId('copy-diagnostics')) })
    expect(writeText).toHaveBeenCalledWith(diagnostics)
    expect(screen.getByTestId('copy-diagnostics').textContent).toBe('Copied')
    act(() => { vi.advanceTimersByTime(1500) })
    expect(screen.getByTestId('copy-diagnostics').textContent).toBe('Copy diagnostics')
    await act(async () => { fireEvent.click(screen.getByTestId('copy-diagnostics')) })
    unmount()
    expect(vi.getTimerCount()).toBe(0)
  })

  it.each(['rejected', 'missing'])('keeps text selectable when the clipboard is %s', async (kind) => {
    clipboard(kind === 'missing' ? undefined : { writeText: vi.fn().mockRejectedValue(new Error('denied')) })
    render(<DetailsDrawer data={{ title: 'Details', diagnostics }} />)
    await act(async () => { fireEvent.click(screen.getByTestId('copy-diagnostics')) })
    expect(screen.getByTestId('copy-diagnostics').textContent).toBe('Copy failed, select the text above')
    expect(screen.getByTestId('diagnostics-block').textContent).toBe(diagnostics)
  })

  it('still closes on Esc from the diagnostics control', () => {
    const onClose = vi.fn()
    render(<DetailsDrawer data={{ title: 'Details', diagnostics }} onClose={onClose} />)
    fireEvent.keyDown(screen.getByTestId('copy-diagnostics'), { key: 'Escape' })
    expect(onClose).toHaveBeenCalledTimes(1)
  })
})
