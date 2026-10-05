import { useEffect, useId, useMemo, useRef, useState } from 'react'
import { absoluteWithZone, relativeTime } from '../lib/railTime.js'
import useRelativeNow from '../lib/useRelativeNow.js'
import {
  engineChangeDiscussText,
  getEngineChange,
  listEngineChanges,
  markEngineChangeRead,
  requestEngineChangeHold,
} from '../engineChanges.js'

const STATES = { accepted: 'Accepted', landed: 'Landed', live: 'Live', reverted: 'Reverted', held: 'Held' }
const text = (value) => typeof value === 'string' || typeof value === 'number' ? String(value) : ''
const actor = (value) => text(value) || text(value?.display_name) || text(value?.subject) || 'Unknown'

function Reference({ value }) {
  const label = text(value)
  if (!label) return null
  // Evidence can be an artifact path or an HTTP URL, never an executable URL.
  let safe = false
  try { safe = ['http:', 'https:'].includes(new URL(label, window.location.href).protocol) }
  catch { /* Keep malformed references readable as text. */ }
  return safe
    ? <a href={label} target="_blank" rel="noopener noreferrer">{label}</a>
    : <span>{label}</span>
}

function facts(value) {
  if (text(value)) return text(value)
  if (!value || typeof value !== 'object' || Array.isArray(value)) return ''
  return Object.entries(value).filter(([, v]) => text(v) !== '')
    .map(([key, v]) => `${key.replace(/_/g, ' ')}: ${text(v)}`).join(' · ')
}

export default function EngineChangesTab({
  result,
  loading = false,
  onRetry,
  onCardChange,
  onDiscuss,
  discussDisabledReason = '',
}) {
  const now = useRelativeNow()
  const [selected, setSelected] = useState(null)
  const discussReasonId = useId()
  const selectedRef = useRef(null)
  const [detail, setDetail] = useState(null)
  const [detailLoading, setDetailLoading] = useState(false)
  const [detailError, setDetailError] = useState(false)
  const [notice, setNotice] = useState('')
  const [confirmHold, setConfirmHold] = useState(false)
  const [holding, setHolding] = useState(false)
  const [patches, setPatches] = useState({})
  const [olderCards, setOlderCards] = useState([])
  const [olderCursor, setOlderCursor] = useState(undefined)
  const [olderLoading, setOlderLoading] = useState(false)
  const [olderError, setOlderError] = useState(false)
  const olderLatchRef = useRef(false)
  const patchesRef = useRef({})
  const rowsRef = useRef(new Map())
  const backRef = useRef(null)
  const focusRowRef = useRef(null)
  const requestRef = useRef(0)
  const mountedRef = useRef(true)
  const readLatchRef = useRef(new Set())
  const holdLatchRef = useRef(new Set())
  useEffect(() => {
    mountedRef.current = true
    return () => { mountedRef.current = false; requestRef.current += 1 }
  }, [])
  useEffect(() => {
    if (selected) backRef.current?.focus()
    else if (focusRowRef.current) {
      rowsRef.current.get(focusRowRef.current)?.focus()
      focusRowRef.current = null
    }
  }, [selected])

  const patchCard = (id, patch) => {
    if (!mountedRef.current) return
    patchesRef.current = { ...patchesRef.current, [id]: { ...patchesRef.current[id], ...patch } }
    setPatches(patchesRef.current)
    setDetail((current) => current?.card_id === id ? { ...current, ...patch } : current)
    onCardChange?.(id, patch)
  }
  const cards = useMemo(() => [...new Map([...olderCards, ...(result?.cards || [])]
    .map((card) => [card.card_id, card])).values()]
    .map((card) => ({ ...card, ...patches[card.card_id] }))
    .sort((a, b) => (Date.parse(b.created_at) || 0) - (Date.parse(a.created_at) || 0)), [result, olderCards, patches])
  const nextCursor = olderCursor === undefined ? result?.next_cursor : olderCursor
  const loadOlder = async () => {
    if (!nextCursor || olderLatchRef.current) return
    olderLatchRef.current = true
    setOlderLoading(true)
    setOlderError(false)
    const response = await listEngineChanges({ before: nextCursor })
    olderLatchRef.current = false
    if (!mountedRef.current) return
    setOlderLoading(false)
    if (response.kind === 'ok') {
      setOlderCards((current) => [...new Map([...current, ...response.cards]
        .map((card) => [card.card_id, card])).values()])
      setOlderCursor(response.next_cursor)
    } else setOlderError(true)
  }

  const loadDetail = async (row) => {
    const requestId = ++requestRef.current
    setDetailLoading(true)
    setDetailError(false)
    const response = await getEngineChange(row.card_id)
    if (!mountedRef.current || requestId !== requestRef.current) return
    setDetailLoading(false)
    if (response.kind === 'ok' && response.card?.card_id === row.card_id) {
      setDetail({ ...response.card, ...patchesRef.current[row.card_id] })
    } else setDetailError(true)
  }
  const open = (row) => {
    selectedRef.current = row.card_id
    setSelected(row.card_id)
    setDetail(row)
    setNotice('')
    setConfirmHold(false)
    void loadDetail(row)
    if (row.unread && !readLatchRef.current.has(row.card_id)) {
      readLatchRef.current.add(row.card_id)
      patchCard(row.card_id, { unread: false })
      void markEngineChangeRead(row.card_id).then((response) => {
        readLatchRef.current.delete(row.card_id)
        if (response.kind !== 'ok') {
          patchCard(row.card_id, { unread: true })
          if (mountedRef.current && selectedRef.current === row.card_id) setNotice('Could not mark this change as read.')
        }
        if (mountedRef.current) {
          // Once the receipt settles, the parent's next list is authoritative.
          // Keep hold receipts cached, but do not pin unread forever locally.
          const rest = { ...patchesRef.current[row.card_id] }
          delete rest.unread
          patchesRef.current = { ...patchesRef.current, [row.card_id]: rest }
          setPatches(patchesRef.current)
        }
      })
    }
  }
  const back = () => {
    focusRowRef.current = selected
    selectedRef.current = null
    requestRef.current += 1
    setSelected(null)
    setDetail(null)
    setConfirmHold(false)
    setNotice('')
  }
  const hold = async () => {
    const card = detail
    if (!card || holdLatchRef.current.has(card.card_id) || card.hold_requested_at || card.state === 'held') return
    holdLatchRef.current.add(card.card_id)
    setHolding(true)
    setConfirmHold(false)
    setNotice('')
    const response = await requestEngineChangeHold(card.card_id)
    if (!mountedRef.current) return
    setHolding(false)
    if (response.kind === 'ok' && response.card?.hold_requested_at) {
      patchCard(card.card_id, {
        hold_requested_at: response.card.hold_requested_at,
        hold_requested_by: response.card.hold_requested_by,
        ...(response.card.state ? { state: response.card.state } : {}),
      })
    } else {
      // A lost reply can still have committed. Re-read rather than issuing a
      // second POST; the endpoint's persisted hold is the display authority.
      if (selectedRef.current === card.card_id) setNotice('Could not confirm the hold request. Refresh this card to check its status.')
    }
  }

  if (selected && detail) {
    const held = !!detail.hold_requested_at || detail.state === 'held'
    const holdAttempted = holdLatchRef.current.has(detail.card_id)
    return (
      <div className="engine-changes-body" onKeyDown={(event) => {
        if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); back() }
      }}>
        <button type="button" className="chip-neutral" ref={backRef} onClick={back}>Back</button>
        <div className="engine-changes-heading">
          <h3>{text(detail.title)}</h3>
          <span className="engine-changes-state">{STATES[detail.state] || 'Unknown'}</span>
          <span className="dim">{text(detail.feature_id)}</span>
        </div>
        <div className="engine-changes-actions">
          <button type="button" className="chip-act" disabled={!!discussDisabledReason || detailLoading || detailError}
            aria-describedby={discussDisabledReason ? discussReasonId : undefined}
            onClick={() => onDiscuss?.(engineChangeDiscussText(detail))}>Discuss</button>
          {held ? (
            <span role="status">Hold requested{detail.hold_requested_by && <> by {actor(detail.hold_requested_by)}</>}{detail.hold_requested_at && <> at <time dateTime={text(detail.hold_requested_at)}>{text(detail.hold_requested_at)}</time></>}</span>
          ) : confirmHold ? (
            <div className="engine-changes-confirm" role="group" aria-label="Confirm hold request">
              <span>Request a hold on this engine change?</span>
              <button type="button" className="chip-neutral" onClick={hold}>Confirm request</button>
              <button type="button" className="chip-neutral" onClick={() => setConfirmHold(false)}>Cancel</button>
            </div>
          ) : (
            <button type="button" className="chip-neutral" disabled={holding || holdAttempted || detailLoading || detailError}
              onClick={() => setConfirmHold(true)}>{holding ? 'Requesting hold…' : 'Request hold'}</button>
          )}
          {holdAttempted && !held && !holding && <button type="button" className="chip-neutral" disabled={detailLoading} onClick={() => loadDetail(detail)}>Refresh card</button>}
        </div>
        {discussDisabledReason && <p className="dim" id={discussReasonId}>{discussDisabledReason}</p>}
        <div className="engine-changes-status" role="status">
          {detailLoading ? 'Loading engine change…' : notice}
        </div>
        {detailError ? (
          <p>Engine change details are unavailable. <button type="button" className="chip-neutral" onClick={() => loadDetail(detail)}>Retry</button></p>
        ) : !detailLoading && (
          <>
            <p className="engine-changes-summary">{text(detail.summary) || 'No summary recorded.'}</p>
            <h4>What changed</h4>
            <dl className="engine-changes-facts">
              <dt>Pull request</dt><dd>{detail.change?.pr_url
                ? <Reference value={detail.change.pr_url} /> : 'Not recorded'}{detail.change?.pr_number != null && <> · PR #{text(detail.change.pr_number)}</>}</dd>
              <dt>Head SHA</dt><dd><code>{text(detail.change?.head_sha).slice(0, 8) || 'Not recorded'}</code></dd>
              <dt>Files</dt><dd>{detail.change?.files?.length
                ? <ul>{detail.change.files.map((file, index) => <li key={index}><code>{text(file)}</code></li>)}</ul> : 'Not recorded'}</dd>
              <dt>Diff stat</dt><dd>{facts(detail.change?.diff_stat) || 'Not recorded'}</dd>
            </dl>
            <h4>Evidence</h4>
            <dl className="engine-changes-facts">
              <dt>Regression spec</dt><dd><code>{text(detail.evidence?.regression_spec) || 'Not recorded'}</code></dd>
              {detail.evidence?.before_ref && <><dt>Before</dt><dd><Reference value={detail.evidence.before_ref} /></dd></>}
              {detail.evidence?.after_ref && <><dt>After</dt><dd><Reference value={detail.evidence.after_ref} /></dd></>}
            </dl>
            <h4>Acceptance</h4>
            <dl className="engine-changes-facts">
              <dt>Verdict</dt><dd>{text(detail.acceptance?.verdict) || 'Not recorded'}</dd>
              <dt>Acceptor</dt><dd>{actor(detail.acceptance?.acceptor)}</dd>
              <dt>Accepted at</dt><dd>{text(detail.acceptance?.accepted_at) || 'Not recorded'}</dd>
              {detail.deployment_identity && <><dt>Deployment identity</dt><dd>{facts(detail.deployment_identity) || 'Not recorded'}</dd></>}
            </dl>
          </>
        )}
      </div>
    )
  }

  return (
    <div className="engine-changes-body" aria-busy={loading || olderLoading}>
      <div className="engine-changes-status" role="status">{loading ? 'Loading engine changes…' : ''}</div>
      {result?.kind !== 'ok' && !loading ? (
        <p>Engine changes are unavailable. <button type="button" className="chip-neutral" onClick={onRetry}>Retry</button></p>
      ) : cards.length ? (
        <ul className="engine-changes-list">
          {cards.map((card) => (
            <li key={card.card_id}>
              <button type="button" className="engine-changes-row" onClick={() => open(card)}
                ref={(node) => { if (node) rowsRef.current.set(card.card_id, node); else rowsRef.current.delete(card.card_id) }}>
                <span className="engine-changes-row-title">{text(card.title)}</span>
                <span className="engine-changes-state">{STATES[card.state] || 'Unknown'}</span>
                <span className="dim">{text(card.feature_id)}</span>
                <time className="dim" dateTime={text(card.created_at)} title={absoluteWithZone(Date.parse(card.created_at))}>{relativeTime(Date.parse(card.created_at), now)}</time>
                {card.unread && <span className="engine-changes-unread" role="img" aria-label="Unread" />}
              </button>
            </li>
          ))}
        </ul>
      ) : !loading && <p>No engine changes have been accepted yet.</p>}
      {olderError && <p role="status">Could not load older changes. Try again.</p>}
      {nextCursor && <button type="button" className="chip-neutral" disabled={loading || olderLoading}
        onClick={loadOlder}>{olderLoading ? 'Loading older changes…' : olderError ? 'Retry loading older changes' : 'Load older changes'}</button>}
    </div>
  )
}
