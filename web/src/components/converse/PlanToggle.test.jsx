// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import PlanToggle from './PlanToggle.jsx'

afterEach(cleanup)

it('announces the pressed state while keeping the Plan label and shortcut', () => {
  const onToggle = vi.fn()
  const view = render(<PlanToggle pressed={false} onToggle={onToggle} />)
  const pill = screen.getByRole('button', { name: 'Plan', pressed: false })
  expect(pill).toHaveAttribute('type', 'button')
  expect(pill).toHaveAttribute('aria-keyshortcuts', 'Shift+Tab')
  expect(pill.style.background).toBe('transparent')
  fireEvent.click(pill)
  expect(onToggle).toHaveBeenCalledOnce()
  view.rerender(<PlanToggle pressed onToggle={onToggle} />)
  expect(screen.getByRole('button', { name: 'Plan', pressed: true })).toBe(pill)
  expect(pill.style.background).toBe('var(--well-accent)')
})
