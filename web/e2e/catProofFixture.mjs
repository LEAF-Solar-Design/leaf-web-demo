import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
const ROOT = join(HERE, '..', '..')

export const REQUEST = 'Rearrange the existing panels in this drawing into the shape of a sitting cat. Preserve every panel, create a new version, and show me the proposed change before anything runs.'
export const CAT_TOOL = {
  name: 'arrange-panels-as-cat',
  version: '1.0.0',
  description: 'Rearrange existing panels into a sitting cat.',
  kind: 'script',
  capabilities: ['drawing.write'],
  params: {
    type: 'object',
    properties: {
      template: { type: 'string', default: 'sitting-v1' },
      drawing_id: { type: 'string', default: 'cat-panels' },
      expected_head: { type: 'number', default: 1 },
    },
  },
  provenance: { author: 'user' },
}
export const COUNT_TOOL = {
  name: 'count-panels',
  version: '1.0.0',
  description: 'Count every panel in the current drawing.',
  kind: 'script',
  capabilities: ['drawing.read'],
  params: { type: 'object', properties: {} },
  provenance: { author: 'user' },
}
export const CAT_PROJECT = { project_id: 'cat-project', name: 'Cat Roof', status: 'active' }
export const CAT_PROJECT_VERSION = {
  version_id: 'cat-version-1', drawing_id: 'cat-panels', seq: 1,
  org_id: 'cat-proof-org', project_id: 'cat-project',
}
export const AUTHORED_TOOL = {
  name: 'count-panels-near-edge',
  version: '1.0.0',
  description: 'Count panels within 24 inches of the roof edge.',
  kind: 'script',
  entry: 'tools/count_panels_near_edge.py',
  capabilities: ['drawing.read'],
  params: {
    type: 'object',
    properties: { distance_in: { type: 'number', default: 24 } },
  },
  provenance: {
    author: 'agent', created: '2026-07-24T18:00:00Z', run_id: 'author-run-0001',
    sha256: 'a'.repeat(64), grants: ['drawing.read'], reviewer: null,
  },
}
export const AUTHORED_CATALOG_DIGEST = `sha256:${'f'.repeat(64)}`
const AUTHOR_RECEIPT = {
  contract: 'leaf.customization.v1', tenant_id: 'cat-litmus-tenant',
  change_set_id: '11111111-1111-4111-8111-111111111111', state: 'staged',
  base_commit: 'b'.repeat(40), staged_commit: 'c'.repeat(40), catalog_digest: 'd'.repeat(64),
  platform_release: 'leaf-platform-2026.07.24', workspace_contract_digest: 'e'.repeat(64),
}

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
  return {
    base, cat, count: handles.length, head: 1, events: [], catalogJob: false,
    authorStaged: false, independentApproved: false, authorPublished: false, authorJob: false,
    grantLinked: true, grantKind: 'oauth',
    checkout: null,
    uploaded: false, uploadCount: 0,
  }
}

export function catProofResponse({ method, path, body = {}, query = {} }, state) {
  const json = (value, status = 200) => ({ status, body: value })
  if (method === 'OPTIONS') return { status: 204, body: null }
  if (path === '/api/converse/mcp') return json({ servers: [] })
  if (path === '/api/converse/registry') return json({ entries: [], counts: {} })
  if (path === '/api/skills') return json({ skills: [] })
  if (path === '/api/overlay') return json({ tokens: {}, document_version: 0, pending_proposal_id: null })
  if (path === '/api/agent/approvals/pending') return json({ approvals: [] })
  if (path === '/api/telemetry' && method === 'POST') return { status: 204, body: null }
  if (path === '/api/site/guest-upload-policy') return json({
    enabled: true, retention_hours: 24, max_bytes: 1024 * 1024,
    accepted: ['.dwg', '.dxf'], extract_live: false, dxf_local_ok: true,
  })
  if (path === '/api/drawings/upload' && method === 'POST') {
    state.uploaded = true
    state.uploadCount += 1
    return json({
      drawing_id: 'uploaded-cat', tenant_id: 'cat-litmus-tenant', tenant_kind: 'account',
      retention_expires_at: null, guest_session: null, status: 'extracting',
    }, 202)
  }
  if (path === '/api/drawings/uploaded-cat/upload-status') return json({
    status: 'ready', error: null, filename: 'cat-proof.dxf', uploaded_at: '2026-07-24T12:00:00Z',
    retention_expires_at: null,
  })
  if (path === '/api/drawings/uploaded-cat/intake') return json({
    drawing_id: 'uploaded-cat', intake: state.base, version: 1, head: 1, latest: 1,
  })
  if (path === '/api/drawings/uploaded-cat/versions') return json({
    drawing_id: 'uploaded-cat', head: 1, latest: 1, checkout: state.checkout,
    versions: [{ v: 1, parent: null, tool: 'upload', note: 'Uploaded drawing' }],
  })
  if (path === '/api/session') return json({ intake: state.base, tenant_id: 'cat-litmus-tenant', tier: 'proof', org_id: 'cat-proof-org' })
  if (path === '/api/projects' && method === 'GET') return json({ projects: [CAT_PROJECT] })
  if (path === '/api/projects/cat-project' && method === 'GET') return json({
    project: CAT_PROJECT,
    drawing_versions: [CAT_PROJECT_VERSION],
    jobs: state.catalogJob
      ? [{ job_id: 'catalog-job-0001', kind: 'run', tool_name: 'count-panels', status: 'succeeded', created_at: '2026-07-24T12:01:00Z' }]
      : [],
    built_tools: [],
  })
  if (path === '/api/tools') return json({
    tools: [
      COUNT_TOOL,
      CAT_TOOL,
      ...(state.authorPublished
        ? [{ ...AUTHORED_TOOL, catalog_digest: AUTHORED_CATALOG_DIGEST }]
        : []),
    ],
  })
  if (path === '/api/capabilities') return json({
    families: [{
      family_id: 'drawing-tools',
      label: 'Drawing tools',
      description: 'Read and transform the current drawing.',
      capabilities: [COUNT_TOOL, CAT_TOOL],
    }, ...(state.authorPublished ? [{
      family_id: 'custom-authored',
      label: 'Custom authored tools',
      description: 'Published tools for this tenant.',
      capabilities: [AUTHORED_TOOL],
    }] : [])],
    source: 'registry',
  })
  if (path === '/api/entitlements') return json({ tier: 'proof', entitlements: { run_read: true, run_write: true, build: true, converse: true } })
  if (path === '/api/tenant/claude-grant' && method === 'GET') return json({
    linked: state.grantLinked,
    kind: state.grantLinked ? state.grantKind : null,
    linked_at: state.grantLinked ? '2026-07-24T12:00:00Z' : null,
  })
  if (path === '/api/tenant/claude-grant' && method === 'DELETE') {
    state.grantLinked = false
    state.grantKind = null
    return json({ linked: false, linked_at: null })
  }
  if (path === '/api/tenant/claude-grant' && method === 'POST') {
    state.grantLinked = true
    state.grantKind = body.kind || 'oauth'
    return json({ linked: true, kind: state.grantKind, linked_at: '2026-07-24T12:10:00Z' })
  }
  if (path === '/api/health') return json({ ok: true, aps_live: false, n_tools: 1, n_authored: 1 })
  if (path === '/api/usage') return json({
    today: { runs: state.head - 1, usd_est: 0 },
    total: { runs: state.head - 1, usd_est: 0 },
    cap: { usd_cap: 10, remaining: 10, enabled: true },
  })
  if (path === '/api/jobs') return json({ jobs: [
    ...(state.catalogJob ? [{ job_id: 'catalog-job-0001', status: 'complete', tool: 'count-panels', elapsed_ms: 120 }] : []),
    ...(state.authorJob ? [{ job_id: 'author-job-0001', status: 'complete', tool: AUTHORED_TOOL.name, elapsed_ms: 160 }] : []),
  ] })
  if (path === '/api/author/stage' && method === 'POST') {
    state.authorStaged = true
    return json({
      receipt: { ...AUTHOR_RECEIPT, idempotency_key: body.idempotency_key },
      tool: AUTHORED_TOOL,
      preview: 'Counts panels whose bounds are within 24 inches of the roof edge.',
      code: 'def run(ctx, distance_in=24):\n    return count_near_edge(ctx, distance_in)\n',
      source: 'harness', static_scan: [], validation: { status: 'passed', checks: 7 },
      diff_summary: 'Adds one tenant-owned tool and one catalog entry.',
      telemetry: { turns: 2, input_tokens: 1200, output_tokens: 480, total_cost_usd: 0.012, models: ['fixture-agent'] },
    })
  }
  if (path === '/api/author/publication-requests' && method === 'POST') {
    if (!state.independentApproved) return json({
      contract: 'leaf.customization.v1',
      change_set_id: AUTHOR_RECEIPT.change_set_id,
      status: 'awaiting_approval',
    })
    state.authorPublished = true
    return json({
      contract: 'leaf.customization.v1', tenant_id: 'cat-litmus-tenant',
      change_set_id: AUTHOR_RECEIPT.change_set_id, status: 'published',
      catalog_digest: AUTHOR_RECEIPT.catalog_digest,
    })
  }
  if (path === '/api/run' && method === 'POST' && body.tool === 'count-panels') {
    state.catalogJob = true
    return json({ job_id: 'catalog-job-0001' }, 202)
  }
  if (path === '/api/run' && method === 'POST' && body.tool === AUTHORED_TOOL.name) {
    if (body.catalog_digest !== AUTHORED_CATALOG_DIGEST) {
      return json({
        ok: false,
        error: {
          error_code: 'BAD_PARAMS',
          message: 'catalog tool changed or confirmation digest is missing; refresh tools and confirm again',
          retryable: false,
        },
      }, 409)
    }
    state.authorJob = true
    return json({ job_id: 'author-job-0001' }, 202)
  }
  if (path === '/api/jobs/catalog-job-0001' && method === 'GET') return json({
    job_id: 'catalog-job-0001', status: 'complete', tool: 'count-panels', elapsed_ms: 120,
    result: {
      ok: true, tool: 'count-panels', version: '1.0.0', timing_ms: 120,
      cost: { engine_seconds: 0.12, usd_est: 0 }, error: null, degraded_mode: false,
      overlay: { highlight_handles: ['P0000'], markers: [], polylines: [] },
      result: { count: state.count, layers: { PANELS: state.count } },
    },
  })
  if (path === '/api/jobs/catalog-job-0001/stream') return { status: 204, body: null }
  if (path === '/api/jobs/author-job-0001' && method === 'GET') return json({
    job_id: 'author-job-0001', status: 'complete', tool: AUTHORED_TOOL.name, elapsed_ms: 160,
    result: {
      ok: true, tool: AUTHORED_TOOL.name, version: '1.0.0', timing_ms: 160,
      cost: 0.001, error: null, degraded_mode: false, overlay: null,
      result: { count: state.count, distance_in: 24 },
    },
  })
  if (path === '/api/jobs/author-job-0001/stream') return { status: 204, body: null }
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
  if (path === '/api/drawings/cat-panels/intake') {
    const requested = query.version
    const shownVersion = requested && requested !== 'head' ? Number(requested) : state.head
    return json({
      drawing_id: 'cat-panels',
      intake: shownVersion === 2 ? state.cat : state.base,
      version: shownVersion,
      head: state.head,
      latest: 2,
    })
  }
  if (path === '/api/drawings/cat-panels/versions') return json({
    drawing_id: 'cat-panels', head: state.head, latest: 2, checkout: state.checkout,
    versions: [
      { v: 1, parent: null, tool: 'base', note: 'Original drawing' },
      { v: 2, parent: 1, tool: 'arrange-panels-as-cat', note: 'Sitting cat, oracle pass' },
    ],
  })
  if (/^\/api\/drawings\/(cat-panels|uploaded-cat)\/checkout$/.test(path) && method === 'POST') {
    state.checkoutCapability = 'proof-checkout-capability'
    state.checkout = {
      holder: body.holder || 'cat-litmus-tenant',
      acquired: '2026-07-24T12:00:00Z',
      expires: '2099-07-25T23:00:00Z',
    }
    return json({
      acquired: true,
      checkout_capability: state.checkoutCapability,
      checkout: state.checkout,
    })
  }
  if (/^\/api\/drawings\/(cat-panels|uploaded-cat)\/checkout$/.test(path) && method === 'DELETE') {
    state.checkout = null
    state.checkoutCapability = null
    return json({ released: true, checkout: null })
  }
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
