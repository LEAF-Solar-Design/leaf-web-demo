// API layer — the single seam between the frontend and the backend.
// In MOCK mode (default) it serves /api/tools, /api/run, /api/author from
// local fixtures + a client-side engine, so `npm run dev` demos with no
// backend. In LIVE mode it proxies to the Lane D server (CONTRACT §4).

import registry from './mock/registry.json'
import { runMock } from './mock/mockEngine.js'
import { authorMock } from './mock/mockAuthor.js'

const API_BASE = import.meta.env.VITE_API_BASE || 'http://localhost:8130'
// Default to mock unless explicitly disabled (VITE_MOCK=0).
const MOCK_DEFAULT = import.meta.env.VITE_MOCK !== '0'

export const config = { apiBase: API_BASE, mockDefault: MOCK_DEFAULT }

// A tiny artificial delay so mock runs show loading states like the real thing.
const nap = (ms) => new Promise((r) => setTimeout(r, ms))

async function http(path, opts) {
  const res = await fetch(`${API_BASE}${path}`, opts)
  if (!res.ok) throw new Error(`${opts?.method || 'GET'} ${path} -> ${res.status}`)
  return res.json()
}

// --- Session / intake ---------------------------------------------------
export async function getSession(mock, dwg = 'rooftop_demo') {
  if (mock) {
    const res = await fetch('/sample.intake.json')
    if (!res.ok) throw new Error('failed to load sample.intake.json')
    return await res.json()
  }
  const data = await http(`/api/session?dwg=${encodeURIComponent(dwg)}`)
  return data.intake
}

// --- Tools --------------------------------------------------------------
export async function getTools(mock) {
  if (mock) {
    await nap(150)
    // Clone so callers can safely append authored tools.
    return registry.tools.map((t) => ({ ...t }))
  }
  const data = await http('/api/tools')
  return data.tools
}

// --- Run ----------------------------------------------------------------
// `intake` is only used in mock mode (client-side engine).
export async function runTool(mock, tool, params, intake, dwg = 'rooftop_demo') {
  if (mock) {
    await nap(400 + Math.random() * 500)
    return runMock(tool, params, intake)
  }
  return http('/api/run', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ tool: tool.name, params: params || {}, dwg }),
  })
}

// --- Author -------------------------------------------------------------
export async function authorTool(mock, description) {
  if (mock) {
    await nap(700)
    return authorMock(description)
  }
  return http('/api/author', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ description }),
  })
}
