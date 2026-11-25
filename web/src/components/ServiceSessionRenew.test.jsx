// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import ServiceSessionRenew from './ServiceSessionRenew.jsx'

vi.mock('../auth.js', () => ({ serviceReauth: vi.fn() }))

const MINUTE = 60 * 1000
const NOW = 1_800_000_000_000
const now = () => NOW

function fakeSession(overrides = {}) {
  let value = { mode: true, identity: { sub: 'service' }, expiresAt: NOW + 4 * MINUTE, ...overrides }
  const listeners = new Set()
  const unsubscribe = vi.fn(listener => listeners.delete(listener))
  return {
    snapshot: vi.fn(() => value),
    subscribe: vi.fn(listener => {
      listeners.add(listener)
      return () => unsubscribe(listener)
    }),
    unsubscribe,
    publish(update) {
      value = { ...value, ...update }
      for (const listener of listeners) listener()
    },
  }
}

afterEach(() => {
  cleanup()
  vi.useRealTimers()
})

describe('ServiceSessionRenew', () => {
  it.each([
    ['human mode', { mode: false }],
    ['non-boolean service mode', { mode: 'service' }],
    ['service mode without identity', { identity: null }],
    ['ten minutes left', { expiresAt: NOW + 10 * MINUTE }],
    ['just outside the window', { expiresAt: NOW + 5 * MINUTE + 1 }],
    ['expiry reached', { expiresAt: NOW }],
    ['expired', { expiresAt: NOW - 1 }],
  ])('renders nothing for %s', (_name, overrides) => {
    const { container } = render(<ServiceSessionRenew session={fakeSession(overrides)} now={now} onRenew={vi.fn()} />)
    expect(container.innerHTML).toBe('')
  })

  it.each([4 * MINUTE, 5 * MINUTE, 1])('renders one renew button with %i ms left', remaining => {
    render(<ServiceSessionRenew session={fakeSession({ expiresAt: NOW + remaining })} now={now} onRenew={vi.fn()} />)
    const button = screen.getByRole('button', { name: 'Renew session' })
    expect(screen.getAllByRole('button')).toHaveLength(1)
    expect(button.type).toBe('button')
    expect(button.className).toBe('chip-act tc-service-renew')
    expect(button.disabled).toBe(false)
  })

  it('uses the injected renewal window', () => {
    const { rerender } = render(<ServiceSessionRenew session={fakeSession()} now={now} windowMs={3 * MINUTE} />)
    expect(screen.queryByRole('button')).toBeNull()
    rerender(<ServiceSessionRenew session={fakeSession()} now={now} windowMs={4 * MINUTE} />)
    expect(screen.getByRole('button', { name: 'Renew session' })).not.toBeNull()
  })

  it('calls onRenew once and stays disabled until unmount', () => {
    const session = fakeSession()
    const onRenew = vi.fn()
    render(<ServiceSessionRenew session={session} now={now} onRenew={onRenew} />)
    const button = screen.getByRole('button', { name: 'Renew session' })
    fireEvent.click(button)
    expect(onRenew).toHaveBeenCalledExactlyOnceWith()
    expect(button.disabled).toBe(true)
    fireEvent.click(button)
    act(() => session.publish({ expiresAt: NOW + 10 * MINUTE }))
    expect(screen.queryByRole('button')).toBeNull()
    act(() => session.publish({ expiresAt: NOW + 4 * MINUTE }))
    const returned = screen.getByRole('button', { name: 'Renew session' })
    expect(returned.disabled).toBe(true)
    fireEvent.click(returned)
    expect(onRenew).toHaveBeenCalledTimes(1)
  })

  it('re-reads the snapshot on session publish', () => {
    const session = fakeSession({ expiresAt: NOW + 10 * MINUTE })
    render(<ServiceSessionRenew session={session} now={now} onRenew={vi.fn()} />)
    expect(screen.queryByRole('button')).toBeNull()
    session.snapshot.mockClear()
    act(() => session.publish({ expiresAt: NOW + 4 * MINUTE }))
    expect(session.snapshot).toHaveBeenCalledTimes(1)
    expect(screen.getByRole('button', { name: 'Renew session' })).not.toBeNull()
    act(() => session.publish({ identity: null }))
    expect(screen.queryByRole('button')).toBeNull()
  })

  it('refreshes every 15 seconds and cleans up the interval and subscription', () => {
    vi.useFakeTimers()
    let current = NOW
    const session = fakeSession({ expiresAt: NOW + 5 * MINUTE + 15000 })
    const { unmount } = render(<ServiceSessionRenew session={session} now={() => current} onRenew={vi.fn()} />)
    expect(session.subscribe).toHaveBeenCalledTimes(1)
    expect(vi.getTimerCount()).toBe(1)
    expect(screen.queryByRole('button')).toBeNull()
    session.snapshot.mockClear()
    current += 14999
    act(() => vi.advanceTimersByTime(14999))
    expect(session.snapshot).not.toHaveBeenCalled()
    current += 1
    act(() => vi.advanceTimersByTime(1))
    expect(session.snapshot).toHaveBeenCalledTimes(1)
    expect(screen.getByRole('button', { name: 'Renew session' })).not.toBeNull()
    current += 5 * MINUTE
    act(() => vi.advanceTimersByTime(5 * MINUTE))
    expect(screen.queryByRole('button')).toBeNull()
    unmount()
    expect(session.unsubscribe).toHaveBeenCalledTimes(1)
    expect(vi.getTimerCount()).toBe(0)
    session.snapshot.mockClear()
    act(() => {
      session.publish({})
      vi.advanceTimersByTime(15000)
    })
    expect(session.snapshot).not.toHaveBeenCalled()
  })
})
