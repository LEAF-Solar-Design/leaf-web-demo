/**
 * ShipReceipts. Card D-3 acceptance oracle, one describe block per clause:
 *   Ship receipts appear in the same project timeline as source and
 *   acceptance receipts (the plan's cross-surface projection requirement);
 *   immutable, sanitized fields only.
 */
import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, render, screen, within } from '@testing-library/react'

import ShipReceipts, {
  projectShipReceipt, projectShipReceipts, SHIP_TIMELINE_KIND, TESTFLIGHT_RECEIPT_KIND,
} from './ShipReceipts.jsx'

afterEach(cleanup)

const SOURCE_RECEIPT = {
  receipt_id: 'rcpt-1',
  kind: 'create_project',
  time: '2026-08-01T12:00:00.000Z',
  fields: { name: 'Substation A' },
}

const ACCEPTANCE_RECEIPT = {
  receipt_id: 'rcpt-2',
  kind: 'export',
  time: '2026-08-02T09:30:00.000Z',
  fields: { filename: 'project-export.zip' },
}

const RAW_SHIP_RECEIPT = {
  kind: TESTFLIGHT_RECEIPT_KIND,
  receipt_id: 'rcpt-3',
  execution_id: 'exec-internal-only',
  hash: 'sha256-internal-digest-not-for-display',
  org_id: 'org-internal-only',
  tenant_id: 'tenant-internal-only',
  project_id: 'proj-1',
  created_at: '2026-08-03T00:00:00.000Z',
  revision: 'r1',
  source_revision: '83bbde1',
  source_sha256: 'a'.repeat(64),
  bundle_identifier: 'com.leaf.soundbeam',
  marketing_version: '1.2',
  build_number: '19',
  image_identity: 'ami-0123@sha256:deadbeef',
  toolchain_identity: 'Xcode 16.2 (16C5032a)',
  app_store_connect_result: {
    status: 'testflight_available', build_id: 'asc-19', beta_group: 'Internal Testers',
    uploaded_at: '2026-08-03T00:05:00.000Z',
  },
}

describe('acceptance: ship receipts appear in the same project timeline as source and acceptance receipts', () => {
  it('renders a TestFlight receipt inside the same ReceiptPanel timeline as source/acceptance receipts', () => {
    render(
      <ShipReceipts
        timelineReceipts={[SOURCE_RECEIPT, ACCEPTANCE_RECEIPT]}
        receipts={[RAW_SHIP_RECEIPT]}
      />,
    )

    // One shared timeline surface, not a second ship-only view.
    expect(screen.getAllByRole('list')).toHaveLength(1)
    expect(screen.getByLabelText('Project timeline')).toBeTruthy()

    const rows = screen.getAllByRole('listitem')
    expect(rows).toHaveLength(3)
    expect(within(rows[0]).getByText('create_project')).toBeTruthy()
    expect(within(rows[1]).getByText('export')).toBeTruthy()
    expect(within(rows[2]).getByText(SHIP_TIMELINE_KIND)).toBeTruthy()
    expect(within(rows[2]).getByText('com.leaf.soundbeam')).toBeTruthy()
  })

  it('projects a raw platform receipt into the exact row shape ReceiptPanel renders', () => {
    const row = projectShipReceipt(RAW_SHIP_RECEIPT)
    expect(row).toEqual({
      receipt_id: 'rcpt-3',
      kind: SHIP_TIMELINE_KIND,
      time: '2026-08-03T00:00:00.000Z',
      fields: {
        revision: 'r1',
        source_revision: '83bbde1',
        source_sha256: 'a'.repeat(64),
        bundle_identifier: 'com.leaf.soundbeam',
        marketing_version: '1.2',
        build_number: '19',
        image_identity: 'ami-0123@sha256:deadbeef',
        toolchain_identity: 'Xcode 16.2 (16C5032a)',
        app_store_connect_status: 'testflight_available',
        app_store_connect_build_id: 'asc-19',
        app_store_connect_beta_group: 'Internal Testers',
        app_store_connect_uploaded_at: '2026-08-03T00:05:00.000Z',
      },
    })
  })

  it('falls back to the App Store Connect upload time when created_at is missing', () => {
    const { created_at, ...withoutCreatedAt } = RAW_SHIP_RECEIPT
    const row = projectShipReceipt(withoutCreatedAt)
    expect(row.time).toBe('2026-08-03T00:05:00.000Z')
  })

  it('drops a record that is not a leaf.ios-testflight-receipt.v1, never guessing a shape', () => {
    expect(projectShipReceipt({ kind: 'some.other.receipt', receipt_id: 'x' })).toBeNull()
    expect(projectShipReceipt(null)).toBeNull()
    expect(projectShipReceipts([RAW_SHIP_RECEIPT, { kind: 'other' }, null])).toHaveLength(1)
  })

  it('renders in the exact order given: prior timeline receipts first, ship receipts appended after', () => {
    render(<ShipReceipts timelineReceipts={[ACCEPTANCE_RECEIPT]} receipts={[RAW_SHIP_RECEIPT]} />)
    const rows = screen.getAllByRole('listitem')
    expect(within(rows[0]).getByText('export')).toBeTruthy()
    expect(within(rows[1]).getByText(SHIP_TIMELINE_KIND)).toBeTruthy()
  })

  it('shows the shared empty state, never a blank surface, when there are no receipts of either kind', () => {
    render(<ShipReceipts timelineReceipts={[]} receipts={[]} />)
    expect(screen.queryByRole('listitem')).toBeNull()
    expect(screen.getByText(/no receipts yet/i)).toBeTruthy()
  })
})

describe('acceptance: immutable, sanitized fields only', () => {
  it('renders no button, input, textarea, or contentEditable element for a ship receipt row', () => {
    render(<ShipReceipts receipts={[RAW_SHIP_RECEIPT]} />)
    expect(screen.queryAllByRole('button')).toHaveLength(0)
    expect(document.querySelector('.receipt-timeline input')).toBeNull()
    expect(document.querySelector('.receipt-timeline textarea')).toBeNull()
    expect(document.querySelector('.receipt-timeline [contenteditable="true"]')).toBeNull()
  })

  it('never forwards internal ids the raw receipt carries (execution_id, hash, org_id, tenant_id)', () => {
    render(<ShipReceipts receipts={[RAW_SHIP_RECEIPT]} />)
    expect(screen.queryByText('exec-internal-only')).toBeNull()
    expect(screen.queryByText('sha256-internal-digest-not-for-display')).toBeNull()
    expect(screen.queryByText('org-internal-only')).toBeNull()
    expect(screen.queryByText('tenant-internal-only')).toBeNull()
  })

  it('redacts a credential-shaped value even if it reached an allow-listed field un-sanitized', () => {
    const dirty = {
      ...RAW_SHIP_RECEIPT,
      receipt_id: 'rcpt-4',
      toolchain_identity: 'Bearer abc.def.ghi',
    }
    render(<ShipReceipts receipts={[dirty]} />)
    expect(screen.queryByText(/Bearer abc\.def\.ghi/)).toBeNull()
    expect(screen.getByText('[redacted]')).toBeTruthy()
  })

  it('projectShipReceipts strips internal ids from every row it produces', () => {
    const rows = projectShipReceipts([RAW_SHIP_RECEIPT])
    expect(rows).toHaveLength(1)
    const fieldKeys = Object.keys(rows[0].fields)
    expect(fieldKeys).not.toContain('execution_id')
    expect(fieldKeys).not.toContain('hash')
    expect(fieldKeys).not.toContain('org_id')
    expect(fieldKeys).not.toContain('tenant_id')
    expect(fieldKeys).not.toContain('receipt_id')
  })
})
