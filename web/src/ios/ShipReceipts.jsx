/**
 * ShipReceipts — projects a leaf.ios-testflight-receipt.v1 record (platform
 * `ios_ship.py` / `/api/ios-ship/receipts/{id}`) into the same timeline row
 * shape ReceiptPanel already renders (card B-U6: receipt_id, kind, time,
 * fields), then renders through that SAME component. A TestFlight ship
 * receipt therefore reads in the identical project timeline as every
 * source and acceptance receipt — never a second, ship-only view.
 *
 * The allow-list below is the sanitized projection: only the fields a ship
 * receipt is meant to surface (revision identity, toolchain identity, and
 * the App Store Connect outcome) reach the timeline row. Internal ids the
 * raw receipt also carries (org_id, tenant_id, execution_id, hash) are
 * never forwarded. ReceiptPanel's own credential redaction still runs
 * underneath this as defense in depth.
 */
import ReceiptPanel from '../projects/ReceiptPanel.jsx'

export const TESTFLIGHT_RECEIPT_KIND = 'leaf.ios-testflight-receipt.v1'
export const SHIP_TIMELINE_KIND = 'ios_testflight_ship'

const RECEIPT_FIELD_ALLOWLIST = [
  'revision', 'source_revision', 'source_sha256', 'bundle_identifier',
  'marketing_version', 'build_number', 'image_identity', 'toolchain_identity',
]

const ASC_RESULT_ALLOWLIST = ['status', 'build_id', 'beta_group', 'uploaded_at']

function sanitizedFields(receipt) {
  const fields = {}
  for (const key of RECEIPT_FIELD_ALLOWLIST) {
    if (typeof receipt[key] === 'string' && receipt[key]) fields[key] = receipt[key]
  }
  const asc = receipt.app_store_connect_result
  if (asc && typeof asc === 'object') {
    for (const key of ASC_RESULT_ALLOWLIST) {
      if (typeof asc[key] === 'string' && asc[key]) fields[`app_store_connect_${key}`] = asc[key]
    }
  }
  return fields
}

// Pure projection: a raw platform receipt in, a ReceiptPanel row out (or
// null when the record isn't a recognizable TestFlight receipt).
export function projectShipReceipt(receipt) {
  if (!receipt || typeof receipt !== 'object' || receipt.kind !== TESTFLIGHT_RECEIPT_KIND) {
    return null
  }
  if (typeof receipt.receipt_id !== 'string' || !receipt.receipt_id) return null
  return {
    receipt_id: receipt.receipt_id,
    kind: SHIP_TIMELINE_KIND,
    time: typeof receipt.created_at === 'string'
      ? receipt.created_at
      : receipt.app_store_connect_result?.uploaded_at ?? null,
    fields: sanitizedFields(receipt),
  }
}

export function projectShipReceipts(receipts) {
  return (Array.isArray(receipts) ? receipts : [])
    .map(projectShipReceipt)
    .filter(Boolean)
}

export default function ShipReceipts({
  receipts, timelineReceipts = [], title = 'Timeline', emptyLabel = 'No receipts yet.',
}) {
  const merged = [...(timelineReceipts || []), ...projectShipReceipts(receipts)]
  return <ReceiptPanel receipts={merged} title={title} emptyLabel={emptyLabel} />
}
