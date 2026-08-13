import assert from 'node:assert/strict'
import { afterEach, test } from 'node:test'
import { readFile } from 'node:fs/promises'

import {
  ensureSession,
  postMessage,
  projectActivityProjection,
  resetSession,
  sessionCacheKey,
} from '../converse.js'

const originalFetch = globalThis.fetch

afterEach(() => {
  globalThis.fetch = originalFetch
})

test('project-aware cache keys recover the same project without cross-project reuse', async () => {
  const bodies = []
  let sequence = 0
  globalThis.fetch = async (_url, options) => {
    bodies.push(JSON.parse(options.body))
    sequence += 1
    return new Response(JSON.stringify({ session_id: `session-${sequence}` }), { status: 201 })
  }

  resetSession('drawing-a', 'project-a')
  resetSession('drawing-a', 'project-b')
  const first = await ensureSession('drawing-a', 'project-a')
  const recovered = await ensureSession('drawing-a', 'project-a')
  const other = await ensureSession('drawing-a', 'project-b')

  assert.equal(first.session_id, recovered.session_id)
  assert.notEqual(first.session_id, other.session_id)
  assert.deepEqual(bodies, [
    { drawing_id: 'drawing-a', project_id: 'project-a' },
    { drawing_id: 'drawing-a', project_id: 'project-b' },
  ])
  assert.notEqual(sessionCacheKey('drawing-a', 'project-a'), sessionCacheKey('drawing-a', 'project-b'))
})

test('legacy session creation retains its existing wire shape', async () => {
  let body
  globalThis.fetch = async (_url, options) => {
    body = JSON.parse(options.body)
    return new Response(JSON.stringify({ session_id: 'legacy-session' }), { status: 201 })
  }
  resetSession('legacy-drawing')
  await ensureSession('legacy-drawing')
  assert.deepEqual(body, { drawing_id: 'legacy-drawing' })
})

test('project request identity and server activity projection stay truthful', async () => {
  let body
  globalThis.fetch = async (_url, options) => {
    body = JSON.parse(options.body)
    return new Response(JSON.stringify({
      request_id: body.request_id,
      status: 'queued',
      active_requests: { queued: 2, executing: 1, total: 3 },
    }), { status: 202 })
  }
  const requestId = '0193f47e-2c2d-7ec1-98fd-e6881268c001'
  const response = await postMessage('project-session', {
    text: 'Continue this project',
    queue: true,
    request_id: requestId,
  })
  assert.equal(body.request_id, requestId)
  assert.equal(body.queue, true)
  assert.deepEqual(projectActivityProjection(response.active_requests), { queued: 2, executing: 1, total: 3 })
})

test('workspace binding resets attachment on project change and close', async () => {
  const controller = await readFile(new URL('./useConverseSessionController.js', import.meta.url), 'utf8')
  const provider = await readFile(new URL('./WorkspaceControllerProvider.jsx', import.meta.url), 'utf8')
  const toolCast = await readFile(new URL('../site/ToolCast.jsx', import.meta.url), 'utf8')
  assert.match(controller, /setProjectContext[\s\S]*sessionRef\.current = null[\s\S]*setTurns\(\[\]\)/)
  assert.match(controller, /const requestId = requestedProjectId \? createRequestId\(\) : null[\s\S]*request_id = requestId/)
  assert.match(provider, /bindConverseProject: converse\.setProjectContext/)
  assert.match(toolCast, /bindConverseProject\(workspace\.openProjectId \|\| null\)/)
  assert.match(toolCast, /bindConverseProject\(null\)[\s\S]*workspace\.closeProject\(\)/)
  assert.match(toolCast, /activeRequests\.queued[\s\S]*activeRequests\.executing/)
})
