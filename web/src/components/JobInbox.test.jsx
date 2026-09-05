// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import JobInbox from './JobInbox.jsx'
import { createNotificationBus } from '../lib/notifications.js'

afterEach(cleanup)

describe('JobInbox', () => {
  it('says plainly that it is empty — no hopeful placeholder — when nothing has been pushed', () => {
    const bus = createNotificationBus(5)
    render(<JobInbox bus={bus} />)
    expect(screen.getByText(/No notices yet\./)).toBeInTheDocument()
  })

  it('lists a pushed notice with its time and a working retry action', () => {
    const bus = createNotificationBus(5)
    const onClick = vi.fn()
    bus.push({ text: 'Version 4 created', action: { label: 'View', onClick } })
    render(<JobInbox bus={bus} />)
    expect(screen.getByText('Version 4 created')).toBeInTheDocument()
    const retry = screen.getByRole('button', { name: /Retry/ })
    expect(retry).toBeEnabled()
    fireEvent.click(retry)
    expect(onClick).toHaveBeenCalledTimes(1)
  })

  it('disables retry with a real prose reason when the notice recorded no action', () => {
    const bus = createNotificationBus(5)
    bus.push({ text: 'Reverted to version 3' })
    render(<JobInbox bus={bus} />)
    const row = screen.getByText('Reverted to version 3').closest('.inbox-row')
    const retry = within(row).getByRole('button', { name: 'Retry' })
    expect(retry).toBeDisabled()
    expect(within(row).getByText(/recorded no follow-up action/)).toBeInTheDocument()
  })

  it('keeps EVERY pushed notice visible (not just the current toast) — the inbox reads the whole ring', () => {
    const bus = createNotificationBus(5)
    bus.push({ text: 'first notice' })
    bus.push({ text: 'second notice' })
    render(<JobInbox bus={bus} />)
    expect(screen.getByText('first notice')).toBeInTheDocument()
    expect(screen.getByText('second notice')).toBeInTheDocument()
  })

  it('renders at most the bus capacity — bounded, never unbounded, list', () => {
    const bus = createNotificationBus(3)
    for (let i = 0; i < 10; i += 1) bus.push({ text: `n${i}` })
    render(<JobInbox bus={bus} />)
    expect(screen.getAllByText(/^n\d$/).length).toBe(3)
  })

  it('dismiss hides a row from this view without touching the shared bus history', () => {
    const bus = createNotificationBus(5)
    bus.push({ text: 'Dismiss-me notice' })
    render(<JobInbox bus={bus} />)
    const row = screen.getByText('Dismiss-me notice').closest('.inbox-row')
    fireEvent.click(within(row).getByRole('button', { name: 'Dismiss' }))
    expect(screen.queryByText('Dismiss-me notice')).not.toBeInTheDocument()
    // the bus itself still holds it — dismiss is a view-local hide, not a delete
    expect(bus.getSnapshot().ring.some((n) => n.text === 'Dismiss-me notice')).toBe(true)
  })

  it('the collapse toggle is a real <button> (native keyboard operability) whose click flips aria-expanded', () => {
    const bus = createNotificationBus(5)
    bus.push({ text: 'Keyboard toggle probe notice' })
    render(<JobInbox bus={bus} />)
    const toggle = screen.getByRole('button', { name: /Collapse the notification inbox/ })
    expect(toggle.tagName).toBe('BUTTON') // native <button>: Enter/Space activate it with no bespoke key handling
    expect(toggle).toHaveAttribute('aria-expanded', 'true')
    fireEvent.click(toggle)
    expect(screen.getByRole('button', { name: /Expand the notification inbox/ })).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByText('Keyboard toggle probe notice')).not.toBeInTheDocument()
  })

  it('wears no studio spine furniture: the rail-OFF byte-identity row counts .spine-collapse and requires 0', () => {
    // one-shell-mount.spec.mjs "rail off is byte-for-byte the old shell" asserts
    // `.spine-collapse` has count 0 with the rail off; the inbox mounts rail-OFF
    // too (it tracks the job rail slot, which the console always passes), so its
    // toggle must never reuse JobRail's / NavRail's collapse-to-spine classes.
    // This went red on main after #1023 (d29c78f1) shipped the toggle as
    // `.spine-btn .spine-collapse`; a sibling proof on f28c0e00 caught it.
    const bus = createNotificationBus(5)
    bus.push({ text: 'spine furniture probe' })
    const { container } = render(<JobInbox bus={bus} />)
    expect(container.querySelector('.spine-collapse')).toBeNull()
    expect(container.querySelector('.spine-btn')).toBeNull()
    expect(container.querySelector('.spine-expand')).toBeNull()
    expect(container.querySelector('[data-spine]')).toBeNull()
    expect(container.querySelector('.inbox-toggle')).not.toBeNull()
  })

  it('every row action (Retry, Dismiss) is also a real <button>', () => {
    const bus = createNotificationBus(5)
    bus.push({ text: 'button-shape probe' })
    render(<JobInbox bus={bus} />)
    const row = screen.getByText('button-shape probe').closest('.inbox-row')
    for (const btn of within(row).getAllByRole('button')) expect(btn.tagName).toBe('BUTTON')
  })
})
