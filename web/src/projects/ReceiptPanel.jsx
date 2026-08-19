/**
 * ReceiptPanel — the project timeline: an append-only list of lifecycle
 * receipts (platform/project_lifecycle.py `project_lifecycle_receipts`).
 *
 * Card B-U6 acceptance:
 *   Project timeline lists immutable receipts (kind, time, sanitized
 *   fields); a receipt is never editable; version pins render exactly.
 *
 * Purely props-driven (ExportDialog.jsx / Membership.jsx pattern): this
 * component owns no fetch. `receipts` is the server's answer, rendered in
 * the order given — never re-sorted, filtered, or mutated client-side.
 *
 * Immutability is structural, not a UI convention: there is no onEdit /
 * onDelete prop anywhere on this component, so no receipt row can ever grow
 * a mutation affordance no matter what a caller passes.
 *
 * "Sanitized fields" is defense in depth, not trust-the-server-blindly: the
 * server already refuses to persist a credential-shaped receipt
 * (_assert_sanitized_receipt in project_lifecycle.py), but this panel
 * mirrors that same deny-list client-side and redacts on sight rather than
 * assume every caller re-validates before passing `receipts` down.
 *
 * A version pin (a digest, a semver+build string, any exact identifier a
 * receipt carries) is rendered byte-for-byte: no truncation, no ellipsis,
 * no locale reformatting. Getting a version pin one character wrong is
 * worse than not showing it at all.
 */

// Mirrors platform/project_lifecycle.py _RECEIPT_SECRET_KEYS — a receipt
// field whose key looks credential-shaped is redacted here too, even if it
// somehow reached this component un-sanitized.
const SECRET_KEYS = new Set([
  'authorization', 'password', 'secret', 'token', 'api_key', 'apikey',
  'access_token', 'refresh_token', 'oauth_token', 'client_secret', 'private_key',
  'cookie', 'set_cookie',
])

const JWT_SHAPE = /^eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}$/

function normalizedKey(key) {
  return String(key).trim().toLowerCase().replace(/-/g, '_')
}

function looksLikeCredential(value) {
  if (typeof value !== 'string') return false
  const stripped = value.trim()
  return stripped.toLowerCase().startsWith('bearer ') || JWT_SHAPE.test(stripped)
}

// Renders a field value exactly as given: primitives as their literal
// string form (no rounding, no locale formatting), objects/arrays as exact
// JSON — never a summarized or truncated stand-in.
function renderFieldValue(value) {
  if (value == null) return String(value)
  if (typeof value === 'string') return value
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  return JSON.stringify(value)
}

function formatTime(time) {
  const d = new Date(time)
  if (Number.isNaN(d.getTime())) return String(time)
  return d.toLocaleString()
}

function ReceiptField({ fieldKey, value }) {
  const redacted = SECRET_KEYS.has(normalizedKey(fieldKey)) || looksLikeCredential(value)
  const isVersionPin = normalizedKey(fieldKey).includes('version_pin')
  return (
    <div className="receipt-field">
      <dt className="receipt-field-key">{fieldKey}</dt>
      <dd
        className={isVersionPin ? 'receipt-field-value receipt-version-pin' : 'receipt-field-value'}
        data-redacted={redacted || undefined}
      >
        {redacted ? '[redacted]' : renderFieldValue(value)}
      </dd>
    </div>
  )
}

export default function ReceiptPanel({ receipts, title = 'Timeline', emptyLabel = 'No receipts yet.' }) {
  const roster = receipts || []

  return (
    <section className="receipt-panel" aria-label="Project timeline">
      <div className="receipt-panel-head">
        <span className="receipt-panel-title">{title}</span>
      </div>

      {roster.length === 0 ? (
        <p className="receipt-panel-empty">{emptyLabel}</p>
      ) : (
        <ol className="receipt-timeline">
          {roster.map((receipt) => {
            const id = receipt.receipt_id || receipt.id
            const kind = receipt.kind || receipt.action
            const fields = receipt.fields || {}
            return (
              <li key={id} className="receipt-row" data-receipt-id={id}>
                <div className="receipt-row-head">
                  <span className="receipt-kind">{kind}</span>
                  <time className="receipt-time" dateTime={receipt.time}>
                    {formatTime(receipt.time)}
                  </time>
                </div>
                {Object.keys(fields).length > 0 && (
                  <dl className="receipt-fields">
                    {Object.entries(fields).map(([fieldKey, value]) => (
                      <ReceiptField key={fieldKey} fieldKey={fieldKey} value={value} />
                    ))}
                  </dl>
                )}
              </li>
            )
          })}
        </ol>
      )}
    </section>
  )
}
