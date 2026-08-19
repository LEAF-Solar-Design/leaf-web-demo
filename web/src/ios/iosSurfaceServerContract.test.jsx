/**
 * Card D-4 (attempt 2): renders the REAL, D-2-merged IosSurface.jsx fed by a
 * contract captured from the REAL server route + REAL validate_contract
 * (server/tests/test_ios_surface_e2e_harness.py, card D-4's Python half) --
 * never a reimplemented validator.
 *
 * The fixture at ../../../harness/tests/fixtures/ios_surface_contract.receipt.json
 * is the server-validated output of routers.ios_surface.validate_contract for
 * the terminal RECEIPT stage, written by that Python test's own TestClient
 * call against the real FastAPI route and checked in here so this file has
 * no run-order dependency on the Python half.
 */
import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'

import IosSurface from './IosSurface.jsx'

afterEach(cleanup)

const FIXTURE_PATH = resolve(
  dirname(fileURLToPath(import.meta.url)),
  '..', '..', '..', 'harness', 'tests', 'fixtures', 'ios_surface_contract.receipt.json',
)

const serverValidatedReceiptContract = JSON.parse(readFileSync(FIXTURE_PATH, 'utf8'))

describe('IosSurface fed by the real server-validated contract (card D-4)', () => {
  it('the fixture is exactly the real router contract shape (D-1 schema, no invented fields)', () => {
    expect(serverValidatedReceiptContract).toEqual({
      schema: 'leaf.ios-ship-surface.v1',
      project_id: 'proj-1',
      revision: 'r1',
      reported_at: '2026-08-19T12:00:00+00:00',
      readiness: { healthy: true, launchable: true },
      build_stage: 'RECEIPT',
      receipt_id: 'receipt-ship-1',
    })
  })

  it('renders "ready" from the real, server-validated terminal-receipt contract', () => {
    render(<IosSurface enabled contract={serverValidatedReceiptContract} />)
    const el = screen.getByLabelText('iOS readiness')
    expect(el).toHaveAttribute('data-state', 'ready')
    expect(screen.getByText('Ready')).toBeInTheDocument()
  })

  it('with ios_surface off, the same real server contract still yields the dormant placeholder', () => {
    render(<IosSurface enabled={false} contract={serverValidatedReceiptContract} />)
    const el = screen.getByLabelText('iOS readiness')
    expect(el).toHaveAttribute('data-state', 'dormant')
    expect(screen.queryByText('Ready')).not.toBeInTheDocument()
  })
})
