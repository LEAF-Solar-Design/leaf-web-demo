// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import EscCap from './EscCap.jsx'

afterEach(cleanup)

describe('EscCap parity', () => {
  it('closes without submitting its enclosing form by default', () => {
    const onSubmit = vi.fn((event) => event.preventDefault())
    const onClose = vi.fn()
    render(
      <form onSubmit={onSubmit}>
        <EscCap onClick={onClose} />
      </form>,
    )

    const button = screen.getByRole('button', { name: 'Close' })
    expect(button.getAttribute('type')).toBe('button')
    fireEvent.click(button)
    expect(onClose).toHaveBeenCalledTimes(1)
    expect(onSubmit).not.toHaveBeenCalled()
  })

  it('submits its enclosing form when explicitly requested', () => {
    const onSubmit = vi.fn((event) => event.preventDefault())
    const onClose = vi.fn()
    render(
      <form onSubmit={onSubmit}>
        <EscCap type="submit" onClick={onClose} />
      </form>,
    )

    fireEvent.click(screen.getByRole('button', { name: 'Close' }))
    expect(onClose).toHaveBeenCalledTimes(1)
    expect(onSubmit).toHaveBeenCalledTimes(1)
  })

  it.each([
    ['Enter', 'Enter'],
    [' ', 'Space'],
  ])('preserves native button activation for %s', (key, code) => {
    const onClose = vi.fn()
    render(<EscCap onClick={onClose} />)
    const button = screen.getByRole('button', { name: 'Close' })
    expect(button.tagName).toBe('BUTTON')
    expect(button.disabled).toBe(false)
    button.focus()
    expect(document.activeElement).toBe(button)

    // jsdom does not generate native keyboard clicks. Model that default action
    // after verifying that the button leaves the activation keys uncancelled.
    expect(fireEvent.keyDown(button, { key, code })).toBe(true)
    if (key === 'Enter') fireEvent.click(button, { detail: 0 })
    expect(fireEvent.keyUp(button, { key, code })).toBe(true)
    if (key === ' ') fireEvent.click(button, { detail: 0 })
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('uses the button type for a null type too', () => {
    render(<EscCap type={null} />)
    expect(screen.getByRole('button', { name: 'Close' }).getAttribute('type')).toBe('button')
  })

  it('keeps the focus recipe and key radius while merging caller classes and styles', () => {
    render(<EscCap className="drawer-close" style={{ color: 'red' }} />)
    const button = screen.getByRole('button', { name: 'Close' })
    expect(button).toHaveClass('key', 'hot', 'esc-cap', 'dp-focus', 'drawer-close')
    expect(button.style.borderRadius).toBe('var(--leaf-radius-key)')
    expect(button.style.color).toBe('red')
  })
})
