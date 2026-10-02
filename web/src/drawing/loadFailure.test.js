import { describe, expect, it } from 'vitest'
import { FetchTimeoutError } from '../fetchBudget.js'
import { classifyDrawingLoadFailure } from './loadFailure.js'

describe('classifyDrawingLoadFailure', () => {
  it.each([400, 404, 410])('classifies HTTP %s as permanent', (status) => {
    const error = Object.assign(new Error(`GET /api/session -> ${status}`), { status })
    expect(classifyDrawingLoadFailure(error)).toBe('permanent')
  })

  it.each(['BAD_PARAMS', 'NOT_FOUND'])('classifies envelope %s as permanent without an HTTP status', (code) => {
    expect(classifyDrawingLoadFailure({ body: { error: { error_code: code } } })).toBe('permanent')
    expect(classifyDrawingLoadFailure({ body: { error: { code } } })).toBe('permanent')
    expect(classifyDrawingLoadFailure({ error_code: code })).toBe('permanent')
    expect(classifyDrawingLoadFailure({ code })).toBe('permanent')
  })

  it('keeps permanent metadata ahead of transient hints', () => {
    expect(classifyDrawingLoadFailure({ status: 503, body: { error: { error_code: 'NOT_FOUND' } } })).toBe('permanent')
    expect(classifyDrawingLoadFailure({ status: 404, message: 'Failed to fetch' })).toBe('permanent')
  })

  it.each([429, 500, 502, 503, 504, 599])('classifies HTTP %s as transient', (status) => {
    expect(classifyDrawingLoadFailure({ status })).toBe('transient')
  })

  it.each([
    new TypeError('Failed to fetch'),
    new TypeError('NetworkError when attempting to fetch resource.'),
    new TypeError('Load failed'),
    new FetchTimeoutError(5000),
    { name: 'TimeoutError' },
    { name: 'AbortError' },
    { name: 'NetworkError' },
    new Error('Request timed out'),
  ])('classifies network and timeout failures as transient: %s', (error) => {
    expect(classifyDrawingLoadFailure(error)).toBe('transient')
  })

  it.each([
    undefined, null, false, 404, '', 'NOT_FOUND', {}, [],
    { status: '404' }, { status: 403 }, { status: 600 },
    { body: null }, { body: { error: false } },
    { body: { error: { error_code: {} } } },
    { code: 'OTHER' }, new Error('Something happened'),
  ])('returns unknown for missing or malformed metadata: %s', (error) => {
    expect(classifyDrawingLoadFailure(error)).toBe('unknown')
  })

  it('never throws on hostile property access', () => {
    const hostile = new Proxy({}, { get() { throw new Error('Cannot read') } })
    expect(classifyDrawingLoadFailure(hostile)).toBe('unknown')
    expect(classifyDrawingLoadFailure({ body: { error: hostile } })).toBe('unknown')
    expect(classifyDrawingLoadFailure({ status: 404, body: hostile })).toBe('permanent')
    const { proxy, revoke } = Proxy.revocable({}, {})
    revoke()
    expect(classifyDrawingLoadFailure(proxy)).toBe('unknown')
  })
})
