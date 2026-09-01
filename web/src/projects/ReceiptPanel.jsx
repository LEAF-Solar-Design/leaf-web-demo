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
 *
 * `isRedactedField`, `renderFieldValue`, and `deepRedact` are exported so
 * other panels that render arbitrary server JSON (operator/WorkerJobsPanel,
 * operator/SessionPanel, operator/RunbooksPanel) apply this exact same
 * credential denylist instead of forking a second copy that can drift out
 * of sync with `_RECEIPT_SECRET_KEYS`.
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

// The one predicate every panel rendering server JSON must run a field
// through before it reaches the DOM: true when the key itself is
// credential-shaped (mirrors _RECEIPT_SECRET_KEYS) OR the value's own shape
// looks like a bearer token / JWT, independent of what its key was called.
export function isRedactedField(fieldKey, value) {
  return SECRET_KEYS.has(normalizedKey(fieldKey)) || looksLikeCredential(value)
}

// Renders a field value exactly as given: primitives as their literal
// string form (no rounding, no locale formatting), objects/arrays as exact
// JSON — never a summarized or truncated stand-in.
export function renderFieldValue(value) {
  if (value == null) return String(value)
  if (typeof value === 'string') return value
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  return JSON.stringify(value)
}

// Recursively redacts credential-shaped keys/values at ANY depth, not just
// the top level `isRedactedField` alone would catch. Every raw-JSON /
// "show everything" fallback a panel offers MUST pass its data through this
// first — a flat one-level redaction pass is not enough once a caller falls
// back to dumping a whole nested object, and that fallback is exactly where
// a token buried two levels deep would otherwise leak in plain text.
export function deepRedact(value, key = null) {
  if (isRedactedField(key, value)) return '[redacted]'
  if (Array.isArray(value)) return value.map((v) => deepRedact(v, key))
  if (value && typeof value === 'object') {
    return Object.fromEntries(Object.entries(value).map(([k, v]) => [k, deepRedact(v, k)]))
  }
  return value
}

function formatTime(time) {
  const d = new Date(time)
  if (Number.isNaN(d.getTime())) return String(time)
  return d.toLocaleString()
}

function ReceiptField({ fieldKey, value }) {
  const redacted = isRedactedField(fieldKey, value)
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
