/**
 * The boundary is the OTHER emitter of `client.exception`.
 *
 * Its labels were never covered by a spec, and it passed `error.name`
 * straight through -- so a component that render-threw an error named
 * `owner@example.com` put that text into a label, under the same event whose
 * contract promises structural labels. The global handlers being safe did
 * not make the boundary's row safe.
 *
 * These specs drive the REAL component through a real render failure.
 */
import { render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import ErrorBoundary from './ErrorBoundary.jsx'

function Boom({ name }) {
  const err = new Error('render exploded')
  if (name) err.name = name
  throw err
}

let consoleError
let installed = []

beforeEach(() => {
  try { sessionStorage.clear() } catch { /* jsdom always has it */ }
  // React logs the caught error; that noise is not the subject here.
  consoleError = vi.spyOn(console, 'error').mockImplementation(() => {})
})

afterEach(() => {
  consoleError?.mockRestore()
  // Each re-import installs listeners on the SHARED jsdom window; without
  // this they accumulate and answer later specs' events.
  for (const [type, fn, opts] of installed) window.removeEventListener(type, fn, opts)
  installed = []
  delete globalThis.fetch
})

/** The events the boundary's dynamic telemetry import actually posted. */
async function postedAfterCrash(childProps) {
  const fetchMock = vi.fn(() => Promise.resolve({ ok: true, status: 202 }))
  globalThis.fetch = fetchMock
  vi.resetModules()

  installed = []
  const realAdd = window.addEventListener.bind(window)
  window.addEventListener = (type, fn, opts) => {
    installed.push([type, fn, opts])
    return realAdd(type, fn, opts)
  }
  let telemetry
  try {
    telemetry = await import('./telemetry.js')
  } finally {
    window.addEventListener = realAdd
  }

  render(<ErrorBoundary><Boom {...childProps} /></ErrorBoundary>)
  // The boundary imports telemetry.js dynamically so it can survive whatever
  // broke below it; let that microtask settle before flushing.
  await new Promise((resolve) => setTimeout(resolve, 0))
  telemetry.flushNow()

  return fetchMock.mock.calls.flatMap(([, init]) => JSON.parse(init.body).events)
}

/** THE INGEST RULE, mirrored from server/routers/telemetry.py
 * (`_dedup_identity` + `_dedup_rank`): one row per (session, dedup_key), and
 * the row WITHOUT `source` -- the boundary's -- wins the tie. The client emits
 * both twins on purpose; this is what the door then stores. */
function dedupRows(events) {
  const winners = new Map()
  const out = []
  for (const ev of events) {
    const key = ev.labels && ev.labels.dedup_key
    if (!key) { out.push(ev); continue }
    const prior = winners.get(key)
    if (prior === undefined) {
      winners.set(key, out.length)
      out.push(ev)
    } else if (!ev.labels.source && out[prior].labels.source) {
      out[prior] = ev
    }
  }
  return out
}

describe('ErrorBoundary telemetry', () => {
  it('renders the calm card instead of a white screen', async () => {
    await postedAfterCrash({})
    expect(screen.getByRole('alert')).toBeInTheDocument()
    expect(screen.getByText(/Something went wrong/i)).toBeInTheDocument()
  })

  it('records EXACTLY ONE row for one React crash, and it is the boundary row', async () => {
    const events = await postedAfterCrash({})

    // The strongest form of this claim available anywhere in the suite: a REAL
    // component, a REAL render failure, both emitters reached the way React
    // actually reaches them. React 18's development build re-throws the render
    // error through a synthetic DOM event, so telemetry's global handler
    // answers it AND the boundary answers it. Its production build uses
    // try/catch and does not, so the browser's row count is a property of the
    // BUILD -- which is exactly why the browser is no longer allowed to decide
    // the row count.
    //
    // So: the client emits BOTH, and they agree on a key.
    expect(events).toHaveLength(2)
    expect(events.map((e) => e.event_name)).toEqual(
      ['client.exception', 'client.exception'])
    expect(events[0].labels.dedup_key).toMatch(/^\d{16}$/)
    expect(events[1].labels.dedup_key).toBe(events[0].labels.dedup_key)

    // And the door stores ONE. The boundary wins the tie: its row is the only
    // one carrying `component_stack_hash`. Asserted by COUNT on purpose --
    // "the pair by shape" is what let the double-count through.
    const rows = dedupRows(events)
    expect(rows).toHaveLength(1)
    expect(rows[0]).toMatchObject({
      event_name: 'client.exception',
      event_type: 'exception',
    })
    expect(rows[0].labels.source).toBeUndefined()
    expect(rows[0].labels.message_class).toBe('Error')
    expect(rows[0].labels.component_stack_hash).toMatch(/^\d{16}$/)
  })

  it('refuses a message_class the platform did not assign', async () => {
    const events = await postedAfterCrash({ name: 'owner@example.com' })

    // BOTH emitters must refuse it, not just the one that survives the merge:
    // the door schema-filters every row it receives, but a row carrying free
    // text has already left the browser by then.
    expect(events).toHaveLength(2)
    for (const ev of events) expect(ev.labels.message_class).toBe('Other')
    expect(JSON.stringify(events)).not.toContain('owner@example.com')

    const rows = dedupRows(events)
    expect(rows).toHaveLength(1)
    expect(rows[0].labels.message_class).toBe('Other')
  })
})
