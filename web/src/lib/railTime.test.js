import { afterEach, describe, expect, it, vi } from 'vitest'
import { absoluteWithZone, dayLabel, fmtWhen, relativeTime, relativeUntil } from './railTime.js'

const NOW = Date.parse('2026-10-05T19:14:00Z')
const dateLabel = (ms) => new Date(ms).toLocaleDateString(undefined, { month: 'short', day: 'numeric' })

afterEach(() => vi.useRealTimers())

describe('relativeTime in epoch milliseconds', () => {
  it.each([
    [-60_000, 'now'],
    [0, 'now'],
    [59_999, 'now'],
    [60_000, '1 m'],
    [3_599_999, '59 m'],
    [3_600_000, '1 h'],
    [86_399_999, '23 h'],
  ])('formats an age of %i ms as %s', (age, expected) => {
    expect(relativeTime(NOW - age, NOW)).toBe(expected)
  })

  it('switches to a date at one day and keeps the timestamp date for older events', () => {
    for (const age of [86_400_000, 10 * 86_400_000]) {
      expect(relativeTime(NOW - age, NOW)).toBe(dateLabel(NOW - age))
    }
    expect(relativeTime(0, NOW)).toBe(dateLabel(0))
  })

  it('uses the current clock when now is omitted', () => {
    vi.useFakeTimers()
    vi.setSystemTime(NOW)
    expect(relativeTime(NOW - 120_000)).toBe('2 m')
  })

  it('names unreadable timestamps instead of displaying NaN or Invalid Date', () => {
    for (const value of [undefined, null, NaN, Infinity, 'banana', '2026-10-05', 9e15]) {
      expect(relativeTime(value, NOW)).toBe('Time unavailable')
    }
    expect(relativeTime(NOW, NaN)).toBe('Time unavailable')
  })
})

describe('absoluteWithZone', () => {
  it('uses Chicago and a CT label even when daylight saving time is in effect', () => {
    expect(absoluteWithZone(NOW)).toBe('Oct 5, 2:14 PM CT')
    expect(absoluteWithZone(Date.parse('2026-01-05T20:14:00Z'))).toBe('Jan 5, 2:14 PM CT')
  })

  it('uses the requested zone for both the date and clock', () => {
    const midnight = Date.parse('2026-10-06T00:14:00Z')
    expect(absoluteWithZone(midnight)).toBe('Oct 5, 7:14 PM CT')
    expect(absoluteWithZone(midnight, { timeZone: 'America/New_York' })).toBe('Oct 5, 8:14 PM ET')
    expect(absoluteWithZone(midnight, { timeZone: 'UTC' })).toBe('Oct 6, 12:14 AM UTC')
  })

  it('omits a hover title for missing or invalid milliseconds, but accepts epoch zero', () => {
    for (const value of [undefined, null, NaN, Infinity, 'banana', 9e15]) {
      expect(absoluteWithZone(value)).toBeUndefined()
    }
    expect(absoluteWithZone(0, { timeZone: 'UTC' })).toBe('Jan 1, 12:00 AM UTC')
  })
})

describe('relativeUntil preserves the lease horizon', () => {
  it.each([
    [1, '~1 m'],
    [20_000, '~1 m'],
    [40 * 60_000, '~40 m'],
    [60 * 60_000, '~1 h'],
    [4 * 3_600_000, '~4 h'],
  ])('formats a live lease with %i ms left as %s', (remaining, expected) => {
    expect(relativeUntil(NOW + remaining, NOW)).toBe(expected)
  })

  it('uses a date at a day, including the existing rounded-minute boundary', () => {
    for (const remaining of [86_400_000 - 20_000, 86_400_000, 2 * 86_400_000]) {
      expect(relativeUntil(NOW + remaining, NOW)).toBe(dateLabel(NOW + remaining))
    }
  })

  it('shows no horizon at or after expiry or for an unreadable expiry', () => {
    for (const value of [NOW, NOW - 1, undefined, null, NaN, Infinity, 'banana', 9e15]) {
      expect(relativeUntil(value, NOW)).toBeNull()
    }
    expect(relativeUntil(NOW + 20_000, NaN)).toBeNull()
  })

  it('uses the current clock when now is omitted', () => {
    vi.useFakeTimers()
    vi.setSystemTime(NOW)
    expect(relativeUntil(NOW + 20_000)).toBe('~1 m')
  })
})

describe('existing rail presentation', () => {
  it('retains seconds, milliseconds and ISO inputs, with the original absolute fields', () => {
    vi.useFakeTimers()
    vi.setSystemTime(NOW)
    const ms = NOW - 120_000
    const d = new Date(ms)
    for (const timestamp of [ms / 1000, ms, d.toISOString()]) {
      expect(fmtWhen(timestamp)).toEqual({
        rel: '2 m',
        abs: d.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }),
        clock: d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' }),
        day: d.toDateString(),
        date: d,
      })
    }
    expect(fmtWhen(null)).toBeNull()
    expect(fmtWhen('banana')).toBeNull()
  })

  it('retains Today, Yesterday and older day labels', () => {
    vi.useFakeTimers()
    vi.setSystemTime(NOW)
    expect(dayLabel(new Date(NOW))).toBe(`Today · ${dateLabel(NOW)}`)
    expect(dayLabel(new Date(NOW - 86_400_000))).toBe(`Yesterday · ${dateLabel(NOW - 86_400_000)}`)
    expect(dayLabel(new Date(NOW - 3 * 86_400_000))).toBe(dateLabel(NOW - 3 * 86_400_000))
  })
})
