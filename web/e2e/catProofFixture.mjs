import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
const ROOT = join(HERE, '..', '..')

export const REQUEST = 'Rearrange the existing panels in this drawing into the shape of a sitting cat. Preserve every panel, create a new version, and show me the proposed change before anything runs.'

function readPbm(path) {
  const tokens = readFileSync(path, 'utf8')
    .replace(/#[^\n]*/g, '')
    .trim()
    .split(/\s+/)
  if (tokens.shift() !== 'P1') throw new Error('cat proof expects an ASCII PBM fixture')
  const width = Number(tokens.shift())
  const height = Number(tokens.shift())
  const points = []
  for (let y = 0; y < height; y += 1) {
    for (let x = 0; x < width; x += 1) {
      if (tokens[y * width + x] === '1') points.push([x, height - y - 1])
    }
  }
  return points.sort((a, b) => a[1] - b[1] || a[0] - b[0])
}

function panel(handle, x, y, size = 1) {
  return {
    handle,
    layer: 'PANELS',
    closed: true,
    pts: [[x, y, 7], [x + size, y, 7], [x + size, y + size, 7], [x, y + size, 7]],
    xdata: { panel: handle },
    metadata: { kind: 'roof-panel' },
  }
}

function envelope(seq, turnId, type, data) {
  return { v: 1, session_id: 'cat-session', turn_id: turnId, seq, type, data }
}

export function makeCatProofState() {
  const points = readPbm(join(ROOT, 'server', 'tests', 'fixtures', 'cat_oracle', 'sitting-v1.pbm'))
  const handles = points.map((_, index) => `P${String(index).padStart(4, '0')}`)
  const base = {
    dwg: 'cat.dwg', layers: ['PANELS'], inserts: [], faces3d: [], blockdefs: {},
    polylines: handles.map((handle, index) => panel(handle, index % 64, Math.floor(index / 64))),
  }
  const cat = {
    ...base,
    polylines: handles.map((handle, index) =>
      panel(handle, points[index][0] * 100, points[index][1] * 100, 100)),
  }
  return { base, cat, count: handles.length, head: 1, events: [] }
}

export function catProofResponse({ method, path, body = {} }, state) {
  const json = (value, status = 200) => ({ status, body: value })
  if (method === 'OPTIONS') return { status: 204, body: null }
  if (path === '/api/session') return json({ intake: state.base, tenant_id: 'cat-litmus-tenant', tier: 'proof', org_id: 'cat-proof-org' })
  if (path === '/api/tools') return json({ tools: [] })
  if (path === '/api/capabilities') return json({ families: [] })
  if (path === '/api/entitlements') return json({ tier: 'proof', entitlements: { run_read: true, run_write: true, build: true, converse: true } })
  if (path === '/api/tenant/claude-grant') return json({ linked: true, linked_at: '2026-07-24T12:00:00Z' })
  if (path === '/api/health') return json({ ok: true, aps_live: false, n_tools: 1, n_authored: 1 })
  if (path === '/api/usage') return json({ today: { runs: state.head - 1, usd_est: 0 }, total: { runs: state.head - 1, usd_est: 0 } })
  if (path === '/api/jobs') return json({ jobs: [] })
  if (path === '/api/nl-prompt' && method === 'POST') return json({ lane: 'build', tool: null, params: {}, confidence: 0.42, rationale: 'The assistant must plan a controlled drawing write.', alternatives: [] })
  if (path === '/api/sessions' && method === 'POST') return json({ session_id: 'cat-session', status: 'idle', created_at: '2026-07-24T12:00:00Z' })
  if (path === '/api/sessions/cat-session/messages' && method === 'POST') {
    if (body.text) {
      state.events = [
        envelope(1, 'turn-1', 'turn_started', {}),
        envelope(2, 'turn-1', 'text_delta', { text: 'I found a tenant tool that preserves all panel identities and local geometry. I will stage a sitting-cat layout as a new version.' }),
        envelope(3, 'turn-1', 'proposed_run', { confirmation_id: 'cat-confirmation', tool: 'arrange-panels-as-cat', params: { template: 'sitting-v1', drawing_id: 'cat-panels', expected_head: 1 }, capability: 'drawing.write', rationale: 'Creates version 2 and keeps version 1 available for undo.' }),
        envelope(4, 'turn-1', 'turn_complete', { stop_reason: 'awaiting_approval' }),
      ]
      return json({ turn_id: 'turn-1', status: 'started' }, 202)
    }
    state.events.push(
      envelope(5, 'turn-2', 'turn_started', {}),
      envelope(6, 'turn-2', 'confirmation_resolved', { confirmation_id: 'cat-confirmation', approved: true, by: 'operator' }),
      envelope(7, 'turn-2', 'tool_call', { tool: 'arrange-panels-as-cat', args_summary: 'sitting-v1 · 3,328 panels · expected head v1' }),
      envelope(8, 'turn-2', 'tool_result', { tool: 'arrange-panels-as-cat', ok: true, summary: 'Cat oracle passed · IoU 1.000 · overlap 0' }),
      envelope(9, 'turn-2', 'job_linked', { job_id: 'cat-job-0002', tool: 'arrange-panels-as-cat' }),
      envelope(10, 'turn-2', 'text_delta', { text: 'Version 2 is ready. The cat oracle passed and version 1 remains available through Undo.' }),
      envelope(11, 'turn-2', 'turn_complete', { stop_reason: 'end_turn' }),
    )
    return json({ turn_id: 'turn-2', status: 'started' }, 202)
  }
  if (path === '/api/agent/approvals/cat-confirmation' && method === 'POST') return json({ resolved: true, approved: true })
  if (path === '/api/sessions/cat-session/transcript') return json({ events: state.events })
  if (path === '/api/sessions/cat-session/stream' || path === '/api/jobs/cat-job-0002/stream') return { status: 204, body: null }
  if (path === '/api/jobs/cat-job-0002' && method === 'GET') {
    state.head = 2
    return json({
      job_id: 'cat-job-0002', status: 'complete', tool: 'arrange-panels-as-cat', elapsed_ms: 5240,
      result: { ok: true, tool: 'arrange-panels-as-cat', version: '1.0.0', timing_ms: 5240, cost: null, error: null, degraded_mode: false, overlay: null, result: { panels_preserved: state.count, cat_oracle: { verdict: 'pass', template: 'sitting-v1', iou: 1, outline_chamfer_px: 0, overlap_pixels: 0 }, new_version: { drawing_id: 'cat-panels', version: 2, parent: 1 } } },
    })
  }
  if (path === '/api/drawings/cat-panels/intake') return json({ drawing_id: 'cat-panels', intake: state.head === 2 ? state.cat : state.base, version: state.head, head: state.head, latest: 2 })
  if (path === '/api/drawings/cat-panels/versions') return json({
    drawing_id: 'cat-panels', head: state.head, latest: 2, checkout: null,
    versions: [
      { v: 1, parent: null, tool: 'base', note: 'Original drawing' },
      { v: 2, parent: 1, tool: 'arrange-panels-as-cat', note: 'Sitting cat, oracle pass' },
    ],
  })
  if (path === '/api/drawings/cat-panels/undo' && method === 'POST') {
    state.head = 1
    return json({ drawing_id: 'cat-panels', intake: state.base, version: 1, head: 1, latest: 2 })
  }
  if (path === '/api/drawings/cat-panels/redo' && method === 'POST') {
    state.head = 2
    return json({ drawing_id: 'cat-panels', intake: state.cat, version: 2, head: 2, latest: 2 })
  }
  if (path === '/api/drawings/demo/versions') return json({ drawing_id: 'demo', head: 1, latest: 1, checkout: null, versions: [] })
  return json({ error: { message: `unhandled proof route ${method} ${path}` } }, 404)
}
