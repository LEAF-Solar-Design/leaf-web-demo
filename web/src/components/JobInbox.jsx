// JobInbox (standardization slice 13a): the right spine's "what you missed"
// card. Reads the ONE notification bus (lib/notifications.js) directly — no
// props threaded from either scene — so mounting it needs nothing beyond the
// SurfaceFrame.Inbox slot (site/SurfaceFrame.jsx) both scenes already call
// beside SurfaceFrame.JobRail.
//
// Row grammar borrows JobRail's own ledger classes (.rail-row, .rail-ledger,
// .rail-note, TM1's fmtWhen — imported, not re-typed) since a notice and a
// job row already read the same way: a dot, a line of text, a time. The one
// thing a notice row adds is retry, and the one thing it can take away is
// itself — both are plain native <button>s, so keyboard (Tab + Enter/Space),
// mouse and touch all work without any bespoke key handling.
import { useState } from 'react'
import './rails.css'

import { fmtWhen } from './JobRail.jsx'
import { useNotices } from '../lib/notifications.js'

const KIND_DOT = { error: 'dot red', warn: 'dot square', success: 'dot' }

// Every disabled Retry carries a REAL reason in prose (the honesty ladder):
// a notice that never recorded an action truthfully has nothing to retry.
const NO_ACTION_REASON = 'This notice recorded no follow-up action, so there is nothing here to retry.'

function InboxRow({ notice, onDismiss }) {
  const when = fmtWhen(notice.time)
  const hasAction = !!(notice.action && typeof notice.action.onClick === 'function')
  return (
    <div className="inbox-row">
      <div className="inbox-row-top">
        <span className={KIND_DOT[notice.kind] || 'dot hollow'} />
        <span className="rail-ev">
          <span className="rail-detail">{notice.text}</span>
        </span>
        {when && <span className="rail-when" title={when.abs}>{when.clock}</span>}
      </div>
      <div className="inbox-row-actions">
        <button
          type="button"
          className="chip-act"
          disabled={!hasAction}
          onClick={hasAction ? () => notice.action.onClick() : undefined}
        >
          {notice.action?.label ? `Retry · ${notice.action.label}` : 'Retry'}
        </button>
        <button type="button" className="chip-act" onClick={() => onDismiss(notice.id)}>
          Dismiss
        </button>
      </div>
      {!hasAction && <p className="lock-note">{NO_ACTION_REASON}</p>}
    </div>
  )
}

export default function JobInbox({ bus } = {}) {
  const notices = useNotices(bus)
  const [expanded, setExpanded] = useState(true)
  // Dismiss only hides a notice from THIS view — the bus is the kept history
  // (lib/notifications.js's ring), never mutated here, so a later mount (or
  // the other scene, over the same singleton bus) still sees the full ring.
  const [dismissedIds, setDismissedIds] = useState(() => new Set())
  const visible = notices.filter((n) => !dismissedIds.has(n.id))
  const dismiss = (id) => setDismissedIds((prev) => new Set(prev).add(id))

  return (
    <aside className="job-inbox">
      <h2>
        Notifications
        <span className="n">{visible.length}</span>
        <button
          type="button"
          className="spine-btn spine-collapse"
          aria-expanded={expanded}
          aria-label={expanded ? 'Collapse the notification inbox' : 'Expand the notification inbox'}
          title={expanded ? 'Collapse' : 'Expand'}
          onClick={() => setExpanded((v) => !v)}
        >
          {expanded ? '»' : '«'}
        </button>
      </h2>
      {expanded && (
        visible.length === 0 ? (
          <div className="rail-note">
            No notices yet. Nothing has been dispatched to this session's notification bus.
          </div>
        ) : (
          <div className="rail-ledger">
            {visible.map((n) => <InboxRow key={n.id} notice={n} onDismiss={dismiss} />)}
          </div>
        )
      )}
    </aside>
  )
}
