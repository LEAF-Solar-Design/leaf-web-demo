/**
 * ReceiptPanel. Card B-U6 acceptance oracle, one describe block per clause:
 *   Project timeline lists immutable receipts (kind, time, sanitized
 *   fields); a receipt is never editable; version pins render exactly.
 */
import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, render, screen, within } from '@testing-library/react'

import ReceiptPanel from './ReceiptPanel.jsx'

afterEach(cleanup)

const RECEIPTS = [
  {
    receipt_id: 'rcpt-1',
    kind: 'create_project',
    time: '2026-08-01T12:00:00.000Z',
    fields: { name: 'Substation A', input_digest: 'a1b2c3' },
  },
  {
    receipt_id: 'rcpt-2',
    kind: 'export',
    time: '2026-08-02T09:30:00.000Z',
    fields: {
      filename: 'project-export.zip',
      version_pin: '4f9c2e7b1a8d3f6015c9e2b7a4d81f3c6e9b2a5d7f10c3e6b9a2d5f8c1e4b7a0',
    },
  },
]

describe('acceptance: the project timeline lists immutable receipts (kind, time, sanitized fields)', () => {
  it('renders one row per receipt with its kind, time, and fields', () => {
    render(<ReceiptPanel receipts={RECEIPTS} />)

    const rows = screen.getAllByRole('listitem')
    expect(rows).toHaveLength(2)

    expect(within(rows[0]).getByText('create_project')).toBeTruthy()
    expect(within(rows[0]).getByText('Substation A')).toBeTruthy()
    expect(within(rows[0]).getByText('a1b2c3')).toBeTruthy()

    expect(within(rows[1]).getByText('export')).toBeTruthy()
    expect(within(rows[1]).getByText('project-export.zip')).toBeTruthy()
  })

  it('renders receipts in the exact order the server sent them, never re-sorted client-side', () => {
    const reversed = [RECEIPTS[1], RECEIPTS[0]]
    render(<ReceiptPanel receipts={reversed} />)
    const rows = screen.getAllByRole('listitem')
    expect(within(rows[0]).getByText('export')).toBeTruthy()
    expect(within(rows[1]).getByText('create_project')).toBeTruthy()
  })

  it('redacts a credential-shaped field even if one reached the panel un-sanitized', () => {
    const dirty = [{
      receipt_id: 'rcpt-3',
      kind: 'invite',
      time: '2026-08-03T00:00:00.000Z',
      fields: { access_token: 'super-secret-value', role: 'editor' },
    }]
    render(<ReceiptPanel receipts={dirty} />)

    expect(screen.queryByText('super-secret-value')).toBeNull()
    expect(screen.getByText('[redacted]')).toBeTruthy()
    expect(screen.getByText('editor')).toBeTruthy()
  })

  it('redacts a bearer-token-shaped or JWT-shaped value regardless of its field key', () => {
    const dirty = [{
      receipt_id: 'rcpt-4',
      kind: 'export',
      time: '2026-08-04T00:00:00.000Z',
      fields: {
        note: 'Bearer abc.def.ghi',
        jwt_like: 'eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U',
      },
    }]
    render(<ReceiptPanel receipts={dirty} />)

    expect(screen.queryByText(/Bearer abc\.def\.ghi/)).toBeNull()
    expect(screen.queryByText(/^eyJ/)).toBeNull()
    expect(screen.getAllByText('[redacted]')).toHaveLength(2)
  })

  it('shows a truthful empty state, never a blank surface, when there are no receipts', () => {
    render(<ReceiptPanel receipts={[]} />)
    expect(screen.queryByRole('listitem')).toBeNull()
    expect(screen.getByText(/no receipts yet/i)).toBeTruthy()
  })
})

describe('acceptance: a receipt is never editable', () => {
  it('renders no button, input, textarea, or contentEditable element anywhere in the timeline', () => {
    render(<ReceiptPanel receipts={RECEIPTS} />)

    expect(screen.queryAllByRole('button')).toHaveLength(0)
    expect(document.querySelector('.receipt-timeline input')).toBeNull()
    expect(document.querySelector('.receipt-timeline textarea')).toBeNull()
    expect(document.querySelector('.receipt-timeline [contenteditable="true"]')).toBeNull()
  })

  it('exposes no onEdit- or onDelete-shaped prop for a caller to wire up a mutation', () => {
    // Structural guarantee: the component signature itself carries no
    // mutation callback, so passing one is simply inert (never called).
    let called = false
    render(
      <ReceiptPanel
        receipts={RECEIPTS}
        onEdit={() => { called = true }}
        onDelete={() => { called = true }}
      />,
    )
    expect(called).toBe(false)
  })
})

describe('acceptance: version pins render exactly', () => {
  it('renders a full-length digest version pin byte-for-byte, with no truncation or ellipsis', () => {
    render(<ReceiptPanel receipts={RECEIPTS} />)
    const fullPin = '4f9c2e7b1a8d3f6015c9e2b7a4d81f3c6e9b2a5d7f10c3e6b9a2d5f8c1e4b7a0'
    const el = screen.getByText(fullPin)
    expect(el.textContent).toBe(fullPin)
    expect(el.textContent).not.toMatch(/…|\.\.\./)
    expect(el.className).toMatch(/receipt-version-pin/)
  })

  it('renders a version pin with meaningful leading zeros and mixed case without altering a character', () => {
    const receipts = [{
      receipt_id: 'rcpt-5',
      kind: 'clone',
      time: '2026-08-05T00:00:00.000Z',
      fields: { version_pin: '00A1-v2.10.0-BETA+007' },
    }]
    render(<ReceiptPanel receipts={receipts} />)
    expect(screen.getByText('00A1-v2.10.0-BETA+007').textContent).toBe('00A1-v2.10.0-BETA+007')
  })
})
