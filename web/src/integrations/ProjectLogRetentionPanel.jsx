import { useId, useLayoutEffect, useRef, useState } from 'react'
import './ProjectLogRetentionPanel.css'

const initialForm = () => ({ slug: '', version: '1', title: '', batchSize: '5000', maxBatches: '200',
  table: '', clock: '', scheduleSlug: '', scheduleVersion: '1', dueDate: '', execution: '',
  id: 'id', source: 'source', level: 'level', timestamp: 'timestamp' })
const failed = () => new Error('Retention action unavailable')
const number = value => { const n = Number(value); if (!value.trim() || !Number.isSafeInteger(n)) throw failed(); return n }
const refKey = dataset => JSON.stringify([dataset.reference, dataset.revision])
const cell = value => typeof value === 'string' ? value : JSON.stringify(value)

export function ProjectLogRetentionPanel(props) {
  const binding = [props.client, props.projectId, props.signal]
  const [context, setContext] = useState(() => ({ binding, epoch: 0 }))
  if (binding.some((value, index) => value !== context.binding[index])) {
    setContext({ binding, epoch: context.epoch + 1 })
    return null
  }
  return <ScopedRetention key={context.epoch} {...props} />
}

function ScopedRetention({ client, projectId, signal, guard, onForbidden, mode = 'light' }) {
  const prefix = useId(), scope = useRef(null), callbacks = useRef({ guard, onForbidden }), busy = useRef(false), stop = useRef(false)
  callbacks.current = { guard, onForbidden }
  const [form, setForm] = useState(initialForm), [pending, setPending] = useState(false)
  const [availability, setAvailability] = useState(signal?.aborted ? 'forbidden' : 'ready')
  const [error, setError] = useState(''), [notice, setNotice] = useState('')
  const [definition, setDefinition] = useState(null), [exported, setExported] = useState(null), [exportedAudit, setExportedAudit] = useState(null)
  const [datasets, setDatasets] = useState([]), [datasetAfter, setDatasetAfter] = useState(null), [selected, setSelected] = useState('')
  const [prepared, setPrepared] = useState(null), [schedule, setSchedule] = useState(null)
  const [run, setRun] = useState(''), [audit, setAudit] = useState(null), [preview, setPreview] = useState(null)
  const [review, setReview] = useState(null), [restoreReview, setRestoreReview] = useState(null)
  const [view, setView] = useState('current'), [page, setPage] = useState(null), [stack, setStack] = useState([undefined]), [pageIndex, setPageIndex] = useState(0)
  const dataset = datasets.find(value => refKey(value) === selected)
  const current = ctx => ctx && scope.current === ctx && !ctx.controller.signal.aborted && !signal?.aborted
  const clearReviews = () => { setReview(null); setRestoreReview(null) }
  const clearRun = () => {
    for (const controller of scope.current?.reads.values() ?? []) controller.abort()
    setRun(''); setAudit(null); setPreview(null); clearReviews(); setPage(null); setStack([undefined]); setPageIndex(0); setExportedAudit(null)
  }
  useLayoutEffect(() => {
    const ctx = { controller: new AbortController(), reads: new Map() }
    scope.current = ctx
    const abort = () => {
      ctx.controller.abort(); for (const controller of ctx.reads.values()) controller.abort()
      setAvailability('forbidden'); setDefinition(null); setExported(null); setDatasets([]); setSelected(''); setPrepared(null); setSchedule(null)
      clearRun(); setForm(initialForm()); busy.current = false; stop.current = true
    }
    if (!signal || typeof signal.addEventListener !== 'function') { abort(); return undefined }
    signal.addEventListener('abort', abort, { once: true })
    if (signal.aborted) abort()
    return () => { scope.current = null; ctx.controller.abort(); for (const controller of ctx.reads.values()) controller.abort(); signal.removeEventListener('abort', abort) }
  }, [signal])
  async function authorize(ctx, requestSignal = ctx?.controller.signal) {
    if (!current(ctx) || requestSignal?.aborted || typeof callbacks.current.guard !== 'function') throw failed()
    await callbacks.current.guard(requestSignal)
    if (!current(ctx) || requestSignal.aborted) throw failed()
  }
  async function invoke(ctx, method, args, requestSignal = ctx.controller.signal, options = {}) {
    await authorize(ctx, requestSignal)
    if (typeof client?.[method] !== 'function') throw failed()
    const result = await client[method](projectId, ...args, { ...options, signal: requestSignal })
    await authorize(ctx, requestSignal)
    return result
  }
  function report(ctx, cause, rowFailure = false) {
    if (!current(ctx)) return
    clearReviews()
    if (rowFailure || page) setPage(null)
    if (cause?.status === 403 || cause?.status === 401 || cause?.code === 'forbidden') {
      setAvailability('forbidden'); setDatasets([]); setDefinition(null); setPrepared(null); setSchedule(null); setExported(null); setForm(initialForm()); clearRun()
      callbacks.current.onForbidden?.()
    }
    setError(cause?.dispatched ? 'The request may have reached the server. Refresh run before doing anything else. No automatic retry was made.'
      : 'This action could not be completed. Check the inputs and refresh before trying again.')
  }
  async function action(work) {
    const ctx = scope.current
    if (!current(ctx) || busy.current) return
    busy.current = true; stop.current = false; setPending(true); setError(''); setNotice('')
    try { await authorize(ctx); await work(ctx) }
    catch (cause) { report(ctx, cause) }
    finally { if (current(ctx)) { busy.current = false; setPending(false) } }
  }
  async function read(key, work, rowFailure = false) {
    const ctx = scope.current
    if (!current(ctx)) return
    ctx.reads.get(key)?.abort()
    const controller = new AbortController(); ctx.reads.set(key, controller)
    const abort = () => controller.abort(); ctx.controller.signal.addEventListener('abort', abort, { once: true })
    setError('')
    try { await work(ctx, controller.signal) }
    catch (cause) { if (!controller.signal.aborted) report(ctx, cause, rowFailure) }
    finally { ctx.controller.signal.removeEventListener('abort', abort); if (ctx.reads.get(key) === controller) ctx.reads.delete(key) }
  }
  function edit(key, value) {
    setForm(previous => ({ ...previous, [key]: value })); clearReviews(); setExported(null)
    if (['slug', 'version', 'title', 'batchSize', 'maxBatches'].includes(key)) { setDefinition(null); setSchedule(null); clearRun() }
    if (['table', 'clock', 'id', 'source', 'level', 'timestamp'].includes(key)) { setPrepared(null); setSchedule(null); clearRun() }
    if (key === 'scheduleSlug' || key === 'scheduleVersion') setSchedule(null)
    if (key === 'execution') clearRun()
  }
  const input = (key, label, type = 'text') => <label htmlFor={`${prefix}-${key}`}>{label}<input id={`${prefix}-${key}`} type={type}
    value={form[key]} disabled={pending || availability !== 'ready'} onChange={event => edit(key, event.target.value)} /></label>
  async function rows(ctx, id, targetView, after, requestSignal = ctx.controller.signal) {
    const result = await invoke(ctx, 'retentionRows', [id], requestSignal, { view: targetView, limit: 25, ...(after ? { after } : {}) })
    setPage(result)
  }
  async function refresh(ctx, id, requestSignal = ctx.controller.signal) {
    const value = await invoke(ctx, 'retentionAudit', [id], requestSignal)
    setAudit(value)
    await rows(ctx, id, view, undefined, requestSignal)
    setStack([undefined]); setPageIndex(0)
  }
  function openRun() {
    clearRun()
    const id = form.execution.trim()
    read('run', async (ctx, requestSignal) => {
      const value = await invoke(ctx, 'retentionAudit', [id], requestSignal)
      setRun(value.resourceId); setAudit(value)
      await rows(ctx, value.resourceId, view, undefined, requestSignal)
    }, true)
  }
  function loadRows(targetView, after, index, nextStack) {
    scope.current?.reads.get('run')?.abort()
    setPage(null)
    read('rows', async (ctx, requestSignal) => {
      await rows(ctx, run, targetView, after, requestSignal)
      setStack(nextStack); setPageIndex(index)
    }, true)
  }
  async function applyReviewed(ctx) {
    if (!review || review.mode !== 'apply' || !preview || !run) throw failed()
    const id = run, reviewId = review.reviewId, cap = preview.settings.maxBatchesPerClass
    if (!Number.isSafeInteger(cap) || cap < 1 || cap > 2000) throw failed()
    for (const kind of ['build', 'other']) {
      if (preview[kind].eligible_count === 0) continue
      for (let index = 0; index < cap; index++) {
        if (stop.current) { setNotice('Stopped after the last acknowledged batch.'); return }
        const result = await invoke(ctx, 'retentionApply', [id, { reviewId, class: kind }])
        await refresh(ctx, id)
        if (stop.current) { setNotice('Stopped after the last acknowledged batch.'); return }
        if (result.stop_reason === 'deadline') { setNotice(`Retention stopped: ${result.stop_reason}.`); clearReviews(); return }
        if (result.stop_reason === 'conflict') throw Object.assign(failed(), { dispatched: true })
        // A zero-effect cap is local to this class. A resumed run can still
        // have reviewed work left in the other class.
        if (result.changed_count === 0) break
        if (result.stop_reason === 'max_batches') { setNotice(`Retention stopped: ${result.stop_reason}.`); clearReviews(); return }
        if (result.stop_reason === 'complete') { clearReviews(); setNotice('Reviewed retention finished. Counts are from the refreshed run.'); return }
      }
    }
    clearReviews(); setNotice('Reviewed retention finished. Counts are from the refreshed run.')
  }
  async function restoreReviewed(ctx) {
    if (!restoreReview || restoreReview.mode !== 'restore' || !run || typeof globalThis.crypto?.getRandomValues !== 'function') throw failed()
    const bytes = globalThis.crypto.getRandomValues(new Uint8Array(32))
    const invocationId = Array.from(bytes, value => value.toString(16).padStart(2, '0')).join('')
    const id = run
    const authority = await invoke(ctx, 'retentionAdmitRestore', [id, { reviewId: restoreReview.reviewId, invocationId }])
    await invoke(ctx, 'retentionRestore', [id, { authorityId: authority.authorityId }])
    clearReviews(); await refresh(ctx, id); setNotice('Restore completed. Counts are from the refreshed run.')
  }
  function download(value, name) {
    action(async ctx => {
      await authorize(ctx)
      const blob = new Blob([JSON.stringify(value, null, 2)], { type: 'application/json' })
      await authorize(ctx)
      const url = URL.createObjectURL(blob)
      try {
        const link = document.createElement('a')
        link.href = url; link.download = name; document.body.appendChild(link)
        try { link.click() } finally { link.remove() }
      } finally { URL.revokeObjectURL(url) }
    })
  }
  const disabled = pending || availability !== 'ready'
  const columns = [...new Set((page?.items ?? []).flatMap(row => Object.keys(row)))].slice(0, 32)
  return <section className="retention-panel" data-mode={mode} aria-label="Ops log retention">
    <header><h3>Log retention</h3><p>Preview first, then review and apply. Daily internal occurrence: 09:45 UTC. External activation is disabled. Server admission fixes a 240-second deadline.</p></header>
    {availability !== 'ready' && <p role="alert">Project access is unavailable. Reopen this project after access is restored.</p>}
    {availability === 'ready' && error && <p role="alert">{error}</p>}{notice && <p role="status">{notice}</p>}{pending && <p role="status">Waiting for the current operation…</p>}
    <fieldset disabled={disabled}><legend>Reusable definition</legend><div className="retention-grid">
      {input('slug', 'Definition slug')}{input('version', 'Definition version', 'number')}{input('title', 'Definition title')}
      {input('batchSize', 'Batch size', 'number')}{input('maxBatches', 'Maximum batches per class', 'number')}</div>
      <div className="retention-actions"><button type="button" onClick={() => action(async ctx => {
        const value = await invoke(ctx, 'retentionRegisterDefinition', [{ id: form.slug.trim(), version: number(form.version), title: form.title,
          settings: { batchSize: number(form.batchSize), maxBatchesPerClass: number(form.maxBatches) } }])
        setDefinition(value); setNotice('Definition saved as an immutable version.')
      })}>Save definition</button><button type="button" onClick={() => { clearRun(); setExported(null); read('definition', async (ctx, requestSignal) => {
        const value = await invoke(ctx, 'retentionGetDefinition', [form.slug.trim(), number(form.version)], requestSignal)
        setDefinition(value); setForm(previous => ({ ...previous, title: value.definition.title ?? '', batchSize: String(value.definition.settings?.batchSize ?? 5000), maxBatches: String(value.definition.settings?.maxBatchesPerClass ?? 200) }))
        clearReviews(); setSchedule(null)
      }) }}>Open definition</button><button type="button" disabled={!definition} onClick={() => read('export', async (ctx, requestSignal) => {
        setExported(await invoke(ctx, 'retentionExportDefinition', [form.slug.trim(), number(form.version)], requestSignal))
      })}>Export definition</button></div>{definition && <p>Saved definition: {definition.resourceId}, revision {definition.revision}.</p>}
      {exported && <><details open><summary>Exported definition</summary><pre>{JSON.stringify(exported, null, 2)}</pre></details>
        <button type="button" onClick={() => download(exported, 'retention-definition.json')}>Download definition</button></>}
    </fieldset>
    <fieldset disabled={disabled}><legend>Original imported records</legend><div className="retention-actions"><button type="button" onClick={() => read('datasets', async (ctx, requestSignal) => {
        const result = await invoke(ctx, 'listDatasets', [], requestSignal, { limit: 25 }); setDatasets(result.records); setDatasetAfter(result.nextCursor)
      })}>Load datasets</button><button type="button" disabled={!datasetAfter} onClick={() => read('datasets', async (ctx, requestSignal) => {
        const result = await invoke(ctx, 'listDatasets', [], requestSignal, { limit: 25, after: datasetAfter });
        setDatasets(previous => [...new Map([...previous, ...result.records].map(value => [refKey(value), value])).values()]); setDatasetAfter(result.nextCursor)
      })}>More datasets</button></div>
      <label htmlFor={`${prefix}-dataset`}>Imported dataset<select id={`${prefix}-dataset`} value={selected} onChange={event => { setSelected(event.target.value); setPrepared(null); setSchedule(null); clearRun() }}>
        <option value="">Choose imported records</option>{datasets.map(value => <option key={refKey(value)} value={refKey(value)} disabled={!value.material_context || !!value.derivation}>{value.title || value.reference.id}</option>)}</select></label>
      <div className="retention-grid">{input('table', 'Logical table ID')}{input('clock', 'Retention clock (UTC milliseconds)')}
        {['id', 'source', 'level', 'timestamp'].map(key => <label key={key} htmlFor={`${prefix}-${key}`}>{({ id: 'Record ID field', source: 'Source field', level: 'Level field', timestamp: 'Timestamp field' })[key]}
          <input id={`${prefix}-${key}`} list={`${prefix}-fields`} value={form[key]} onChange={event => edit(key, event.target.value)} /></label>)}
      </div><datalist id={`${prefix}-fields`}>{(dataset?.fields ?? []).map(field => <option key={field.id} value={field.id}>{field.label}</option>)}</datalist>
      <button type="button" disabled={!dataset?.material_context || !!dataset?.derivation} onClick={() => action(async ctx => {
        const value = await invoke(ctx, 'retentionPrepare', [{ datasetRef: dataset.reference, expectedDatasetRevision: dataset.revision,
          tableId: form.table.trim(), fields: { id: form.id, source: form.source, level: form.level, timestamp: form.timestamp }, retentionClock: form.clock.trim() }])
        setPrepared(value); setSchedule(null); clearRun(); setNotice('Original records prepared from the selected imported revision.')
      })}>Prepare records</button>{prepared && <p>Prepared table: {prepared.tableId}.</p>}
    </fieldset>
    <fieldset disabled={disabled}><legend>Internal schedule and occurrence</legend><div className="retention-grid">{input('scheduleSlug', 'Schedule slug')}{input('scheduleVersion', 'Schedule version', 'number')}{input('dueDate', 'Occurrence due date', 'date')}</div>
      <div className="retention-actions"><button type="button" disabled={!definition || !prepared} onClick={() => action(async ctx => {
        setSchedule(await invoke(ctx, 'retentionRegisterSchedule', [{ id: form.scheduleSlug.trim(), version: number(form.scheduleVersion),
          definition: { resourceId: definition.resourceId, revision: definition.revision }, preparedTableId: prepared.tableId }]))
        clearReviews(); setNotice('Internal schedule saved. No timer or external trigger was started.')
      })}>Save internal schedule</button><button type="button" disabled={!schedule || !definition || !prepared || !form.dueDate} onClick={() => action(async ctx => {
        const value = await invoke(ctx, 'retentionAdmit', [{ schedule: { resourceId: schedule.resourceId, revision: schedule.revision },
          definition: { resourceId: definition.resourceId, revision: definition.revision }, preparedTableId: prepared.tableId, dueAt: `${form.dueDate}T09:45:00.000Z` }])
        clearRun(); setRun(value.resourceId); setAudit(value); setForm(previous => ({ ...previous, execution: value.resourceId })); await rows(ctx, value.resourceId, view)
      })}>Admit internal run</button></div>
    </fieldset>
    <fieldset disabled={disabled}><legend>Open and inspect a run</legend>{input('execution', 'Execution ID')}<div className="retention-actions">
      <button type="button" onClick={openRun}>Open run</button><button type="button" disabled={!run} onClick={() => read('run', (ctx, requestSignal) => refresh(ctx, run, requestSignal), true)}>Refresh run</button>
      <button type="button" disabled={!run} onClick={() => read('plan', async (ctx, requestSignal) => { clearReviews(); setPreview(await invoke(ctx, 'retentionPlan', [run, {}], requestSignal)) })}>Preview retention</button>
      <button type="button" disabled={!audit} onClick={() => read('audit-export', async (ctx, requestSignal) => { setExportedAudit(await invoke(ctx, 'retentionAudit', [run], requestSignal)) })}>Export audit</button>
    </div>{preview && <section aria-label="Retention preview"><p>Dry run. Active: {preview.active_count}. Retained: {preview.retained_count}. Batch size: {preview.settings.batchSize}. Maximum batches per class: {preview.settings.maxBatchesPerClass}.</p>
      <p>Build eligible: {preview.build.eligible_count}; cutoff {preview.build.retentionCutoff}. Other eligible: {preview.other.eligible_count}; cutoff {preview.other.retentionCutoff}.</p></section>}
    {audit && <section aria-label="Actual run audit"><p>Active {audit.counters.active} of {audit.counters.input}. Deleted build {audit.counters.deletedBuild}; other {audit.counters.deletedOther}. Restored {audit.counters.restored}.</p>
      <p>Admitted {audit.admittedAt}. Parent deadline {audit.parentDeadlineAt}. {audit.expired ? 'Deadline reached.' : 'Deadline is controlled by the server.'} Status: {audit.checkpoint.stop_reason || audit.checkpoint.phase}.</p>
      <p>Archive count {audit.counters.archived}. Content hash <code>{audit.archiveDigest}</code>.</p></section>}
    {exportedAudit && <><details open><summary>Exported audit</summary><pre>{JSON.stringify(exportedAudit, null, 2)}</pre></details>
      <button type="button" onClick={() => download(exportedAudit, 'retention-audit.json')}>Download audit</button></>}
    </fieldset>
    <section aria-label="Reviewed changes" className="retention-actions">
      <button type="button" disabled={disabled || !run || !preview} onClick={() => action(async ctx => { setRestoreReview(null); setReview(await invoke(ctx, 'retentionReview', [run, { mode: 'apply' }])) })}>Review deletion</button>
      <button type="button" disabled={disabled || !review} onClick={() => action(applyReviewed)}>Apply reviewed retention</button>
      <button type="button" disabled={!pending} onClick={() => { stop.current = true; setNotice('Stop requested. Waiting for the current batch receipt.') }}>Stop after current batch</button>
      <button type="button" disabled={disabled || !run} onClick={() => action(async ctx => { setReview(null); setRestoreReview(await invoke(ctx, 'retentionReview', [run, { mode: 'restore' }])) })}>Review restore</button>
      <button type="button" disabled={disabled || !restoreReview} onClick={() => action(restoreReviewed)}>Restore archived records</button>
      <button type="button" disabled={disabled || !run} onClick={() => action(async ctx => { clearReviews(); await invoke(ctx, 'retentionRecover', [run]); await refresh(ctx, run); setNotice('Expired batch metadata recovered. Nothing was resumed.') })}>Recover expired batch metadata</button>
      {review && <p role="status">Deletion reviewed against archive count {review.archive_count}, hash {review.archive_digest}.</p>}
      {restoreReview && <p role="status">Restore reviewed against archive count {restoreReview.archive_count}, hash {restoreReview.archive_digest}.</p>}
    </section>
    <section aria-label="Persisted records"><div className="retention-actions">{['current', 'archive'].map(value => <button type="button" key={value} aria-pressed={view === value} disabled={disabled || !run} onClick={() => {
      setView(value); setStack([undefined]); setPageIndex(0); loadRows(value, undefined, 0, [undefined])
    }}>{value === 'current' ? 'Current records' : 'Archive records'}</button>)}</div>
      {run && !page && <p>No rows loaded. <button type="button" disabled={disabled} onClick={() => loadRows(view, stack[pageIndex], pageIndex, stack)}>Retry rows</button></p>}
      {page && <><p>{page.total} persisted {view} records.</p>{page.items.length === 0 ? <p>No records in this view.</p> : <div className="retention-table-frame"><table><thead><tr>{columns.map(key => <th scope="col" key={key}>{key}</th>)}</tr></thead>
        <tbody>{page.items.map((row, index) => <tr key={`${pageIndex}-${index}`}>{columns.map(key => <td key={key}><span>{cell(row[key])}</span></td>)}</tr>)}</tbody></table></div>}
        <div className="retention-actions"><button type="button" disabled={disabled || pageIndex === 0} onClick={() => loadRows(view, stack[pageIndex - 1], pageIndex - 1, stack)}>Previous records</button>
          <button type="button" disabled={disabled || !page.nextCursor} onClick={() => { const next = [...stack.slice(0, pageIndex + 1), page.nextCursor]; loadRows(view, page.nextCursor, pageIndex + 1, next) }}>Next records</button></div></>}
    </section>
  </section>
}
