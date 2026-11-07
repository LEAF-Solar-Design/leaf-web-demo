import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { act, cleanup, render, screen } from '@testing-library/react'
import GroundSkeleton from './GroundSkeleton.jsx'

beforeEach(() => vi.useFakeTimers())
afterEach(() => {
  cleanup()
  vi.useRealTimers()
})

it('waits 200ms before announcing the named ground with static rows', () => {
  // Option A: design-parity-forks-20261101/receipts/operator-forks-all-a-20261101.json
  const { container } = render(<GroundSkeleton label="Lab" />)
  act(() => vi.advanceTimersByTime(150))
  expect(container).toBeEmptyDOMElement()
  act(() => vi.advanceTimersByTime(49))
  expect(screen.queryByRole('status')).toBeNull()
  act(() => vi.advanceTimersByTime(51))
  const status = screen.getByRole('status')
  expect(status).toHaveTextContent('Loading Lab')
  expect(status).toHaveAttribute('aria-live', 'polite')
  expect(status).toHaveAttribute('data-dp-case', 'ground-skeleton')
  expect(status.querySelectorAll('.skeleton-row[aria-hidden="true"]')).toHaveLength(3)
})

it('clears the pending timer on unmount', () => {
  const { unmount } = render(<GroundSkeleton label="Lab" />)
  act(() => vi.advanceTimersByTime(150))
  expect(vi.getTimerCount()).toBe(1)
  unmount()
  expect(vi.getTimerCount()).toBe(0)
})

it('supports a custom delay and never holds ready content', () => {
  const { rerender } = render(<GroundSkeleton label="Studio" delayMs={50} />)
  act(() => vi.advanceTimersByTime(50))
  expect(screen.getByRole('status')).toHaveTextContent('Loading Studio')
  rerender(<p>Ready workspace</p>)
  expect(screen.queryByRole('status')).toBeNull()
  expect(screen.getByText('Ready workspace')).toBeVisible()
  expect(vi.getTimerCount()).toBe(0)
})
