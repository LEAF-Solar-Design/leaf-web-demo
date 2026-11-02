import { afterEach, expect, it, vi } from 'vitest'
vi.mock('./api.js', () => ({ config: { apiBase: '', tenant: 'T' }, authHeaders: () => ({}), noteUnauthorized: vi.fn() }))
vi.mock('./telemetry.js', () => ({ trackErrorShown: vi.fn(), trackStreamDown: vi.fn() }))
import { postMessage } from './converse.js'

afterEach(() => vi.unstubAllGlobals())

async function body(args) {
  const fetch = vi.fn().mockResolvedValue({ status: 202, ok: true, json: async () => ({ turn_id: 't1' }) })
  vi.stubGlobal('fetch', fetch)
  await postMessage('S', args)
  return fetch.mock.calls[0][1].body
}

it('test_absent_binding_preserves_legacy_body_and_behavior', async () => {
  expect(await body({ text: 'move A' })).toBe('{"text":"move A"}')
  expect(await body({ confirm: { confirmationId: 'c1', approved: true } })).toBe('{"confirm":{"confirmationId":"c1","approved":true}}')
})

it('serializes explicit binding and preserves explicit null', async () => {
  expect(await body({ text: 'move A', entity_scope: { drawing_id: 'D', handle: 'AB12' } })).toBe('{"text":"move A","entity_scope":{"drawing_id":"D","handle":"AB12"}}')
  expect(await body({ text: 'move A', entity_scope: null })).toBe('{"text":"move A","entity_scope":null}')
})

const projectContext = {
  project_id: '11111111-1111-4111-8111-111111111111',
  input_version_id: '22222222-2222-4222-8222-222222222222',
}

it('sends only the selected project/version request pin, separate from model hints', async () => {
  const sent = JSON.parse(await body({ text: 'read selected version',
    projectContext: { ...projectContext, organization_id: 'never-client-authority', drawing_id: 'not-client-authority' },
    classifier_hint: { project_id: 'advisory-only' },
  }))
  expect(sent.project_context).toEqual(projectContext)
  expect(sent.classifier_hint).toEqual({ project_id: 'advisory-only' })
  expect(sent).not.toHaveProperty('projectContext')
})

it('pins image-only requests and omits absent or null selection', async () => {
  const images = [{ media_type: 'image/png', data: 'c2FtcGxl' }]
  expect(JSON.parse(await body({ images, projectContext }))).toEqual({ images, project_context: projectContext })
  expect(await body({ text: 'discuss', projectContext: null })).toBe('{"text":"discuss"}')
})

it('confirmation requests cannot retarget a stored proposal', async () => {
  const confirm = { confirmationId: 'c1', approved: true }
  expect(JSON.parse(await body({ confirm, projectContext }))).toEqual({ confirm })
})
