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

beforeEach(() => {
  try { sessionStorage.clear() } catch { /* jsdom always has it */ }
  // React logs the caught error; that noise is not the subject here.
  consoleError = vi.spyOn(console, 'error').mockImplementation(() => {})
})

afterEach(() => {
  consoleError?.mockRestore()
  delete globalThis.fetch
})

/** The events the boundary's dynamic telemetry import actually posted. */
async function postedAfterCrash(childProps) {
  const fetchMock = vi.fn(() => Promise.resolve({ ok: true, status: 202 }))
  globalThis.fetch = fetchMock
  vi.resetModules()
  const telemetry = await import('./telemetry.js')

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

  it('records ONE React crash as exactly two rows, told apart by `source`', async () => {
    const events = await postedAfterCrash({})

    // React re-throws a boundary-caught error to `window`, so the global
    // handler sees it too. The pair is deliberate -- each half carries what
    // the other cannot -- but anything COUNTING crashes must filter on
    // `source` or it double-counts every React failure. This spec is what
    // stops that contract being broken silently.
    expect(events).toHaveLength(2)
    for (const e of events) {
      expect(e).toMatchObject({ event_name: 'client.exception', event_type: 'exception' })
      expect(e.labels.message_class).toBe('Error')
    }

    const [globalRow, boundaryRow] = events
    expect(globalRow.labels.source).toBe('window.onerror')
    expect(globalRow.labels.stack_hash).toMatch(/^\d+$/)
    expect(globalRow.labels.route).toBe('site')

    expect(boundaryRow.labels.source).toBeUndefined()
    expect(boundaryRow.labels.component_stack_hash).toMatch(/^\d+$/)
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
