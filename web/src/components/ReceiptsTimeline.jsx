/**
 * ReceiptsTimeline — the delivery receipts that EXIST, newest first.
 *
 * This renders rows another system already minted: a prewarm-relay receipt, a
 * gate proof, a supply-set manifest, a reconciler entry, a per-job receipt. It
 * invents nothing. There is no "pending" row, no "expected" row, no row for a
 * run that has not published its artifact yet. Absence is shown as absence,
 * and a source that could not be read is shown as that source being
 * unreadable, with the reason the server gave.
 *
 * The two honest states are the load-bearing part of this component:
 *
 *   EMPTY       the sources answered and no receipt exists for this scope yet.
 *               The sentence says exactly that, and says nothing about when one
 *               will appear, because nothing here knows.
 *   UNAVAILABLE at least one source could not be read at all (no platform
 *               credential configured, the API did not answer, a receipt did
 *               not parse). The sentence names the source and the reason, so a
 *               reader can tell "nothing happened" apart from "we cannot see".
 *
 * Those two are DIFFERENT and must never collapse into each other: an empty
 * timeline over an unreadable source would claim a build did not run when the
 * truth is that nobody looked.
 *
 * NOT MOUNTED YET, deliberately. Slice 11 mounts it beside the build card.
 * This slice exports it and covers it with a jsdom test over fixture rows, so
 * the mount is a placement decision and not also a behaviour decision.
 *
 * Every field is treated as untrusted text: rows originate in workflow
 * artifacts and a receipt file, so nothing here is rendered as markup and the
 * only link rendered is an http(s) URL that passed `safeHref`.
 *
 * What a row MEANS is decided server-side, not here. A "Gate proof" row only
 * reaches this component after `receipts_read` has checked that the artifact is
 * un-expired, was minted by a run of this repository (not a fork's), and came
 * from an allowlisted minting workflow; the row's own summary names that
 * workflow. This component never upgrades a row's status, and there is no
 * "unverified" state to render, because an unverified artifact never becomes a
 * row at all.
 */

const KIND_LABELS = {
  'prewarm-relay': 'Prewarm relay',
  'gate-proof': 'Gate proof',
  'supply-set': 'Supply set',
  reconciler: 'Reconciler',
  job: 'Job',
}

const REASON_SENTENCES = {
  source_unavailable: 'is not configured on this deployment, so it was not read',
  source_unreachable: 'did not answer, so its receipts are not shown',
  receipt_unreadable: 'answered with something this reader could not parse',
  // Deliberately cause-neutral: the server uses this ONE reason for three
  // distinct inconclusive states (too many reads already in flight, a
  // per-name provenance budget exhausted, a run record that could not be
  // read), and the server's own `detail` sentence -- appended below -- always
  // names which one. A specific clause here would contradict `detail` on the
  // cases it was not written for.
  source_busy: 'was not fully read this time',
}

/** A short commit for display. Never pads, never invents, never truncates a
 *  value that is already shorter than eight characters into looking like one. */
export function sha8(value) {
  if (typeof value !== 'string') return ''
  const trimmed = value.trim()
  if (!/^[0-9a-fA-F]{7,64}$/.test(trimmed)) return ''
  return trimmed.slice(0, 8).toLowerCase()
}

/** Only an absolute http(s) URL becomes a link. Everything else renders as
 *  plain text, so a `javascript:` or `data:` value in an artifact field cannot
 *  become a clickable target. */
export function safeHref(value) {
  if (typeof value !== 'string' || !value) return null
  try {
    const url = new URL(value)
    return url.protocol === 'https:' || url.protocol === 'http:' ? url.href : null
  } catch {
    return null
  }
}

function kindLabel(kind) {
  return KIND_LABELS[kind] || (typeof kind === 'string' && kind ? kind : 'Receipt')
}

function unavailableSentence(entry) {
  const source = entry && typeof entry.source === 'string' ? entry.source : 'a receipt source'
  const tail = REASON_SENTENCES[entry && entry.reason] || 'could not be read'
  const detail = entry && typeof entry.detail === 'string' && entry.detail ? ` ${entry.detail}.` : ''
  return `${source} ${tail}.${detail}`
}

function ReceiptRow({ row }) {
  const href = safeHref(row.url)
  const short = sha8(row.sha)
  return (
    <li className="receipt-row" data-kind={row.kind || 'receipt'}>
      <span className="receipt-kind" data-testid="receipt-kind">{kindLabel(row.kind)}</span>
      <span className="receipt-summary">{row.summary || 'No summary was recorded.'}</span>
      <span className="receipt-meta">
        {short ? <code className="receipt-sha" data-testid="receipt-sha">{short}</code> : null}
        {row.at ? <time className="receipt-at" dateTime={row.at}>{row.at}</time> : null}
        {href ? (
          <a className="receipt-link" href={href} target="_blank" rel="noreferrer noopener">
            Open
          </a>
        ) : null}
      </span>
    </li>
  )
}

/**
 * @param {object}   props
 * @param {Array}    props.rows        receipt rows from GET /api/receipts
 * @param {Array}    props.unavailable sources that could not be read
 * @param {string}   props.scope       the scope these rows answer for
 * @param {boolean}  props.loading     a read is in flight
 */
export default function ReceiptsTimeline({ rows = [], unavailable = [], scope = '', loading = false }) {
  const safeRows = Array.isArray(rows) ? rows.filter((row) => row && typeof row === 'object') : []
  const blocked = Array.isArray(unavailable) ? unavailable.filter(Boolean) : []
  // Newest first. The server already sorts; sorting again keeps the component
  // correct when it is handed fixture rows or a merged set.
  const ordered = safeRows
    .slice()
    .sort((a, b) => String(b.at || '').localeCompare(String(a.at || '')))

  return (
    <section className="receipts-timeline" aria-label="Delivery receipts" data-scope={scope}>
      <h3 className="receipts-heading">Receipts</h3>

      {loading ? (
        <p className="receipts-note" data-testid="receipts-loading">Reading the receipts that exist…</p>
      ) : null}

      {!loading && ordered.length === 0 && blocked.length === 0 ? (
        <p className="receipts-note" data-testid="receipts-empty">
          No receipt exists for this scope yet. Nothing here predicts when one will.
        </p>
      ) : null}

      {ordered.length > 0 ? (
        <ol className="receipts-rows" data-testid="receipts-rows">
          {ordered.map((row, index) => (
            <ReceiptRow key={`${row.kind || 'receipt'}:${row.ref || index}:${row.sha || index}`} row={row} />
          ))}
        </ol>
      ) : null}

      {blocked.length > 0 ? (
        <ul className="receipts-unavailable" data-testid="receipts-unavailable">
          {blocked.map((entry, index) => (
            <li key={`${entry.source || 'source'}:${index}`} className="receipts-note">
              {unavailableSentence(entry)}
            </li>
          ))}
        </ul>
      ) : null}
    </section>
  )
}
