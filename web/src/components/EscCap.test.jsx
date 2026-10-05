// @vitest-environment jsdom
import { createRef } from 'react'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import EscCap from './EscCap.jsx'

afterEach(cleanup)

describe('EscCap', () => {
  it.each([
    'Close workspace',
    'Hide drawer',
    'Close version history',
    'Close Claude account panel',
    'Close linked services panel',
  ])('keeps the existing cap DOM for %s without adding a type attribute', (label) => {
    const { container } = render(<EscCap label={label} onClick={() => {}} />)
    expect(container.innerHTML).toBe(`<button class="key hot" aria-label="${label}">Esc</button>`)
    expect(screen.getByRole('button', { name: label }).hasAttribute('type')).toBe(false)
  })

  it('keeps the explicit type before class and label, as in DetailsDrawer', () => {
    const { container } = render(<EscCap type="button" label="Close details" />)
    expect(container.innerHTML).toBe('<button type="button" class="key hot" aria-label="Close details">Esc</button>')
  })

  it('forwards the ref to the focusable button and calls the dismiss handler', () => {
    const ref = createRef()
    const onClick = vi.fn()
    const { unmount } = render(<EscCap ref={ref} label="Hide drawer" onClick={onClick} />)
    const button = screen.getByRole('button', { name: 'Hide drawer' })
    expect(ref.current).toBe(button)
    ref.current.focus()
    expect(document.activeElement).toBe(button)
    fireEvent.click(button)
    expect(onClick).toHaveBeenCalledTimes(1)
    unmount()
    expect(ref.current).toBeNull()
  })

  it('passes button attributes through while keeping Esc as the visible text', () => {
    const onClick = vi.fn()
    render(<EscCap type="button" label="Close" data-testid="dismiss" disabled onClick={onClick} />)
    const button = screen.getByTestId('dismiss')
    expect(button).toBe(screen.getByRole('button', { name: 'Close' }))
    expect(button.textContent).toBe('Esc')
    expect(button.disabled).toBe(true)
    fireEvent.click(button)
    expect(onClick).not.toHaveBeenCalled()
  })

  it('does not submit its enclosing form when the caller passes type="button"', () => {
    const onSubmit = vi.fn((event) => event.preventDefault())
    const onClose = vi.fn()
    render(
      <form onSubmit={onSubmit}>
        <EscCap type="button" label="Close" onClick={onClose} />
        <button type="submit">Send</button>
      </form>,
    )
    fireEvent.click(screen.getByRole('button', { name: 'Close' }))
    expect(onClose).toHaveBeenCalledTimes(1)
    expect(onSubmit).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Send' }))
    expect(onSubmit).toHaveBeenCalledTimes(1)
  })
})
