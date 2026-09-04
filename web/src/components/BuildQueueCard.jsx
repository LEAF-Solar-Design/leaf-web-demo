// THE BUILD QUEUE CARD (standardization slice 11a). Renders ONE build record
// (web/src/lib/buildQueue.js) as a ledger row, whatever lane ran it. JobRail
// hosts it on the right spine: for a broker job the row is byte-for-byte the
// row JobRail rendered before this slice (same dot grammar, same status
// words, same cost text, same clock column, same `current` mark), plus the
// two-stage terminal marks on terminal rows and a meta line only when the
// record actually carries a requester, receipts or an action the host wired.
//
// The card never decides a state. It reads `record.state`, `record.status`
// and `record.terminal` as facts the mapper established, and it renders an
// action button ONLY for a verb the record declares AND the host handed a
// handler for: a declared verb with no handler renders nothing, so a host
// that has no cancel route cannot show a cancel button that does nothing.
import { formatCostUsd, isTerminalBuild } from '../lib/buildQueue.js'
import { fmtWhen } from '../lib/railTime.js'

/** Dot grammar (contract .dot classes): pulse only on live work, hollow =
 *  not in play, square = advisory/degraded, red = failed. JobRail's dotFor,
 *  spelled on the record's coarse state and tint. */
export function dotClassFor(record) {
  switch (record.state) {
    case 'running': return 'dot live pulse'
    case 'queued': return 'dot hollow'
    case 'verifying': return 'dot live'
    case 'failed': return record.status.tint === 'warn' ? 'dot square' : 'dot red'
    case 'done': return record.status.tint === 'warn' ? 'dot square' : 'dot'
    default: return 'dot hollow'
  }
}

export function elapsedText(ms) {
  return ms != null ? `${(ms / 1000).toFixed(1)}s` : null
}

const LANE_WORD = Object.freeze({ broker: 'tool run', fold: 'autonomous run', fleet: 'fleet task' })
const ACTION_WORD = Object.freeze({ cancel: 'Cancel', retry: 'Retry', promote: 'Promote' })

function Stage({ on, name, yes, no, glyph }) {
  return (
    <span
      className={`bq-mark ${name}${on ? ' on' : ''}`}
      role="img"
      aria-label={on ? yes : no}
      title={on ? yes : no}
    >
      {glyph}
    </span>
  )
}

export default function BuildQueueCard({ record, current = false, onSelect, actions = null }) {
  if (!record) return null
  const terminal = isTerminalBuild(record)
  const when = fmtWhen(record.started)
  const elapsed = elapsedText(record.elapsed_ms)

  // Quiet muted detail after the tinted status word (one line, ellipsized).
  let detail = record.status.detail
  if (current) detail = detail ? `this session · ${detail}` : 'this session'

  // Right column: running = elapsed (indeterminate work is pulse dot + verb +
  // elapsed, never a fake progress bar); terminal = clock-only inside the day
  // group, absolute revealed on hover (TM1).
  const tail = record.state === 'running' ? elapsed : ((when && when.clock) || elapsed)

  const wired = actions
    ? record.actions.filter((verb) => typeof actions[verb] === 'function')
    : []
  // A broker row already carries its cost in the detail (JobRail's "$0.0123"
  // after "complete"); the other lanes report spend on the meta line.
  const cost = record.lane !== 'broker' && record.cost_usd != null ? formatCostUsd(record.cost_usd) : null
  const hasMeta = !!record.requested_by || record.receipts.length > 0 || !!record.estimate_ms || !!cost
  const rowClass = ['rail-row', 'build-queue-card', current ? 'current' : ''].filter(Boolean).join(' ')

  const inner = (
    <>
      <span className={dotClassFor(record)} />
      <span className="rail-ev">
        <b className="rail-tool">{record.title}</b>
        <span className={`rail-word ${record.status.tint}`}>{record.status.word}</span>
        {detail && <span className="rail-detail">· {detail}</span>}
      </span>
      {tail && <span className="rail-when" title={when ? when.abs : undefined}>{tail}</span>}
      {terminal && (
        <span className="bq-stages" aria-label="Terminal stages">
          <Stage
            on={record.terminal.verified}
            name="verified"
            glyph="✓"
            yes="Verified: this lane's own terminal artifact exists"
            no="Not verified: no terminal artifact for this build"
          />
          <Stage
            on={record.terminal.promoted}
            name="promoted"
            glyph="↑"
            yes="Promoted: a promotion receipt exists"
            no="Not promoted: no promotion receipt"
          />
        </span>
      )}
    </>
  )

  const row = terminal && onSelect
    ? <button type="button" className={rowClass} onClick={() => onSelect(record)}>{inner}</button>
    : <div className={rowClass}>{inner}</div>

  return (
    <div
      className="bq-card"
      data-testid="build-queue-card"
      data-lane={record.lane}
      data-state={record.state}
      data-verified={record.terminal.verified ? '1' : '0'}
      data-promoted={record.terminal.promoted ? '1' : '0'}
    >
      {row}
      {hasMeta && (
        <div className="bq-meta">
          <span className="bq-lane">{LANE_WORD[record.lane]}</span>
          {record.requested_by && (
            <span className="bq-requested" title="Who asked for this build">
              requested by <b>{record.requested_by}</b>
            </span>
          )}
          {record.estimate_ms != null && <span className="bq-estimate">est. {elapsedText(record.estimate_ms)}</span>}
          {cost && <span className="rail-cost">{cost}</span>}
          {record.receipts.length > 0 && (
            <span className="bq-receipts" title={record.receipts.map((r) => `${r.kind}: ${r.ref}`).join('\n')}>
              {record.receipts.length === 1 ? '1 receipt' : `${record.receipts.length} receipts`}
            </span>
          )}
        </div>
      )}
      {wired.length > 0 && (
        <div className="bq-actions">
          {wired.map((verb) => (
            <button
              key={verb}
              type="button"
              className="chip-act"
              data-action={verb}
              onClick={() => actions[verb](record)}
            >
              {ACTION_WORD[verb]}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
