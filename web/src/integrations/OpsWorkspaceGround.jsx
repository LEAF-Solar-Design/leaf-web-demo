import { useLayoutEffect, useMemo, useRef, useState } from 'react'
import { DatasetElement } from '../../../vendor/assistant-react/src/datasets/DatasetElement'
import { DisplayPreferencesSchema } from '../../../vendor/assistant-react/src/datasets/formatting'
import { DatasetElementV1, DatasetQueryV1 } from '../../../vendor/assistant-contract/src/integration'
import { createIntegrationsClient, UUID } from './client.js'
import { NO_OCCLUDERS, useGroundWindow, windowStyle } from '../site/groundWindow.js'
import './ops-workspace.css'

const empty = () => ({ scope: null, datasets: [], elements: [], datasetCursor: null, elementCursor: null, selected: null, read: null, historical: false, pending: '', error: '', notice: '' })
const blankForm = () => ({ dataset: '', id: '', title: '', kind: 'table', columns: [], x: '', y: '', editing: null })
const xKinds = ['date', 'instant', 'number', 'count']
const yKinds = ['number', 'count', 'money', 'ratio', 'duration']
const referenceKey = (ref) => JSON.stringify([ref.id, ref.version, ref.digest])
export function scopeCacheIdentity(scope, generation) {
  return JSON.stringify([scope.tenantId, scope.projectId, scope.platformUserId, scope.bindingId, scope.projectRole, scope.externalAuthority, scope.externalSubject, generation])
}

export function OpsWorkspaceGround({ active = false, projectId = null, projectLabel, session, signedIn = false, accountScope, mock = false,
  leavingGround = null, studioShell = false, occluders = NO_OCCLUDERS, client: suppliedClient }) {
  const client = useMemo(() => suppliedClient || createIntegrationsClient(), [suppliedClient])
  const [state, setState] = useState(empty)
  const [form, setForm] = useState(blankForm)
  const [epoch, setEpoch] = useState(0)
  const [retry, setRetry] = useState(0)
  const [revision, setRevision] = useState('')
  const [preferences, setPreferences] = useState({ locale: 'en-US', timezone: 'UTC', hourFormat: '24h' })
  const [preferenceError, setPreferenceError] = useState('')
  const boardRef = useRef(null)
  const resultRef = useRef(null)
  const generation = useRef(0)
  const context = useRef(null)
  const viewGeneration = useRef(0)
  const busy = useRef(false)
  const snapshot = session?.controller?.getSnapshot?.() || session
  const sessionGeneration = snapshot?.generation
  const status = snapshot?.status
  const eligible = active && signedIn && !mock && UUID.test(projectId || '') && status === 'active'
  // Comparing the render identity prevents a single frame of old-account content.
  const identityKey = JSON.stringify([projectId, accountScope, signedIn, mock, sessionGeneration, status, active, epoch])
  const identity = useMemo(() => ({ key: identityKey }), [identityKey, session?.controller])
  const visible = state.identity === identity && eligible
  const leaving = leavingGround === 'integrations'
  const win = useGroundWindow(active || leaving, studioShell, boardRef, occluders)

  function invalidate() {
    generation.current += 1
    context.current?.controller.abort()
    context.current = null
    busy.current = false
    viewGeneration.current += 1
  }
  useLayoutEffect(() => {
    const controller = session?.controller
    if (!controller) return undefined
    let previous = controller.getSnapshot()
    return controller.subscribe(() => {
      const next = controller.getSnapshot()
      if (next.generation !== previous.generation || next.status !== 'active') {
        invalidate()
        setState(empty())
        setForm(blankForm())
        setRevision('')
        setEpoch((value) => value + 1)
      }
      previous = next
    })
  }, [session?.controller])

  const current = (ctx) => {
    const now = session?.controller?.getSnapshot?.()
    return context.current === ctx && !ctx.controller.signal.aborted && generation.current === ctx.generation &&
      (!now || (now.status === 'active' && now.generation === ctx.sessionGeneration))
  }
  function fail(error, ctx) {
    if (!current(ctx) || error?.name === 'AbortError') return
    if (error?.status === 403) {
      invalidate()
      setState({ ...empty(), identity, error: 'You do not have permission to view these integrations.' })
      setForm(blankForm())
      setRevision('')
    } else setState((old) => ({ ...old, pending: '', error: error?.message || 'Integrations are unavailable. Please retry.' }))
  }
  useLayoutEffect(() => {
    invalidate()
    setState({ ...empty(), identity, pending: eligible ? 'Loading integrations…' : '' })
    setForm(blankForm())
    setRevision('')
    if (!eligible) return undefined
    const ctx = { controller: new AbortController(), generation: generation.current, sessionGeneration }
    context.current = ctx
    ;(async () => {
      try {
        const scope = await client.getScope(projectId, { signal: ctx.controller.signal })
        if (!current(ctx)) return
        ctx.cacheIdentity = scopeCacheIdentity(scope, sessionGeneration)
        const [datasets, elements] = await Promise.all([
          client.listDatasets(projectId, { signal: ctx.controller.signal }),
          client.listElements(projectId, { signal: ctx.controller.signal }),
        ])
        if (!current(ctx)) return
        setState({ ...empty(), identity, scope, datasets: datasets.records, elements: elements.records,
          datasetCursor: datasets.nextCursor, elementCursor: elements.nextCursor })
      } catch (error) { fail(error, ctx) }
    })()
    return invalidate
  }, [identity, retry, client])

  async function operation(label, action) {
    const ctx = context.current
    if (!ctx || !current(ctx) || busy.current) return
    busy.current = true
    setState((old) => ({ ...old, pending: label, error: '', notice: '' }))
    try { await action(ctx) } catch (error) { fail(error, ctx) }
    finally {
      if (current(ctx)) {
        busy.current = false
        setState((old) => ({ ...old, pending: '' }))
      }
    }
  }
  async function readView(record, historical, ctx, cursor) {
    const request = ++viewGeneration.current
    setState((old) => ({ ...old, selected: record, historical, read: null }))
    const read = await client.queryDataset(projectId, record.element.dataset_ref,
      DatasetQueryV1.parse({ ...record.element.query, ...(cursor ? { cursor } : {}) }), { signal: ctx.controller.signal })
    if (!current(ctx) || request !== viewGeneration.current) return
    if (read.status === 'forbidden') { fail({ status: 403 }, ctx); return }
    setState((old) => ({ ...old, read }))
  }
  function chooseSaved(record) {
    operation('Loading saved view…', async (ctx) => {
      setRevision('')
      setForm(blankForm())
      await readView(record, false, ctx)
    })
  }
  function compose(event) {
    event.preventDefault()
    operation('Saving view…', async (ctx) => {
      const dataset = state.datasets.find((item) => referenceKey(item.reference) === form.dataset)
      if (!dataset) throw new Error('Select an authorized dataset.')
      const fields = form.kind === 'table' ? form.columns : [...new Set([form.x, form.y])]
      if (fields.some((id) => !dataset.fields.some((field) => field.id === id))) throw new Error('Select authorized fields.')
      if (form.kind === 'line' && (!xKinds.includes(dataset.fields.find((field) => field.id === form.x)?.type.kind) ||
        !yKinds.includes(dataset.fields.find((field) => field.id === form.y)?.type.kind))) throw new Error('Choose compatible line axes.')
      const parsed = DatasetElementV1.safeParse({ schema_version: 1, id: form.id, title: form.title.trim(),
        dataset_ref: dataset.reference, query: { fields, limit: 100 }, kind: `dataset.${form.kind}`,
        props: form.kind === 'table' ? { columns: fields, row_limit: 100 } : { x_field: form.x, y_field: form.y } })
      if (!parsed.success) throw new Error('Enter a title, a stable lowercase slug ID, and valid fields.')
      if (!form.editing && state.elements.some((record) => record.element.id === form.id)) throw new Error('Select that saved view and choose Edit current recipe to update it.')
      const saved = await client.saveElement(projectId, parsed.data, { signal: ctx.controller.signal,
        ...(form.editing ? { expectedRevision: form.editing.revision } : {}) })
      if (!current(ctx)) return
      setState((old) => ({ ...old, notice: `Saved ${saved.element.title}, revision ${saved.revision}.`,
        elements: [...old.elements.filter((record) => record.element.id !== saved.element.id), saved] }))
      setForm((old) => ({ ...old, editing: saved }))
      resultRef.current?.focus()
      await readView(saved, false, ctx)
    })
  }
  function editSelected() {
    const saved = state.selected
    const element = saved.element
    setForm({ dataset: referenceKey(element.dataset_ref), id: element.id, title: element.title, kind: element.kind === 'dataset.table' ? 'table' : 'line',
      columns: element.props.columns || [], x: element.props.x_field || '', y: element.props.y_field || '', editing: saved })
  }
  function loadMore(resource) {
    operation('Loading more…', async (ctx) => {
      const isDataset = resource === 'datasets'
      const page = await client[isDataset ? 'listDatasets' : 'listElements'](projectId, { signal: ctx.controller.signal,
        after: isDataset ? state.datasetCursor : state.elementCursor })
      if (current(ctx)) setState((old) => ({ ...old, [resource]: [...old[resource], ...page.records],
        [isDataset ? 'datasetCursor' : 'elementCursor']: page.nextCursor }))
    })
  }
  const dataset = state.datasets.find((item) => referenceKey(item.reference) === form.dataset)
  const pending = Boolean(state.pending)
  const message = mock ? 'Integrations are unavailable in demo mode.' : !signedIn ? 'Sign in to view integrations.' :
    !UUID.test(projectId || '') ? 'Open a project to view integrations.' : status !== 'active' ? 'An active session is required to view integrations.' : null
  return <section ref={boardRef} className="studio-ground-ops" data-ground="ops" hidden={!active && !leaving}
    data-studio-shell={studioShell ? 'cockpit' : undefined} aria-label="Ops workspace" aria-hidden={leaving ? 'true' : undefined}
    inert={leaving ? '' : undefined} data-ground-phase={leaving ? 'leaving' : active && leavingGround ? 'entering' : undefined}>
    <div className="ops-workspace" style={windowStyle(win)}>
      <h1>Ops workspace</h1><p>{projectLabel || 'Project'}{projectId ? ` · ${projectId}` : ''}</p>
      {message ? <p role="status">{message}</p> : visible && <>
        <p role="status" aria-live="polite" tabIndex={-1} ref={resultRef}>{state.notice || state.pending}</p>
        {state.error && <div role="alert"><p>{state.error}</p><button onClick={() => setRetry((value) => value + 1)}>Retry integrations</button></div>}
        {state.scope && <>
          <section aria-label="Integration datasets"><h2>Datasets</h2>
            {!state.datasets.length && <p>No connection or dataset is available. Integration setup is needed.</p>}
            <ul>{state.datasets.map((item) => <li key={`${item.reference.id}:${item.reference.version}`}>{item.title} · version {item.reference.version}</li>)}</ul>
            {state.datasetCursor && <button disabled={pending} onClick={() => loadMore('datasets')}>Load more datasets</button>}
          </section>
          <section aria-label="Saved views"><h2>Saved views</h2>
            {!state.elements.length && <p>No saved views yet.</p>}
            <ul>{state.elements.map((record) => <li key={record.element.id}><button disabled={pending} onClick={() => chooseSaved(record)}>{record.element.title} · revision {record.revision}</button></li>)}</ul>
            {state.elementCursor && <button disabled={pending} onClick={() => loadMore('elements')}>Load more saved views</button>}
          </section>
          <form onSubmit={compose} aria-label="Compose native view"><h2>Compose native view</h2>
            <fieldset disabled={pending}><legend>{form.editing ? `Update revision ${form.editing.revision}` : 'Create a view'}</legend>
              <label>Dataset<select required value={form.dataset} onChange={(event) => setForm({ ...blankForm(), dataset: event.target.value })}>
                <option value="">Select dataset</option>{state.datasets.map((item) => <option key={referenceKey(item.reference)} value={referenceKey(item.reference)}>{item.title} · version {item.reference.version}</option>)}
              </select></label>
              <label>Title<input required maxLength={120} value={form.title} onChange={(event) => setForm({ ...form, title: event.target.value })} /></label>
              <label>Stable ID<input required maxLength={128} pattern="[a-z0-9]+([-_][a-z0-9]+)*" disabled={Boolean(form.editing)} value={form.id} onChange={(event) => setForm({ ...form, id: event.target.value })} /></label>
              <label>View type<select value={form.kind} onChange={(event) => setForm({ ...form, kind: event.target.value })}><option value="table">Table</option><option value="line">Line</option></select></label>
              {form.kind === 'table' ? <fieldset><legend>Table columns</legend>{dataset?.fields.map((field) => <label key={field.id}><input type="checkbox" checked={form.columns.includes(field.id)} onChange={(event) => setForm({ ...form, columns: event.target.checked ? [...form.columns, field.id] : form.columns.filter((id) => id !== field.id) })} />{field.label}</label>)}</fieldset> : <>
                {['x', 'y'].map((axis) => <label key={axis}>{axis.toUpperCase()} field<select required value={form[axis]} onChange={(event) => setForm({ ...form, [axis]: event.target.value })}>
                  <option value="">Select field</option>{dataset?.fields.filter((field) => (axis === 'x' ? xKinds : yKinds).includes(field.type.kind)).map((field) => <option key={field.id} value={field.id}>{field.label}</option>)}
                </select></label>)}
              </>}
              <button type="submit">{form.editing ? 'Update view' : 'Save view'}</button>
              {form.editing && <button type="button" onClick={() => setForm(blankForm())}>Create another view</button>}
            </fieldset>
          </form>
          <fieldset><legend>Personal display preferences</legend><p>Local to this view. Dataset reporting timezone is unchanged; these preferences are not saved to your organization.</p>
            {['locale', 'timezone', 'hourFormat'].map((key) => <label key={key}>{({ locale: 'Display locale', timezone: 'Display timezone', hourFormat: 'Hour cycle' })[key]}
              <select value={preferences[key]} onChange={(event) => {
                const parsed = DisplayPreferencesSchema.safeParse({ ...preferences, [key]: event.target.value })
                if (parsed.success) { setPreferences(parsed.data); setPreferenceError('') } else setPreferenceError('Choose valid display preferences.')
              }}>{({ locale: ['en-US', 'en-GB', 'de-DE'], timezone: ['UTC', 'America/New_York', 'Europe/London', 'Asia/Tokyo'], hourFormat: ['24h', '12h'] })[key].map((value) => <option key={value}>{value}</option>)}</select>
            </label>)}{preferenceError && <p role="alert">{preferenceError}</p>}
          </fieldset>
          {state.selected && <section aria-label="Selected native view">
            <p>Recipe revision {state.selected.revision}{state.historical ? ' · Historical recipe' : ''}. Views query live data; this is not an immutable evidence snapshot.</p>
            {!state.historical && <button disabled={pending || !state.datasets.some((item) => referenceKey(item.reference) === referenceKey(state.selected.element.dataset_ref))} onClick={editSelected}>Edit current recipe</button>}
            <form onSubmit={(event) => { event.preventDefault(); operation('Loading historical recipe…', async (ctx) => {
              const record = await client.getElement(projectId, state.selected.element.id, { revision: Number(revision), signal: ctx.controller.signal })
              if (current(ctx)) { setForm(blankForm()); await readView(record, true, ctx) }
            }) }}><label>Earlier recipe revision<input type="number" min="1" step="1" required value={revision} onChange={(event) => setRevision(event.target.value)} /></label><button disabled={pending}>Read revision</button></form>
            {(state.read || pending) && <DatasetElement element={state.selected.element} read={state.read} preferences={preferences} isLoading={pending}
              onRetry={() => operation('Loading view…', (ctx) => readView(state.selected, state.historical, ctx))}
              onLoadMore={(cursor) => operation('Loading view data…', (ctx) => readView(state.selected, state.historical, ctx, cursor))} />}
            {!state.read && !pending && <button onClick={() => operation('Loading view…', (ctx) => readView(state.selected, state.historical, ctx))}>Retry view</button>}
          </section>}
        </>}
      </>}
    </div>
  </section>
}
