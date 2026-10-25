import React, { useEffect, useMemo, useRef, useState } from 'react'
import { humanizeError } from '../errorHumanize.js'
import {
  SOLAR_STRING_COMPOSER_REASONS, STRING_ADD_TOOL, STRING_MULTI_ADD_TOOL,
  MAX_SINGLE_REFS, MAX_MULTI_REFS, MAX_STRING_LENGTH, stringComposerView,
  composeStringRequest, checkStringFreshness, stringRunRefusal,
  PROJECT_STRING_COMPOSER_REASONS, projectStringComposerView, composeProjectStringRequest,
  checkProjectStringFreshness, projectStringPreviewResult,
} from './solarStringComposerModel.js'
import { createSolarDrawingBinding, sameSolarDrawingBinding, solarDrawingBindingKey } from './solarDrawingBinding.js'

const slotText = /^[1-9][0-9]{0,8}$/
const lengthText = /^[0-9]{1,4}$/
const PAGE_SIZE = 25
const SKIPPED = Symbol('skipped')

const projectJobId = new RegExp('^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
const localProjectRefusal = (code) => Object.freeze({ ok: false, code, reason: PROJECT_STRING_COMPOSER_REASONS[code] })

function ProjectStringComposer(props) {
  const { row, solarBinding, authorityKey, projectClient, catalogDigest, busy = false, onClose } = props
  let binding = null, bindingKey = null
  try { binding = createSolarDrawingBinding(solarBinding); bindingKey = solarDrawingBindingKey(binding) } catch { /* local refusal */ }
  const latest = useRef(props)
  latest.current = props
  const owner = useRef(null)
  if (!owner.current || owner.current.bindingKey !== bindingKey || owner.current.authorityKey !== authorityKey ||
      owner.current.client !== projectClient) {
    if (owner.current) {
      owner.current.alive = false
      for (const controller of owner.current.controllers) controller.abort()
    }
    owner.current = { binding, bindingKey, authorityKey, client: projectClient, alive: false,
      generation: 0, controllers: new Set(), candidateControllers: new Set(), predicates: new Set(),
      candidate: null, armed: null, lock: null, fingerprint: null, submitted: null }
  }
  const scope = owner.current
  const [loaded, setLoaded] = useState(null)
  const [queue, setQueue] = useState([])
  const [selected, setSelected] = useState(null)
  const [first, setFirst] = useState(null)
  const [last, setLast] = useState(null)
  const [lengthDraft, setLengthDraft] = useState(null)
  const [search, setSearch] = useState('')
  const [sourcePage, setSourcePage] = useState(0)
  const [queuePage, setQueuePage] = useState(0)
  const [previewPage, setPreviewPage] = useState(0)
  const [phase, setPhase] = useState('loading')
  const [feedback, setFeedback] = useState(null)
  const [refresh, setRefresh] = useState(0)
  const [reload, setReload] = useState(0)
  const [, renderScope] = useState(0)
  const supported = row?.name === STRING_ADD_TOOL || row?.name === STRING_MULTI_ADD_TOOL
  const dependencies = binding && typeof authorityKey === 'string' && authorityKey.trim().length > 0 &&
    projectClient && ['readContext', 'getSnapshot', 'subscribe', 'captureCatalogScope', 'prepareRun', 'catalog']
      .every((key) => typeof projectClient[key] === 'function')
  const available = dependencies && typeof props.newProjectIntentKey === 'function' && typeof props.onProjectSubmit === 'function'
  const snapshotMatches = () => {
    const snapshot = scope.client?.getSnapshot()
    return sameSolarDrawingBinding(snapshot?.binding, scope.binding) && snapshot?.authorityKey === scope.authorityKey
  }
  const isScopeCurrent = () => scope.alive && owner.current === scope &&
    latest.current.projectClient === scope.client && latest.current.authorityKey === scope.authorityKey &&
    sameSolarDrawingBinding(latest.current.solarBinding, scope.binding) && snapshotMatches()
  const invalidate = () => {
    scope.generation += 1
    scope.armed = null
    for (const controller of scope.candidateControllers) controller.abort()
  }
  const operation = (candidate = true, token = null) => {
    const generation = scope.generation
    const predicate = scope.client.captureCatalogScope()
    const controller = new AbortController()
    scope.controllers.add(controller)
    if (candidate) scope.candidateControllers.add(controller)
    scope.predicates.add(predicate)
    return { signal: controller.signal,
      isCurrent: () => isScopeCurrent() && !controller.signal.aborted && predicate() &&
        (!candidate || scope.generation === generation) && (!token || scope.lock === token),
      abort: () => controller.abort(),
      finish: () => {
        scope.controllers.delete(controller)
        scope.candidateControllers.delete(controller)
        scope.predicates.delete(predicate)
      } }
  }
  const publish = (refusal, outcome = null, admission = null) => {
    setFeedback({ scope, generation: scope.generation, refusal, outcome, admission })
  }

  useEffect(() => {
    scope.alive = true
    setLoaded(null); setQueue([]); setSelected(null); setFirst(null); setLast(null); setLengthDraft(null)
    setSearch(''); setSourcePage(0); setQueuePage(0); setPreviewPage(0); setFeedback(null); setPhase('loading')
    if (!supported || !available) return () => { scope.alive = false }
    let read = null
    let contextSettled = false
    const startRead = () => {
      if (!isScopeCurrent()) {
        setLoaded({ scope, view: localProjectRefusal('binding_invalid') })
        setPhase('refused')
        return
      }
      read?.abort()
      const current = operation(false)
      read = current
      // Schedule the actual read after mounting so obsolete StrictMode setups never start transport.
      Promise.resolve().then(async () => {
        if (!current.isCurrent()) { current.finish(); return }
        try {
          const outcome = await scope.client.readContext({ signal: current.signal })
          if (!current.isCurrent()) return
          contextSettled = true
          const mapped = outcome?.ok === true ? null : projectStringPreviewResult({ outcome, intent: null })
          const view = outcome?.ok === true ? projectStringComposerView({ binding: scope.binding, contextView: outcome.data })
            : mapped.code === 'preview_refused' ? localProjectRefusal('context_unreadable') : mapped
          setLoaded({ scope, view })
          setPhase(view.ok ? 'editing' : 'refused')
          if (!view.ok) publish(view, outcome)
        } catch {
          if (!current.isCurrent()) return
          contextSettled = true
          const refusal = localProjectRefusal('context_unreadable')
          setLoaded({ scope, view: refusal }); setPhase('refused'); publish(refusal)
        } finally { current.finish() }
      })
    }
    const unsubscribe = scope.client.subscribe(() => {
      if (!scope.alive || owner.current !== scope) return
      const matches = snapshotMatches()
      if (!matches || scope.clientMismatch || (scope.armed && !scope.armed.isCatalogCurrent()) ||
          [...scope.predicates].some((predicate) => !predicate())) {
        scope.clientMismatch = !matches
        invalidate()
        for (const controller of scope.controllers) controller.abort()
        scope.predicates.clear()
        if (matches) setFeedback(null)
        else publish(localProjectRefusal('scope_stale'))
        scope.needsPreview = true
        setPhase(scope.submitted && scope.submitted === scope.candidate ? 'submitted' : 'editing')
        setRefresh((value) => value + 1)
        if (!contextSettled) startRead()
      }
    })
    startRead()
    return () => {
      scope.alive = false
      invalidate()
      for (const controller of scope.controllers) controller.abort()
      scope.predicates.clear()
      unsubscribe()
    }
  }, [scope, Boolean(available), supported, reload])

  const view = loaded?.scope === scope ? loaded.view : null
  const multi = row?.name === STRING_MULTI_ADD_TOOL
  const text = lengthDraft ?? (view?.savedLimit >= 1 && view.savedLimit <= MAX_STRING_LENGTH ? String(view.savedLimit) : '')
  const stringLength = lengthText.test(text) ? Number(text) : text
  const preview = view?.ok ? composeProjectStringRequest({ binding, view, toolName: row.name, queue, stringLength,
    catalogDigest, idempotencyKey: 'candidate-validation' }) : localProjectRefusal('loading')
  const fingerprint = preview.ok ? JSON.stringify(preview.request) : null
  // Changing tools or catalog data can happen during render, before effect cleanup. Disarm immediately.
  if (scope.fingerprint !== fingerprint) {
    invalidate()
    scope.fingerprint = fingerprint
  }

  useEffect(() => {
    scope.armed = null
    if (scope.lock) { scope.needsPreview = true; return undefined }
    scope.needsPreview = false
    if (!fingerprint || !available || !isScopeCurrent()) return undefined
    const current = operation()
    let candidate = scope.candidate
    try {
      if (!candidate || candidate.fingerprint !== fingerprint) {
        const key = latest.current.newProjectIntentKey()
        if (!current.isCurrent()) { current.finish(); return undefined }
        const built = composeProjectStringRequest({ binding, view, toolName: row.name, queue, stringLength,
          catalogDigest, idempotencyKey: key })
        if (!built.ok) {
          publish(built); setPhase('refused'); current.finish()
          return undefined
        }
        const intent = scope.client.prepareRun(built.request)
        if (!current.isCurrent()) { current.finish(); return undefined }
        if (!sameSolarDrawingBinding(intent?.binding, binding) || intent?.authorityKey !== authorityKey) {
          publish(localProjectRefusal('request_invalid')); setPhase('refused'); current.finish()
          return undefined
        }
        candidate = { fingerprint, intent, preview: built.preview }
        scope.candidate = candidate
      }
    } catch {
      if (current.isCurrent()) { publish(localProjectRefusal('request_invalid')); setPhase('refused') }
      current.finish()
      return undefined
    }
    if (scope.submitted === candidate) {
      setPhase('submitted'); current.finish()
      return undefined
    }
    setFeedback(null); setPhase('previewing')
    Promise.resolve().then(async () => {
      if (!current.isCurrent()) { current.finish(); return }
      try {
        const outcome = await scope.client.catalog({ intents: [candidate.intent], signal: current.signal })
        if (!current.isCurrent() || scope.candidate !== candidate) return
        const checked = projectStringPreviewResult({ outcome, intent: candidate.intent })
        if (checked.ok) {
          // Retain R7's predicate even after catalog settlement: an armed preview can become stale.
          scope.armed = { candidate, candidateGeneration: scope.generation, isCatalogCurrent: current.isCurrent }
          setPhase('ready')
          if (candidate.submissionFeedback) publish(candidate.submissionFeedback.refusal,
            candidate.submissionFeedback.outcome)
          else setFeedback(null)
        } else {
          const rows = Array.isArray(outcome?.data?.families) ? outcome.data.families
            .flatMap((family) => Array.isArray(family?.capabilities) ? family.capabilities : []) : []
          const refusedRow = rows.find((item) => item?.name === candidate.intent.tool &&
            !projectStringPreviewResult({ outcome: { ok: true, data: { families: [{ capabilities: [item] }] } },
              intent: candidate.intent }).ok)
          const admission = refusedRow?.availability?.admission ?? null
          publish(checked, outcome, admission); setPhase('refused')
        }
      } catch {
        if (current.isCurrent()) { publish(localProjectRefusal('preview_refused')); setPhase('refused') }
      } finally { current.finish() }
    })
    return () => current.abort()
  }, [scope, fingerprint, available, refresh])

  const change = (setter, update) => {
    if (scope.lock || latest.current.busy) return
    invalidate()
    setFeedback(null); setPhase('editing')
    setter(update)
    setRefresh((value) => value + 1)
  }
  const sources = useMemo(() => {
    if (!view?.ok) return []
    const needle = search.toLowerCase()
    return view.design === 'Ground' ? view.rows.filter((item) => item.name.toLowerCase().includes(needle))
      : view.panels.filter((item) => item.id.toLowerCase().includes(needle) || item.frameName.toLowerCase().includes(needle))
  }, [view, search])
  const rowNames = useMemo(() => new Map((view?.rows ?? []).map((item) => [item.frameIndex, item.name])), [view])
  const disabled = busy || !!scope.lock
  const bound = multi ? MAX_MULTI_REFS : MAX_SINGLE_REFS
  const addDisabled = disabled || queue.length >= bound
  const append = (entry) => change(setQueue, (items) => items.length < bound ? [...items, entry] : items)
  const move = (index, offset) => change(setQueue, (items) => {
    const copy = items.slice()
    ;[copy[index], copy[index + offset]] = [copy[index + offset], copy[index]]
    return copy
  })
  const shownQueuePage = Math.min(queuePage, Math.max(0, Math.ceil(queue.length / PAGE_SIZE) - 1))
  const sizes = preview.ok ? preview.preview.sizes : []
  const shownPreviewPage = Math.min(previewPage, Math.max(0, Math.ceil(sizes.length / PAGE_SIZE) - 1))
  const armed = scope.armed && scope.armed.candidateGeneration === scope.generation &&
    scope.armed.candidate.fingerprint === fingerprint && scope.armed.isCatalogCurrent() &&
    scope.submitted !== scope.armed.candidate

  async function review() {
    if (scope.lock || latest.current.busy || !armed || !isScopeCurrent() || !scope.armed?.isCatalogCurrent() ||
        scope.armed.candidateGeneration !== scope.generation || scope.armed.candidate.fingerprint !== fingerprint ||
        scope.submitted === scope.armed.candidate) return
    const accepted = scope.armed
    const candidate = accepted.candidate
    const token = {}
    scope.lock = token
    const current = operation(true, token)
    setPhase('checking'); setFeedback(null)
    try {
      const outcome = await Promise.resolve().then(() => current.isCurrent()
        ? scope.client.readContext({ signal: current.signal }) : SKIPPED)
      if (outcome === SKIPPED || !current.isCurrent() || !accepted.isCatalogCurrent() ||
          scope.armed !== accepted || scope.candidate !== candidate) return
      const mapped = outcome?.ok === true ? null : projectStringPreviewResult({ outcome, intent: candidate.intent })
      const fresh = outcome?.ok === true ? checkProjectStringFreshness({ binding, view, contextView: outcome.data })
        : mapped.code === 'preview_refused' ? localProjectRefusal('context_unreadable') : mapped
      if (!fresh.ok) {
        scope.armed = null
        publish(fresh, outcome); setPhase('refused')
        return
      }
      if (!current.isCurrent() || latest.current.busy || typeof latest.current.onProjectSubmit !== 'function') return
      setPhase('submitting')
      let result
      const handoff = operation(true, token)
      const isHandoffCurrent = () => handoff.isCurrent() && current.isCurrent() &&
        accepted.isCatalogCurrent() && scope.candidate === candidate && scope.armed === accepted
      try {
        // All three async stages have their own R7 scope predicate, including rejection settlement.
        if (!isHandoffCurrent()) return
        result = await latest.current.onProjectSubmit(candidate.intent,
          { binding: scope.binding, authorityKey: scope.authorityKey, signal: handoff.signal, isCurrent: isHandoffCurrent })
        if (!isHandoffCurrent()) return
      } catch {
        if (!isHandoffCurrent()) return
        candidate.submissionFeedback = { refusal: localProjectRefusal('submission_uncertain'), outcome: null }
        publish(candidate.submissionFeedback.refusal); setPhase('refused')
        return
      } finally { handoff.finish() }
      if (result?.ok === true && result.status === 202 && result.data?.status === 'submitted' &&
          typeof result.data.job_id === 'string' && projectJobId.test(result.data.job_id)) {
        scope.submitted = candidate
        candidate.submissionFeedback = null
        publish(null, result); setPhase('submitted')
      } else {
        const uncertain = !result || result.ok === true || result.status === null || result.status === undefined || result.status >= 500
        candidate.submissionFeedback = { refusal: uncertain ? localProjectRefusal('submission_uncertain')
          : projectStringPreviewResult({ outcome: result, intent: candidate.intent }), outcome: result }
        publish(candidate.submissionFeedback.refusal, result)
        setPhase('refused')
      }
    } catch {
      if (current.isCurrent()) { publish(localProjectRefusal('context_unreadable')); setPhase('refused') }
    } finally {
      if (scope.lock === token) {
        scope.lock = null
        if (isScopeCurrent()) {
          if (scope.needsPreview) setRefresh((value) => value + 1)
          else renderScope((value) => value + 1)
        }
      }
      current.finish()
    }
  }

  const message = feedback?.scope === scope && feedback.generation === scope.generation ? feedback : null
  const refusal = !binding ? localProjectRefusal('binding_invalid') : !supported ? localProjectRefusal('request')
    : !dependencies || !available ? localProjectRefusal('submit_unavailable') : !view ? null
      : !view.ok ? view : preview.ok ? message?.refusal : null
  const cancel = <button className="chip-act" onClick={() => onClose?.()}>Cancel</button>
  if (!binding || !supported || !dependencies || !available) return <div><p className="solar-step-note" role="status">{refusal.reason}</p>{cancel}</div>
  return <div data-phase={phase}>
    {!view && <p className="solar-step-note" role="status">{PROJECT_STRING_COMPOSER_REASONS.loading}</p>}
    {refusal && <p className="solar-step-note" role={view?.ok ? 'alert' : 'status'}>{refusal.reason}</p>}
    {isScopeCurrent() && scope.submitted === scope.candidate && scope.submitted && scope.candidate.fingerprint === fingerprint &&
      <p className="solar-step-note" role="status">{PROJECT_STRING_COMPOSER_REASONS.submitted}</p>}
    {scope.lock && <p className="solar-step-note" role="status">{phase === 'submitting'
      ? PROJECT_STRING_COMPOSER_REASONS.pending : PROJECT_STRING_COMPOSER_REASONS.checking}</p>}
    {view?.ok && <>
      {!armed && preview.ok && !refusal && !scope.lock && scope.submitted !== scope.candidate &&
        <p className="solar-step-note" role="status">{PROJECT_STRING_COMPOSER_REASONS.preview_required}</p>}
      <StringSelection view={view} search={search} setSearch={setSearch} setSourcePage={setSourcePage}
        sources={sources} sourcePage={sourcePage} disabled={disabled} setSelected={setSelected}
        addDisabled={addDisabled} append={append} selected={selected} first={first} last={last}
        setFirst={setFirst} setLast={setLast} multi={multi} text={text} lengthDraft={lengthDraft}
        setLengthDraft={(value) => change(setLengthDraft, value)} setCheckReason={() => {}}
        queue={queue} shownQueuePage={shownQueuePage} rowNames={rowNames}
        setQueue={(update) => change(setQueue, update)} move={move} setQueuePage={setQueuePage}
        preview={preview} sizes={sizes} shownPreviewPage={shownPreviewPage} setPreviewPage={setPreviewPage} />
      <button className="chip-act" disabled={disabled || !armed} onClick={review}>Review &amp; run</button>
      {message?.refusal && <button className="chip-act" disabled={disabled || !armed} onClick={review}>Retry</button>}
      <button className="chip-act" disabled={disabled || !preview.ok || (!!scope.submitted && scope.submitted === scope.candidate)}
        onClick={() => { invalidate(); setFeedback(null); setRefresh((value) => value + 1) }}>Refresh preview</button>
    </>}
    {view && (!view.ok || ['scope_stale', 'stale', 'context_unreadable', 'not_current_head'].includes(message?.refusal?.code)) &&
      <button className="chip-act" disabled={disabled}
      onClick={() => setReload((value) => value + 1)}>Reload drawing</button>}
    {cancel}
  </div>
}


function StringSelection({ view, search, setSearch, setSourcePage, sources, sourcePage, disabled, setSelected,
  addDisabled, append, selected, first, last, setFirst, setLast, multi, text, lengthDraft,
  setLengthDraft, setCheckReason, queue, shownQueuePage, rowNames, setQueue, move,
  setQueuePage, preview, sizes, shownPreviewPage, setPreviewPage }) {
  return <>
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
  </>
}

function Pages({ page, count, onChange, disabled }) {
  const last = Math.max(0, Math.ceil(count / PAGE_SIZE) - 1)
  return <div>
    <button className="chip-act" disabled={disabled || page === 0}
      onClick={() => onChange(page - 1)}>Previous page</button>
    <button className="chip-act" disabled={disabled || page >= last}
      onClick={() => onChange(page + 1)}>Next page</button>
  </div>
}

export default function SolarStringComposer(props) {
  return Object.hasOwn(props, 'solarBinding')
    ? <ProjectStringComposer {...props} /> : <StandaloneStringComposer {...props} />
}

function StandaloneStringComposer({
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
  const [reloadRequired, setReloadRequired] = useState(false)
  const latestBinding = useRef(null)

  useEffect(() => {
    mounted.current = true
    return () => { mounted.current = false; generation.current += 1; lock.current = null }
  }, [])

  useEffect(() => {
    setQueue([]); setSelected(null); setFirst(null); setLast(null); setLengthDraft(null)
    setSearch(''); setSourcePage(0); setQueuePage(0); setPreviewPage(0)
    setCheckReason(null); setChecking(false); setReloadRequired(false); lock.current = null
  }, [binding])

  useEffect(() => {
    const number = ++generation.current
    if (!supported || scope.code === 'scope') return undefined
    setLoaded(null)
    const active = () => mounted.current && current.current === binding && generation.current === number
    // The reader runs only while this load is still the current one: a scope change, an unmount or a
    // second StrictMode setup before the microtask runs starts no read for the obsolete binding.
    const latest = latestBinding.current === binding
    Promise.resolve().then(() => (active() ? reader.current(drawingId, latest ? 'head' : drawingVersion) : SKIPPED)).then((envelope) => {
      if (envelope === SKIPPED || !active()) return
      const view = stringComposerView({ envelope, drawingId, drawingVersion: latest ? envelope?.version : drawingVersion, projectId })
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
  const disabled = checking || status === 'pending' || reloadRequired
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
        fresh = checkStringFreshness({ view: capturedView, drawingId,
          drawingVersion: capturedView.binding.drawingVersion, envelope })
      } catch {
        fresh = { ok: false, reason: SOLAR_STRING_COMPOSER_REASONS.unavailable }
      }
      if (!active()) return
      if (!fresh.ok) { setCheckReason(fresh.reason); setReloadRequired(true) }
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
    {checkReason && <p className="solar-step-note" role="alert">{humanizeError(checkReason)}</p>}
    {reloadRequired && <button className="chip-act" disabled={checking || status === 'pending'} onClick={() => {
      latestBinding.current = binding
      setQueue([]); setSelected(null); setFirst(null); setLast(null); setLengthDraft(null)
      setSearch(''); setSourcePage(0); setQueuePage(0); setPreviewPage(0)
      setCheckReason(null); setReloadRequired(false); setLoaded(null)
      setReload((value) => value + 1)
    }}>Reload drawing</button>}
    {!view ? <p className="solar-step-note" role="status">{SOLAR_STRING_COMPOSER_REASONS.loading}</p>
      : !view.ok ? <>
        <p className="solar-step-note" role="status">{view.reason}</p>
        <button className="chip-act" disabled={disabled} onClick={() => {
          latestBinding.current = binding
          setQueue([]); setSelected(null); setFirst(null); setLast(null); setLengthDraft(null)
          setCheckReason(null); setLoaded(null); setReload((value) => value + 1)
        }}>Reload drawing</button>
      </> : <>
        <StringSelection view={view} search={search} setSearch={setSearch} setSourcePage={setSourcePage}
          sources={sources} sourcePage={sourcePage} disabled={disabled} setSelected={setSelected}
          addDisabled={addDisabled} append={append} selected={selected} first={first} last={last}
          setFirst={setFirst} setLast={setLast} multi={multi} text={text} lengthDraft={lengthDraft}
          setLengthDraft={setLengthDraft} setCheckReason={setCheckReason} queue={queue}
          shownQueuePage={shownQueuePage} rowNames={rowNames} setQueue={setQueue} move={move}
          setQueuePage={setQueuePage} preview={preview} sizes={sizes}
          shownPreviewPage={shownPreviewPage} setPreviewPage={setPreviewPage} />
      </>}
    <button className="chip-act" disabled={disabled || !view?.ok || !preview.ok} onClick={review}>Review &amp; run</button>
    {status === 'failed' && <button className="chip-act" disabled={disabled || !view?.ok || !preview.ok}
      onClick={review}>Retry</button>}
    {cancel}
  </div>
}
