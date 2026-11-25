import { afterEach, describe, expect, it, vi } from 'vitest'
import { createProject, isWorkspaceBootstrapRequired } from './api.js'

afterEach(() => vi.unstubAllGlobals())

describe('project creation intent', () => {
  const project = { project_id: 'p1', name: 'Maple' }
  const reply = (created) => new Response(JSON.stringify({ project, created }), { status: 200 })

  it.each([true, false])('preserves the default project result when created is %s', async (created) => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(reply(created)))
    expect(await createProject('Maple', 'o1')).toEqual(project)
  })

  it('allows deliberate replay and rejects only explicit fresh-create collisions', async () => {
    const fetch = vi.fn().mockImplementation(() => Promise.resolve(reply(false)))
    vi.stubGlobal('fetch', fetch)
    expect(await createProject('Maple', 'o1', { requireCreated: false })).toEqual(project)
    await expect(createProject('Maple', 'o1', { requireCreated: true })).rejects.toMatchObject({
      code: 'project_name_collision', message: expect.stringMatching(/Choose a different name.*open the existing project/),
    })
    expect(JSON.parse(fetch.mock.calls[1][1].body)).toEqual({ name: 'Maple' })
  })

  it('returns a unique fresh project without changing the public result shape', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(reply(true)))
    expect(await createProject('Maple', 'o1', { requireCreated: true })).toEqual(project)
  })
})

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
