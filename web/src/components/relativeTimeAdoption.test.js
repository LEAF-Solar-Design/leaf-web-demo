import { createElement, StrictMode } from 'react'
import { act, cleanup, render } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { readFileSync } from 'node:fs'
import { relativeTime } from '../lib/railTime.js'
import useRelativeNow from '../lib/useRelativeNow.js'
import EngineChangesTab from './EngineChangesTab.jsx'
import ClaudeAccountPanel from './ClaudeAccountPanel.jsx'
import VersionHistory from './VersionHistory.jsx'
import CheckoutChip from './CheckoutChip.jsx'

const NOW = Date.parse('2026-10-05T19:14:00Z')
const source = (filename) => readFileSync(new URL(filename, import.meta.url), 'utf8')

afterEach(() => {
  cleanup()
  vi.useRealTimers()
  vi.restoreAllMocks()
})

describe('shared time formatter adoption', () => {
  it.each(['EngineChangesTab.jsx', 'ClaudeAccountPanel.jsx', 'VersionHistory.jsx', 'CheckoutChip.jsx'])('%s has no private relative formatter or clock', (filename) => {
    const code = source(`./${filename}`)
    expect(code).not.toMatch(/\bfunction\s+(?:relativeTime|fmtWhen|fmtUntil)\s*\(/)
    expect(code).not.toMatch(/Date\.now\s*\(|Math\.(?:round|floor)\s*\(|\.toLocaleDateString\s*\(/)
    expect(code).toMatch(/from ['"]\.\.\/lib\/railTime\.js['"]/)
    expect(code).toMatch(/\buseRelativeNow\(\)/)
  })

  it('keeps the VersionHistory time span free of extra attributes and its rowTitle on fmtAbs', () => {
    const code = source('./VersionHistory.jsx')
    expect(code.match(/<span\b[^>]*\bclassName="vh-when"[^>]*>/g)).toEqual(['<span className="vh-when">'])
    expect(code).toContain('rowTitle={(r) => fmtAbs(r.created)}')
  })

  it('updates all four surfaces and puts the CT absolute in the three permitted titles', () => {
    vi.useFakeTimers()
    vi.setSystemTime(NOW)
    const created = new Date(NOW - 45_000).toISOString()
    const expires = new Date(NOW + 45_000).toISOString()
    const version = { v: 1, parent: null, created, bytes: 10 }
    const { container } = render(createElement('div', null,
      createElement(EngineChangesTab, { result: { kind: 'ok', cards: [{ card_id: 'one', title: 'A change', state: 'accepted', created_at: created }] } }),
      createElement(ClaudeAccountPanel, {
        mock: false, open: true, loading: false, busy: false, onToggle: vi.fn(),
        grant: { linked: true, accounts: [{ id: 'one', label: 'Mount one', kind: 'oauth', linked_at: created }] },
      }),
      createElement(VersionHistory, { data: { head: 1, latest: 1, versions: [version] }, onClose: vi.fn(), onPreview: vi.fn(), mock: true }),
      createElement(CheckoutChip, { checkout: { holder: 'Someone', expires } }),
    ))
    const engineTime = container.querySelector('.engine-changes-row time')
    const mounted = container.querySelector('.ca-account-meta span[title]')
    const versionTime = container.querySelector('.vh-when')
    const checkout = container.querySelector('.checkout-chip')
    expect(engineTime.textContent).toBe('now')
    expect(mounted.textContent).toBe('mounted now')
    expect(versionTime.textContent).toBe('now')
    expect(engineTime.title).toBe('Oct 5, 2:13 PM CT')
    expect(mounted.title).toBe('Oct 5, 2:13 PM CT')
    expect(checkout.title).toBe('Oct 5, 2:14 PM CT')
    expect(checkout.querySelector('.t-rel').textContent).toBe('~1 m')
    expect(versionTime.getAttributeNames()).toEqual(['class'])
    const d = new Date(created)
    expect(container.querySelector('.vh-row').title).toBe(d.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }))

    act(() => vi.advanceTimersByTime(60_000))
    expect(engineTime.textContent).toBe('1 m')
    expect(mounted.textContent).toBe('mounted 1 m')
    expect(versionTime.textContent).toBe('1 m')
    expect(versionTime.getAttributeNames()).toEqual(['class'])
    expect(checkout.querySelector('.t-rel')).toBeNull()
  })
})

function Clock({ id, timestamp }) {
  return createElement('span', { 'data-testid': id }, relativeTime(timestamp, useRelativeNow()))
}

describe('one document ticker', () => {
  it('shares one 30-second interval, stops after the last subscriber, and refreshes on remount', () => {
    vi.useFakeTimers()
    vi.setSystemTime(NOW)
    const timestamp = NOW - 45_000
    const first = render(createElement(StrictMode, null, createElement(Clock, { id: 'first', timestamp })))
    const second = render(createElement(Clock, { id: 'second', timestamp }))
    expect(vi.getTimerCount()).toBe(1)
    expect(first.getByTestId('first').textContent).toBe('now')
    expect(second.getByTestId('second').textContent).toBe('now')
    act(() => vi.advanceTimersByTime(29_999))
    expect(first.getByTestId('first').textContent).toBe('now')
    act(() => vi.advanceTimersByTime(1))
    expect(first.getByTestId('first').textContent).toBe('1 m')
    expect(second.getByTestId('second').textContent).toBe('1 m')
    first.unmount()
    expect(vi.getTimerCount()).toBe(1)
    second.unmount()
    expect(vi.getTimerCount()).toBe(0)

    vi.setSystemTime(NOW + 3_600_000)
    const remounted = render(createElement(Clock, { id: 'later', timestamp }))
    expect(remounted.getByTestId('later').textContent).toBe('1 h')
    expect(vi.getTimerCount()).toBe(1)
    remounted.unmount()
    expect(vi.getTimerCount()).toBe(0)
  })
})
