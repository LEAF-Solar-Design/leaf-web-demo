import { manifestFor } from '../canvas/registry.js'
import { authHeaders, config } from '../../api.js'

const validId = (id) => typeof id === 'string' && /^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}(?![\s\S])/.test(id)
async function request(docId, suffix, { fetch: fetchImpl = globalThis.fetch, timeout = 10000, ...options } = {}) {
  if (!validId(docId)) return { ok: false, status: 400, detail: 'Enter a valid document ID.' }
  const controller = new AbortController()
  let timer
  try {
    const response = await Promise.race([
      fetchImpl(`${config.apiBase}/api/lab/${suffix === 'values' ? 'widgets' : 'docs'}/${encodeURIComponent(docId)}/${suffix}`, {
        ...options, method: options.method ?? 'GET', credentials: 'same-origin', redirect: 'error', signal: controller.signal,
        headers: { accept: 'application/json', 'X-Tenant-Id': config.tenant, ...authHeaders(), ...options.headers },
      }).then(async (res) => {
        let body, readable = true
        try { body = await res.json() }
        catch { readable = false }
        return { res, body, readable }
      }),
      new Promise((_, reject) => { timer = setTimeout(() => { controller.abort(); reject(new Error('Request timed out.')) }, Math.min(Math.max(timeout, 1), 30000)) }),
    ])
    const { res, body, readable } = response
    if (!res.ok) return { ok: false, status: res.status, detail: body?.detail ?? 'The document request failed.', pointer: body?.pointer ?? body?.detail?.pointer }
    if (!readable || !body || typeof body !== 'object' || Array.isArray(body)) return { ok: false, status: 502, detail: 'The document store returned an unreadable response.' }
    return { ...body, ok: true }
  } catch (error) { return { ok: false, status: 0, detail: controller.signal.aborted || error.name === 'AbortError' ? 'Request timed out.' : 'Unable to reach the document store.' } }
  finally { clearTimeout(timer) }
}
export async function loadWidget(docId, options) {
  const result = await request(docId, 'head', options)
  if (result.status === 404) return { ok: true, doc: null, revision: null, sha256: null }
  if (result.ok && result.kind && result.kind !== 'widget') return { ok: false, status: 409, detail: 'lab_document_kind_fixed' }
  if (result.ok && !result.graph) return { ok: false, status: 502, detail: 'The stored widget document is missing.' }
  return result.ok ? { ...result, doc: result.graph } : result
}
export function saveWidget(docId, doc, expectedHead, options) {
  return request(docId, 'save', { ...options, method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ graph: doc, expected_head: expectedHead, kind: 'widget' }) })
}
export function resolveValues(docId, bindings, options) {
  return request(docId, 'values', { ...options, method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ bindings }) })
}
export async function loadGraphOutputs(docId, options) {
  const result = await request(docId, 'head', options)
  if (!result.ok) return result
  try {
    if (result.kind && result.kind !== 'graph') throw new Error('Choose a graph document.')
    const graph = result.graph
    const outputs = Object.entries(graph.outputs).map(([name, endpoint]) => {
      const block = graph.blocks.find((candidate) => candidate.id === endpoint.node)
      const pin = manifestFor(block).outputs[endpoint.port]
      if (!pin) throw new Error('Unknown output pin.')
      return { name, type: `${pin.mode}<${pin.schema.type}>`, pin }
    })
    return { ok: true, outputs }
  } catch { return { ok: false, status: 422, detail: 'Graph outputs could not be verified against the block registry.' } }
}
