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

/** The boundary's OWN row: the one WITHOUT `source` (that label belongs to
 * the global handlers only) and carrying `component_stack_hash` (only the
 * boundary can compute it). There is no cross-emitter de-duplication (see
 * telemetry.js's comment above `emitGlobalException`), so a dev-mode crash
 * may ALSO reach the global handler and produce a second, independent row;
 * these specs identify the boundary's row by shape rather than asserting a
 * total count, so a dev-mode double-emit is never something they police. */
function boundaryRow(events) {
  return events.find((ev) => ev.labels && ev.labels.source === undefined)
}

describe('ErrorBoundary telemetry', () => {
  it('renders the calm card instead of a white screen', async () => {
    await postedAfterCrash({})
    expect(screen.getByRole('alert')).toBeInTheDocument()
    expect(screen.getByText(/Something went wrong/i)).toBeInTheDocument()
  })

  it('records the boundary row for a React crash, uncapped and independent of the global handler', async () => {
    const events = await postedAfterCrash({})

    // A REAL component, a REAL render failure, driven the way React actually
    // reaches componentDidCatch. DEV React's synthetic-event re-throw can
    // ALSO reach telemetry's global handler for the same crash; production
    // React's try/catch does not, so that possibility is dev-build-only and
    // is not asserted against here (see telemetry.js's comment above
    // `emitGlobalException` for the full account, including why a shared
    // occurrence key cannot be derived reliably on the client).
    const row = boundaryRow(events)
    expect(row).toBeDefined()
    expect(row).toMatchObject({
      event_name: 'client.exception',
      event_type: 'exception',
    })
    expect(row.labels.message_class).toBe('Error')
    expect(row.labels.component_stack_hash).toMatch(/^\d{16}$/)
    expect(row.labels.dedup_key).toBeUndefined()
  })

  it('refuses a message_class the platform did not assign', async () => {
    const events = await postedAfterCrash({ name: 'owner@example.com' })

    // Every row the browser emits refuses it, not just the boundary's: the
    // door also schema-filters, but a row carrying free text has already
    // left the browser by then.
    for (const ev of events) expect(ev.labels.message_class).toBe('Other')
    expect(JSON.stringify(events)).not.toContain('owner@example.com')

    const row = boundaryRow(events)
    expect(row).toBeDefined()
    expect(row.labels.message_class).toBe('Other')
  })
})
