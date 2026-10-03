import React, { useEffect, useMemo, useRef, useState } from 'react'
import {
  SOLAR_STRING_COMPOSER_REASONS, STRING_ADD_TOOL, STRING_MULTI_ADD_TOOL,
  MAX_SINGLE_REFS, MAX_MULTI_REFS, MAX_STRING_LENGTH, stringComposerView,
  composeStringRequest, checkStringFreshness, stringRunRefusal,
} from './solarStringComposerModel.js'

const slotText = /^[1-9][0-9]{0,8}$/
const lengthText = /^[0-9]{1,4}$/
const PAGE_SIZE = 25
const SKIPPED = Symbol('skipped')

function Pages({ page, count, onChange, disabled }) {
  const last = Math.max(0, Math.ceil(count / PAGE_SIZE) - 1)
  return <div>
    <button className="chip-act" disabled={disabled || page === 0}
      onClick={() => onChange(page - 1)}>Previous page</button>
    <button className="chip-act" disabled={disabled || page >= last}
      onClick={() => onChange(page + 1)}>Next page</button>
  </div>
}

export default function SolarStringComposer({
  row, drawingId, drawingVersion, projectId = null, readIntake, status = null,
  failureCode = null, onSubmit, onClose,
}) {
  const scope = stringComposerView({ envelope: null, drawingId, drawingVersion, projectId })
  const supported = row.name === STRING_ADD_TOOL || row.name === STRING_MULTI_ADD_TOOL
  const binding = useMemo(() => ({ drawingId, drawingVersion, projectId, toolName: row.name }),
    [drawingId, drawingVersion, projectId, row.name])
  const current = useRef(binding)
  current.current = binding
  const reader = useRef(readIntake)
  reader.current = readIntake
  const mounted = useRef(false)
  const generation = useRef(0)
  const lock = useRef(null)
  const previousStatus = useRef(status)
  const [loaded, setLoaded] = useState(null)
  const [reload, setReload] = useState(0)
  const [queue, setQueue] = useState([])
  const [selected, setSelected] = useState(null)
  const [first, setFirst] = useState(null)
  const [last, setLast] = useState(null)
  const [lengthDraft, setLengthDraft] = useState(null)
  const [search, setSearch] = useState('')
  const [sourcePage, setSourcePage] = useState(0)
  const [queuePage, setQueuePage] = useState(0)
  const [previewPage, setPreviewPage] = useState(0)
  const [checking, setChecking] = useState(false)
  const [checkReason, setCheckReason] = useState(null)

  useEffect(() => {
    mounted.current = true
    return () => { mounted.current = false; generation.current += 1; lock.current = null }
  }, [])

  useEffect(() => {
    setQueue([]); setSelected(null); setFirst(null); setLast(null); setLengthDraft(null)
    setSearch(''); setSourcePage(0); setQueuePage(0); setPreviewPage(0)
    setCheckReason(null); setChecking(false); lock.current = null
  }, [binding])

  useEffect(() => {
    const number = ++generation.current
    if (!supported || scope.code === 'scope') return undefined
    setLoaded(null)
    const active = () => mounted.current && current.current === binding && generation.current === number
    // The reader runs only while this load is still the current one: a scope change, an unmount or a
    // second StrictMode setup before the microtask runs starts no read for the obsolete binding.
    Promise.resolve().then(() => (active() ? reader.current(drawingId, drawingVersion) : SKIPPED)).then((envelope) => {
      if (envelope === SKIPPED || !active()) return
      const view = stringComposerView({ envelope, drawingId, drawingVersion, projectId })
      setLoaded({ binding, view })
    }, () => {
      if (active()) setLoaded({ binding, view: { ok: false,
        reason: SOLAR_STRING_COMPOSER_REASONS.unavailable } })
    })
    return () => { generation.current += 1 }
  }, [binding, reload, supported, scope.code, drawingId, drawingVersion, projectId])

  useEffect(() => {
    if (status === 'finished' && previousStatus.current !== 'finished') {
      setQueue([]); setQueuePage(0); setPreviewPage(0); setCheckReason(null)
      setReload((value) => value + 1)
    }
    previousStatus.current = status
  }, [status])

  const view = loaded?.binding === binding ? loaded.view : null
  const multi = row.name === STRING_MULTI_ADD_TOOL
  const text = lengthDraft ?? (view?.savedLimit >= 1 && view.savedLimit <= MAX_STRING_LENGTH
    ? String(view.savedLimit) : '')
  const stringLength = lengthText.test(text) ? Number(text) : text
  const preview = useMemo(() => composeStringRequest({ view, toolName: row.name, queue, stringLength }),
    [view, row.name, queue, stringLength])
  const sources = useMemo(() => {
    if (!view?.ok) return []
    const needle = search.toLowerCase()
    return view.design === 'Ground'
      ? view.rows.filter((item) => item.name.toLowerCase().includes(needle))
      : view.panels.filter((item) => item.id.toLowerCase().includes(needle) ||
        item.frameName.toLowerCase().includes(needle))
  }, [view, search])
  const rowNames = useMemo(() => new Map((view?.rows ?? []).map((item) => [item.frameIndex, item.name])), [view])
  const disabled = checking || status === 'pending'
  const bound = multi ? MAX_MULTI_REFS : MAX_SINGLE_REFS
  const addDisabled = disabled || queue.length >= bound
  const queueLast = Math.max(0, Math.ceil(queue.length / PAGE_SIZE) - 1)
  const shownQueuePage = Math.min(queuePage, queueLast)
  const sizes = preview.ok ? preview.preview.sizes : []
  const shownPreviewPage = Math.min(previewPage, Math.max(0, Math.ceil(sizes.length / PAGE_SIZE) - 1))
  const append = (entry) => {
    if (disabled || lock.current) return
    setQueue((items) => items.length < bound ? [...items, entry] : items)
    setCheckReason(null)
  }
  const move = (index, offset) => {
    setQueue((items) => {
      const copy = items.slice()
      ;[copy[index], copy[index + offset]] = [copy[index + offset], copy[index]]
      return copy
    })
    setCheckReason(null)
  }
  const review = async () => {
    if (lock.current || disabled || !preview.ok) return
    const token = { binding }
    lock.current = token
    const capturedParams = preview.params
    const capturedView = view
    const capturedRow = row
    setChecking(true); setCheckReason(null)
    const active = () => mounted.current && current.current === binding && lock.current === token
    try {
      let fresh
      try {
        // The head read starts only while this review still holds the lock for the current binding.
        const envelope = await Promise.resolve().then(() => (active() ? reader.current(drawingId, 'head') : SKIPPED))
        if (envelope === SKIPPED || !active()) return
        fresh = checkStringFreshness({ view: capturedView, drawingId, drawingVersion, envelope })
      } catch {
        fresh = { ok: false, reason: SOLAR_STRING_COMPOSER_REASONS.unavailable }
      }
      if (!active()) return
      if (!fresh.ok) setCheckReason(fresh.reason)
      else onSubmit?.(capturedRow, capturedParams)
    } finally {
      if (active()) { lock.current = null; setChecking(false) }
    }
  }
  const cancel = <button className="chip-act" onClick={() => onClose?.()}>Cancel</button>
  if (!supported) return <div><p className="solar-step-note" role="status">
    {SOLAR_STRING_COMPOSER_REASONS.request}</p>{cancel}</div>
  if (scope.code === 'scope') return <div><p className="solar-step-note" role="status">
    {scope.reason}</p>{cancel}</div>
  const runRefusal = status === 'failed' ? stringRunRefusal(failureCode) : null
  return <div>
    {checking && <p className="solar-step-note" role="status">{SOLAR_STRING_COMPOSER_REASONS.checking}</p>}
    {status === 'pending' && <p className="solar-step-note" role="status">{SOLAR_STRING_COMPOSER_REASONS.pending}</p>}
    {runRefusal && <p className="solar-step-note" role="alert">{runRefusal.code === 'failed'
      ? SOLAR_STRING_COMPOSER_REASONS.failed : runRefusal.reason}</p>}
    {status === 'finished' && <p className="solar-step-note" role="status">{SOLAR_STRING_COMPOSER_REASONS.finished}</p>}
    {checkReason && <p className="solar-step-note" role="alert">{checkReason}</p>}
    {!view ? <p className="solar-step-note" role="status">{SOLAR_STRING_COMPOSER_REASONS.loading}</p>
      : !view.ok ? <>
        <p className="solar-step-note" role="status">{view.reason}</p>
        <button className="chip-act" disabled={disabled} onClick={() => setReload((value) => value + 1)}>Reload drawing</button>
      </> : <>
        <section aria-label={view.design === 'Ground' ? 'Rows' : 'Panels'}>
          <label>{view.design === 'Ground' ? 'Search rows' : 'Search panels'}
            <input type="text" value={search} disabled={disabled} onChange={(event) => {
              setSearch(event.target.value); setSourcePage(0)
            }} />
          </label>
          <ul>{sources.slice(sourcePage * PAGE_SIZE, (sourcePage + 1) * PAGE_SIZE).map((item) =>
            <li key={view.design === 'Ground' ? item.frameIndex : item.id}>
              {view.design === 'Ground' ? <button className="chip-act" disabled={disabled}
                aria-label={`Select row ${item.frameIndex + 1} ${item.name}`} onClick={() => setSelected(item)}>
                {item.name} <span>{item.slotCount} slots</span></button>
                : <button className="chip-act" disabled={addDisabled} aria-label={`Add panel ${item.id}`}
                  onClick={() => append({ kind: 'panel', panelId: item.id })}>{item.id} {item.frameName}</button>}
            </li>)}</ul>
          <Pages page={sourcePage} count={sources.length} onChange={setSourcePage} disabled={disabled} />
        </section>
        {view.design === 'Ground' && selected && <div>
          <label>First slot<input type="text" value={first ?? ''} disabled={disabled}
            aria-invalid={first !== null && !slotText.test(first)} onChange={(event) => setFirst(event.target.value)} /></label>
          <label>Last slot<input type="text" value={last ?? ''} disabled={disabled}
            aria-invalid={last !== null && !slotText.test(last)} onChange={(event) => setLast(event.target.value)} /></label>
          <button className="chip-act" disabled={addDisabled || !slotText.test(first ?? '') || !slotText.test(last ?? '')}
            onClick={() => append({ kind: 'range', frameIndex: selected.frameIndex, frameId: selected.frameId,
              fromSlot: Number(first), toSlot: Number(last) })}>Add range</button>
        </div>}
        {multi && <label>String length<input type="text" value={text} disabled={disabled}
          aria-invalid={lengthDraft !== null && !lengthText.test(text)} onChange={(event) => {
            setLengthDraft(event.target.value); setCheckReason(null)
          }} /></label>}
        <section aria-label="Selection queue">
          <h3>Selection queue</h3>
          <p>{queue.length} selections</p>
          <ol start={shownQueuePage * PAGE_SIZE + 1}>{queue.slice(shownQueuePage * PAGE_SIZE,
            (shownQueuePage + 1) * PAGE_SIZE).map((entry, offset) => {
            const index = shownQueuePage * PAGE_SIZE + offset
            const n = index + 1
            return <li key={index}>
              <span>{entry.kind === 'panel' ? entry.panelId
                : `${rowNames.get(entry.frameIndex)} slots ${entry.fromSlot} to ${entry.toSlot}`}</span>
              <button className="chip-act" disabled={disabled} aria-label={`Remove selection ${n}`}
                onClick={() => { setQueue((items) => items.filter((_, i) => i !== index)); setCheckReason(null) }}>Remove</button>
              <button className="chip-act" disabled={disabled || index === 0} aria-label={`Move selection ${n} up`}
                onClick={() => move(index, -1)}>Move up</button>
              <button className="chip-act" disabled={disabled || index === queue.length - 1} aria-label={`Move selection ${n} down`}
                onClick={() => move(index, 1)}>Move down</button>
            </li>
          })}</ol>
          <Pages page={shownQueuePage} count={queue.length} onChange={setQueuePage} disabled={disabled} />
        </section>
        <section aria-label="Composed strings">
          <h3>Composed strings</h3>
          {!preview.ok ? <p className="solar-step-note" role="status">{preview.reason}</p> : <>
            <p>{preview.preview.ids.length} panels</p>
            <p>Saved maximum: {view.savedLimit}</p>
            <p>{sizes.length} strings</p>
            <ol start={shownPreviewPage * PAGE_SIZE + 1}>{sizes.slice(shownPreviewPage * PAGE_SIZE,
              (shownPreviewPage + 1) * PAGE_SIZE).map((size, index) =>
              <li key={index}>String {shownPreviewPage * PAGE_SIZE + index + 1}: {size} panels</li>)}</ol>
            <Pages page={shownPreviewPage} count={sizes.length} onChange={setPreviewPage} disabled={disabled} />
          </>}
        </section>
      </>}
    <button className="chip-act" disabled={disabled || !view?.ok || !preview.ok} onClick={review}>Review &amp; run</button>
    {status === 'failed' && <button className="chip-act" disabled={disabled || !view?.ok || !preview.ok}
      onClick={review}>Retry</button>}
    {cancel}
  </div>
}
