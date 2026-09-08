import { describe, expect, it } from 'vitest'
import { isWorkspaceBootstrapRequired } from './api.js'

const messages = [
  'verified subject has no active platform identity binding',
  'verified subject has no active platform tenant authority',
]
const formats = [
  ['legacy detail', (message) => ({ detail: message })],
  ['current error message', (message) => ({ error: { message } })],
]

describe('isWorkspaceBootstrapRequired', () => {
  for (const [format, bodyFor] of formats) {
    describe(format, () => {
      it.each(messages)('recognizes HTTP 403: %s', (message) => {
        expect(isWorkspaceBootstrapRequired({ status: 403, body: bodyFor(message) })).toBe(true)
      })

      it.each([200, 400, 401, 404, 429, 500, '403', null, undefined])('rejects status %s', (status) => {
        for (const message of messages) {
          expect(isWorkspaceBootstrapRequired({ status, body: bodyFor(message) })).toBe(false)
        }
      })

      it.each([
        'access denied',
        'grant_required',
        'verified subject has no active platform identity binding elsewhere',
        'verified subject has no active platform tenant authority ',
        '',
        null,
        undefined,
        403,
        {},
        [],
        [messages[0]],
      ])('rejects unrelated or malformed message %j', (message) => {
        expect(isWorkspaceBootstrapRequired({ status: 403, body: bodyFor(message) })).toBe(false)
      })
    })
  }

  it.each([undefined, null, {}, 'denied', { status: 403 }])('rejects incomplete error %j', (error) => {
    expect(isWorkspaceBootstrapRequired(error)).toBe(false)
  })

  it.each([null, undefined, '', 403, [], {}, { error: null }, { error: 'denied' }, { error: [] }, { message: messages[0] }])('rejects malformed body %j', (body) => {
    expect(isWorkspaceBootstrapRequired({ status: 403, body })).toBe(false)
  })

  it('recognizes either supported field when both are present', () => {
    for (const message of messages) {
      expect(isWorkspaceBootstrapRequired({ status: 403, body: { detail: 'denied', error: { message } } })).toBe(true)
      expect(isWorkspaceBootstrapRequired({ status: 403, body: { detail: message, error: { message: 'denied' } } })).toBe(true)
    }
  })
})
