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

describe('ErrorBoundary telemetry', () => {
  it('renders the calm card instead of a white screen', async () => {
    await postedAfterCrash({})
    expect(screen.getByRole('alert')).toBeInTheDocument()
    expect(screen.getByText(/Something went wrong/i)).toBeInTheDocument()
  })

  it('always records the boundary row, and any global row agrees with it', async () => {
    const events = await postedAfterCrash({})

    // Asserted BY SHAPE, never by count or index. React 18 DEV dispatches a
    // synthetic DOM event so DevTools can observe render exceptions, which
    // makes the global handler fire too; the production implementation uses
    // try/catch and need not reach `window`. Pinning "exactly two rows" would
    // pin a dev-build artifact and mislead anyone reading it as a production
    // guarantee.
    const boundary = events.filter((e) => e.labels.source === undefined)
    const global = events.filter((e) => e.labels.source !== undefined)

    expect(boundary).toHaveLength(1)
    expect(boundary[0]).toMatchObject({
      event_name: 'client.exception',
      event_type: 'exception',
    })
    expect(boundary[0].labels.message_class).toBe('Error')
    expect(boundary[0].labels.component_stack_hash).toMatch(/^\d{16}$/)

    // Whether a global row appears is React's business; if one does, it is
    // the same failure and `source` is what tells the two apart. That filter
    // is the contract consumers depend on to avoid double-counting.
    for (const g of global) {
      expect(g.labels.source).toBe('window.onerror')
      expect(g.labels.message_class).toBe('Error')
      expect(g.labels.stack_hash).toMatch(/^\d{16}$/)
    }
  })

  it('refuses a message_class the platform did not assign, on BOTH rows', async () => {
    const events = await postedAfterCrash({ name: 'owner@example.com' })

    expect(events.length).toBeGreaterThan(0)
    for (const e of events) {
      expect(e.labels.message_class).toBe('Other')
    }
    expect(JSON.stringify(events)).not.toContain('owner@example.com')
  })
})
