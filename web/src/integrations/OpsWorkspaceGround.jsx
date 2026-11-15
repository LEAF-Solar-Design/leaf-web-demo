import { useLayoutEffect, useMemo, useRef, useState } from 'react'
import { DatasetElement } from '../../../vendor/assistant-react/src/datasets/DatasetElement'
import { IntegrationActionForm } from '../../../vendor/assistant-react/src/actions/IntegrationActionForm'
import { IntegrationActionPreflightError } from '../../../vendor/assistant-react/src/actions'
import { DisplayPreferencesSchema } from '../../../vendor/assistant-react/src/datasets/formatting'
import { DatasetElementV1, DatasetQueryV1 } from '../../../vendor/assistant-contract/src/integration'
import { actionRunMatches, canonicalRecordsPointer, recordsAtOriginal, createIntegrationsClient, UUID } from './client.js'
import { listProjectReferences, readProjectReferenceOriginal } from '../projects/api.js'
import { IntegrationSetupPanel } from './IntegrationSetupPanel.jsx'
import { DatasetDerivationPanel, DatasetDerivationEvidence, isImportedMaterialDataset } from './DatasetDerivationPanel.jsx'
import { ManualWorkflowRunPanel } from './ManualWorkflowRunPanel.jsx'
import { ManualWorkflowStartPanel } from './ManualWorkflowStartPanel.jsx'
import { ManualWorkflowDefinitionPanel } from './ManualWorkflowDefinitionPanel.jsx'
import { ActionDefinitionPanel } from './ActionDefinitionPanel.jsx'
import { WorkflowSchedulePreviewPanel } from './WorkflowSchedulePreviewPanel.jsx'
import { WebhookReceiptPanel } from './WebhookReceiptPanel.jsx'
import { WorkflowScheduleDraftBrowser } from './WorkflowScheduleDraftBrowser.jsx'
import OpsHomePanel from './OpsHomePanel.jsx'
import { useSurfaceFrame } from '../site/SurfaceFrame.jsx'
import { NO_OCCLUDERS, useGroundWindow, windowStyle } from '../site/groundWindow.js'
import { STUDIO_DRAWING_OCCLUDERS } from '../site/drawingOccluders.js'
import './ops-workspace.css'
import { humanizeError } from '../errorHumanize.js'

const STUDIO_CHROME_OCCLUDERS = Object.freeze(STUDIO_DRAWING_OCCLUDERS.filter(([selector]) =>
  ['header.top', '#drafting-ribbon', '.bar.bar-command-line', 'footer.foot-bar'].includes(selector)))

const empty = () => ({ scope: null, datasets: [], elements: [], datasetCursor: null, elementCursor: null, selected: null, read: null, readIdentity: null, previousRead: false, action: null, historical: false, pending: '', error: '', errorOperation: null, notice: '' })
const blankForm = () => ({ dataset: '', id: '', title: '', kind: 'table', columns: [], x: '', y: '', editing: null, window: '', query: null, grouping: false, pairKey: '', providerField: '', firstProvider: '', secondProvider: '' })
const xKinds = ['date', 'instant', 'number', 'count']
const yKinds = ['number', 'count', 'money', 'ratio', 'duration']
const referenceKey = (ref) => JSON.stringify([ref.id, ref.version, ref.digest])
const namedWindows = (dataset) => dataset?.derivation?.plan.kind === 'group_count' &&
  dataset.fields.some(field => field.id === 'window_id' && field.type.kind === 'identifier') ? dataset.derivation.plan.windows : []
export function scopeCacheIdentity(scope, generation) {
  return JSON.stringify([scope.tenantId, scope.projectId, scope.platformUserId, scope.bindingId, scope.projectRole, scope.externalAuthority, scope.externalSubject, generation])
}

function ProjectMaterialImport({ projectId, client, signal, guard, onImported, onForbidden }) {
  const [references, setReferences] = useState([])
  const [cursor, setCursor] = useState(null)
  const [source, setSource] = useState(null)
  const [arrays, setArrays] = useState([])
  const [pointer, setPointer] = useState(null)
  const [fields, setFields] = useState([])
  const [id, setId] = useState('')
  const [title, setTitle] = useState('')
  const [version, setVersion] = useState(1)
  const [pending, setPending] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [contextBytes, setContextBytes] = useState(null)
  const request = useRef(null)
  useLayoutEffect(() => {
    const abort = () => request.current?.abort()
    signal.addEventListener('abort', abort)
    return () => { signal.removeEventListener('abort', abort); abort() }
  }, [signal])
  const alive = (controller) => request.current === controller && !controller.signal.aborted && !signal.aborted
  async function run(action) {
    request.current?.abort()
    const controller = new AbortController()
    request.current = controller
    setPending(true); setError(''); setNotice('')
    try {
      await guard(controller.signal)
      if (alive(controller)) await action(controller)
    } catch (failure) {
      if (!alive(controller) || failure?.name === 'AbortError') return
      if (failure?.status === 403) onForbidden()
      else setError(failure.message || 'The original is unavailable. Nothing was imported.')
    } finally { if (alive(controller)) setPending(false) }
  }
  function loadReferences(after) {
    run(async (controller) => {
      const page = await listProjectReferences(projectId, { signal: controller.signal, cursor: after })
      await guard(controller.signal)
      if (!alive(controller)) return
      if (!Array.isArray(page.materials) || (page.next_cursor !== null && !UUID.test(page.next_cursor || ''))) throw new Error('Project originals are unavailable.')
      const eligible = page.materials.filter((item) => item.project_id === projectId && item.media_type === 'application/json' &&
        UUID.test(item.material_id) && /^[0-9a-f]{64}(?![\s\S])/.test(item.sha256) && Number.isInteger(item.byte_size) && item.byte_size > 0 && item.byte_size <= 8388608)
      setReferences((old) => after ? [...old, ...eligible] : eligible); setCursor(page.next_cursor)
      if (!eligible.length) setNotice('No eligible JSON originals on this page. JSON originals must be at most 8 MiB.')
    })
  }
  function chooseOriginal(materialId) {
    setSource(null); setArrays([]); setPointer(null); setFields([]); setContextBytes(null)
    const material = references.find((item) => item.material_id === materialId)
    if (!material) { request.current?.abort(); setPending(false); return }
    run(async (controller) => {
      const blob = await readProjectReferenceOriginal(projectId, materialId, { signal: controller.signal })
      if (blob.size !== material.byte_size || blob.size > 8388608) throw new Error('The original byte size changed. Nothing was imported.')
      if (!globalThis.crypto?.subtle) throw new Error('Original digest verification is unavailable in this browser.')
      const bytes = await blob.arrayBuffer()
      const digest = Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', bytes)), (byte) => byte.toString(16).padStart(2, '0')).join('')
      if (digest !== material.sha256) throw new Error('The original digest changed. Nothing was imported.')
      let original
      try { original = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(bytes)) }
      catch { throw new Error('The original must contain valid UTF-8 JSON.') }
      const found = []
      const stack = [{ value: original, path: '', depth: 0 }]
      let arrayListBytes = 2
      let limited = false
      for (let position = 0; position < stack.length; position += 1) {
        const { value, path, depth } = stack[position]
        if (Array.isArray(value)) {
          const entry = { pointer: path, count: value.length }
          arrayListBytes += new TextEncoder().encode(JSON.stringify(entry)).byteLength + 1
          if (arrayListBytes > 262144) { limited = true; break }
          found.push(entry)
        }
        if (value && typeof value === 'object' && depth < 32) {
          for (const key of Object.keys(value)) {
            const child = `${path}/${key.replace(/~/g, '~0').replace(/\//g, '~1')}`
            try { canonicalRecordsPointer(child) } catch { continue }
            if (value[key] && typeof value[key] === 'object') stack.push({ value: value[key], path: child, depth: depth + 1 })
          }
        }
      }
      await guard(controller.signal)
      if (!alive(controller)) return
      setSource({ material, original }); setArrays(found)
      if (limited) setNotice('Additional array paths are unavailable because the array list is too large. The original is retained; choose one of the listed arrays.')
      if (!found.length) setError('No safe record arrays are available in this original.')
    })
  }
  function selectArray(value) {
    setPointer(value); setFields([]); setError(''); setContextBytes(null)
    if (value === null || !source) return
    const rows = recordsAtOriginal(source.original, value)
    if (!Array.isArray(rows)) return
    if (value) {
      try {
        const envelope = JSON.parse(JSON.stringify(source.original))
        const split = value.lastIndexOf('/')
        const parent = recordsAtOriginal(envelope, value.slice(0, split))
        const key = value.slice(split + 1).replace(/~1/g, '/').replace(/~0/g, '~')
        parent[key] = { schema: 'leaf.selected-records-reference.v1', records_pointer: value, row_count: rows.length }
        const context = { schema: 'leaf.project-material-context.v1', source: { kind: 'project_material', material_id: source.material.material_id,
          sha256: source.material.sha256, byte_length: source.material.byte_size }, selection: { records_pointer: value }, row_count: rows.length, envelope }
        const size = new TextEncoder().encode(JSON.stringify(context)).byteLength
        setContextBytes(size)
        if (size > 262144) { setPointer(null); setError('Complete material context is too large to display. The original and last valid dataset are retained.'); return }
      } catch { setPointer(null); setError('Complete material context is unavailable. The original and last valid dataset are retained.'); return }
    }
    const first = rows.find((row) => row && typeof row === 'object' && !Array.isArray(row))
    setFields(Object.keys(first || {}).filter((key) => /^[A-Za-z_][A-Za-z0-9_]{0,127}$/.test(key) &&
      !['__proto__', 'prototype', 'constructor'].includes(key)).slice(0, 100).map((key, index) => ({
      id: key, label: key, pointer: `/${key}`, kind: 'text', conversion: 'identity', key: index === 0, nullable: false,
    })))
  }
  function updateField(index, key, value) { setFields((old) => old.map((field, position) => position === index ? { ...field, [key]: value } : field)) }
  function submit(event) {
    event.preventDefault()
    run(async (controller) => {
      if (!source || pointer === null || !Array.isArray(recordsAtOriginal(source.original, pointer))) throw new Error('Choose an original and records array.')
      const definition = { schema_version: 1, id, version: Number(version), title, reporting_time_zone: 'UTC', retention_days: 365,
        fields: fields.map((field) => ({ id: field.id, label: field.label,
          type: field.kind === 'ratio' ? { kind: 'ratio', scale: field.ratioScale || 'percent' } : { kind: field.kind },
          nullable: field.nullable, sensitivity: 'internal' })),
        key_fields: fields.filter((field) => field.key).map((field) => field.id),
        lineage: { kind: 'project_material', material_id: source.material.material_id, sha256: source.material.sha256, byte_length: source.material.byte_size } }
      const mapping = { schema_version: 1, fields: fields.map((field) => ({ field_id: field.id, pointer: field.pointer, conversion: field.conversion })) }
      const result = await client.importProjectMaterial(projectId, { definition, mapping, ...(pointer ? { selection: { records_pointer: pointer } } : {}) }, { signal: controller.signal })
      await guard(controller.signal)
      if (!alive(controller)) return
      await onImported(result.reference, controller.signal)
      if (alive(controller)) setNotice(`Imported ${result.rowCount} records. Original bytes and source clocks are retained.`)
    })
  }
  return <section aria-label="Import project original"><h2>Import project original</h2>
    <button disabled={pending} onClick={() => loadReferences()}>List JSON originals</button>
    {cursor && <button disabled={pending} onClick={() => loadReferences(cursor)}>More originals</button>}
    <label>Project original<select defaultValue="" onChange={(event) => chooseOriginal(event.target.value)}>
      <option value="">Select original</option>{references.map((item) => <option key={item.material_id} value={item.material_id}>{item.name}</option>)}
    </select></label>
    {pending && <p role="status">Reading or importing original…</p>}{error && <p role="alert">{error}</p>}{notice && <p role="status">{notice}</p>}
    {source && <><pre aria-label="Original provenance">{JSON.stringify(source.material, null, 2)}</pre>
      {contextBytes !== null && <p>Complete material context: {contextBytes} UTF-8 bytes. Source capture and ingestion clocks remain unchanged.</p>}
      <label>Records array<select value={pointer === null ? 'unselected' : pointer} disabled={pending} onChange={(event) => selectArray(event.target.value === 'unselected' ? null : event.target.value)}>
        <option value="unselected">Select records array</option>{arrays.map((item) => <option key={item.pointer} value={item.pointer}>{item.pointer || '(root)'} · {item.count} records</option>)}
      </select></label>
      <form aria-label="Map project records" onSubmit={submit}><fieldset disabled={pending || pointer === null}>
        <label>Import dataset ID<input required value={id} onChange={(event) => setId(event.target.value)} /></label>
        <label>Import dataset title<input required value={title} onChange={(event) => setTitle(event.target.value)} /></label>
        <label>Import dataset version<input required type="number" min="1" value={version} onChange={(event) => setVersion(event.target.value)} /></label>
        {fields.map((field, index) => <fieldset key={index}><legend>Import field {index + 1}</legend>
          {['id', 'label', 'pointer'].map((key) => <label key={key}>{`Field ${index + 1} ${key}`}<input required value={field[key]} onChange={(event) => updateField(index, key, event.target.value)} /></label>)}
          <label>{`Field ${index + 1} type`}<select value={field.kind} onChange={(event) => updateField(index, 'kind', event.target.value)}>{['text', 'identifier', 'count', 'number', 'ratio', 'boolean', 'date', 'instant'].map((kind) => <option key={kind}>{kind}</option>)}</select></label>
          {field.kind === 'ratio' && <label>{`Field ${index + 1} ratio scale`}<select value={field.ratioScale || 'percent'} onChange={(event) => updateField(index, 'ratioScale', event.target.value)}>
            <option value="percent">Percent (80 means 80%)</option><option value="fraction">Fraction (0.8 means 80%)</option>
          </select></label>}
          <label>{`Field ${index + 1} conversion`}<select value={field.conversion} onChange={(event) => updateField(index, 'conversion', event.target.value)}>{['identity', 'string_number', 'iso_instant'].map((kind) => <option key={kind}>{kind}</option>)}</select></label>
          <label><input type="checkbox" checked={field.key} onChange={(event) => updateField(index, 'key', event.target.checked)} />{`Field ${index + 1} key`}</label>
          <label><input type="checkbox" checked={field.nullable} onChange={(event) => updateField(index, 'nullable', event.target.checked)} />{`Field ${index + 1} nullable`}</label>
          <button type="button" onClick={() => setFields((old) => old.filter((_, position) => position !== index))}>Remove field {index + 1}</button>
        </fieldset>)}
        <button type="button" disabled={fields.length >= 100} onClick={() => setFields((old) => [...old, { id: '', label: '', pointer: '', kind: 'text', conversion: 'identity', key: false, nullable: false }])}>Add import field</button>
        <button type="submit">Import selected records</button>
      </fieldset></form></>}
  </section>
}

export function OpsWorkspaceGround({ active = false, projectId = null, projectLabel, session, signedIn = false, accountScope, mock = false,
  leavingGround = null, studioShell = false, occluders = NO_OCCLUDERS, client: suppliedClient, mode }) {
  const devices = useSurfaceFrame()?.devices
  const hasLinkedDevice = Array.isArray(devices?.devices) && devices.devices.length > 0
  const client = useMemo(() => suppliedClient || createIntegrationsClient(), [suppliedClient])
  const [state, setState] = useState(empty)
  const [form, setForm] = useState(blankForm)
  const [epoch, setEpoch] = useState(0)
  const [retry, setRetry] = useState(0)
  const [revision, setRevision] = useState('')
  const [preferences, setPreferences] = useState({ locale: 'en-US', timezone: 'UTC', hourFormat: '24h' })
  const [preferenceError, setPreferenceError] = useState('')
  const [handoff, setHandoff] = useState(null)
  const boardRef = useRef(null)
  const resultRef = useRef(null)
  const composeFeedback = useRef(null)
  const [composeError, setComposeError] = useState('')
  const [savedReadback, setSavedReadback] = useState(null)
  useLayoutEffect(() => { if (composeError || savedReadback) composeFeedback.current?.focus() }, [composeError, savedReadback])
  const generation = useRef(0)
  const context = useRef(null)
  const viewGeneration = useRef(0)
  // Keep only dataset selection intent while a profile hides this Ground.
  // Results and action authority are always discarded and read again.
  const rememberedView = useRef(null)
  const busy = useRef(false)
  const snapshot = session?.controller?.getSnapshot?.() || session
  const sessionGeneration = snapshot?.generation
  const status = snapshot?.status
  const eligible = active && signedIn && !mock && UUID.test(projectId || '') && status === 'active'
  // Comparing the render identity prevents a single frame of old-account content.
  const identityKey = JSON.stringify([projectId, accountScope, signedIn, mock, sessionGeneration, status, active, epoch])
  const resumeOwner = JSON.stringify([projectId, accountScope, signedIn, mock, sessionGeneration, status, epoch])
  const identity = useMemo(() => ({ key: identityKey }), [identityKey, session?.controller])
  const visible = state.identity === identity && eligible
  const leaving = leavingGround === 'integrations'
  // These shell cards can float over the Ground at tablet widths.
  const shellOccluders = useMemo(() => [
    ...occluders, ['aside.nav', 'nearest'], ['.studio-profile-info', 'nearest'],
    ...(studioShell ? STUDIO_CHROME_OCCLUDERS : NO_OCCLUDERS),
  ], [occluders, studioShell])
  const win = useGroundWindow(active || leaving, studioShell, boardRef, shellOccluders)

  function invalidate() {
    setComposeError('')
    setSavedReadback(null)
    setHandoff(null)
    generation.current += 1
    context.current?.action?.controller.abort()
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
  function fail(error, ctx, errorOperation = null) {
    if (!current(ctx) || error?.name === 'AbortError') return
    if (error?.status === 403) {
      rememberedView.current = null
      invalidate()
      setState({ ...empty(), identity, error: 'You do not have permission to view these integrations.' })
      setForm(blankForm())
      setRevision('')
    } else setState((old) => ({ ...old, ...(error?.status === 401 ? { read: null, readIdentity: null, previousRead: false } : {}),
      pending: '', error: humanizeError(error), errorOperation, retryable: error?.retryable !== false && ![401, 403, 404, 409, 422].includes(error?.status) }))
  }
  useLayoutEffect(() => {
    if (rememberedView.current?.owner !== resumeOwner) rememberedView.current = null
    invalidate()
    setState({ ...empty(), identity, pending: eligible ? 'Loading integrations…' : '' })
    setForm(blankForm())
    setRevision('')
    if (!eligible) return undefined
    const ctx = { controller: new AbortController(), generation: generation.current, sessionGeneration, resumeOwner }
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
        const resume = rememberedView.current
        rememberedView.current = null
        if (resume?.owner === ctx.resumeOwner && resume.scope === ctx.cacheIdentity) {
          const saved = elements.records.find(record => record.element.id === resume.id && record.element.kind.startsWith('dataset.'))
          if (saved) {
            setState(old => ({ ...old, pending: 'Loading view…' }))
            await readView(saved, false, ctx)
            if (current(ctx)) setState(old => ({ ...old, pending: '' }))
          }
        }
      } catch (error) { fail(error, ctx) }
    })()
    return invalidate
  }, [identity, retry, client])

  async function operation(label, action, composing = false, owner = 'integrations') {
    const ctx = context.current
    if (!ctx || !current(ctx) || busy.current) return
    busy.current = true
    setState((old) => ({ ...old, pending: label, error: '', errorOperation: null, notice: '' }))
    if (composing) setComposeError('')
    try { await action(ctx) } catch (error) {
      if (composing && current(ctx) && error?.name !== 'AbortError' && ![401, 403].includes(error?.status)) setComposeError(humanizeError(error))
      else fail(error, ctx, { owner, retry: () => operation(label, action, composing, owner) })
    }
    finally {
      if (current(ctx)) {
        busy.current = false
        setState((old) => ({ ...old, pending: '' }))
      }
    }
  }
  async function guardSetup(signal) {
    const ctx = context.current
    if (!ctx || !current(ctx) || signal.aborted) throw new DOMException('Aborted', 'AbortError')
    const scope = await client.getScope(projectId, { signal })
    if (!current(ctx) || signal.aborted) throw new DOMException('Aborted', 'AbortError')
    if (scope.projectRole !== 'owner' || scopeCacheIdentity(scope, ctx.sessionGeneration) !== ctx.cacheIdentity) {
      fail({ status: 403 }, ctx)
      throw new DOMException('Aborted', 'AbortError')
    }
  }
  async function guardWorkflow(signal) {
    const ctx = context.current
    if (!ctx || !current(ctx) || signal.aborted) throw new DOMException('Aborted', 'AbortError')
    const scope = await client.getScope(projectId, { signal })
    if (!current(ctx) || signal.aborted) throw new DOMException('Aborted', 'AbortError')
    if (scope.projectId !== projectId || !['owner', 'editor'].includes(scope.projectRole) || scopeCacheIdentity(scope, ctx.sessionGeneration) !== ctx.cacheIdentity) {
      fail({ status: 403 }, ctx)
      throw new DOMException('Aborted', 'AbortError')
    }
  }
  async function guardSchedulePreview(signal) {
    const ctx = context.current
    if (!ctx || !current(ctx) || signal.aborted) throw new DOMException('Aborted', 'AbortError')
    let scope
    try { scope = await client.getScope(projectId, { signal }) }
    catch (error) {
      // The client also uses status 0 for scope validation failures, not just transport errors.
      if (error?.status === 0 && error?.message === 'The integration response or request was invalid.') {
        fail({ status: 403 }, ctx)
        throw new DOMException('Aborted', 'AbortError')
      }
      throw error
    }
    if (!current(ctx) || signal.aborted) throw new DOMException('Aborted', 'AbortError')
    if (scope.projectId !== projectId || !['owner', 'editor', 'reviewer', 'read_only'].includes(scope.projectRole) ||
        scopeCacheIdentity(scope, ctx.sessionGeneration) !== ctx.cacheIdentity) {
      fail({ status: 403 }, ctx)
      throw new DOMException('Aborted', 'AbortError')
    }
  }
  async function refreshSetupDataset(reference, signal) {
    const ctx = context.current
    if (!ctx || !current(ctx) || signal.aborted) return
    try {
      const dataset = await client.getDataset(projectId, reference, { signal })
      if (!current(ctx) || signal.aborted) return
      setState((old) => ({ ...old, datasets: [...old.datasets.filter((item) => referenceKey(item.reference) !== referenceKey(reference)), dataset],
        notice: `Dataset ${dataset.title} is available in the native composer.` }))
      setForm({ ...blankForm(), dataset: referenceKey(reference), columns: dataset.fields.map((field) => field.id) })
    } catch (error) { fail(error, ctx); throw error }
  }
  async function adoptDerivedDataset(reference, signal) {
    const ctx = context.current
    if (!ctx || !current(ctx) || signal.aborted) return
    const dataset = await client.getDataset(projectId, reference, { signal })
    await guardWorkflow(signal)
    if (!current(ctx) || signal.aborted) return
    setState(old => ({ ...old, datasets: [...old.datasets.filter(item => referenceKey(item.reference) !== referenceKey(reference)), dataset] }))
    // A completed materialization never overwrites an in-progress view draft.
    setForm(old => old.dataset || old.id || old.title || old.editing ? old
      : { ...old, dataset: referenceKey(reference), columns: dataset.fields.map(field => field.id) })
  }
  async function readView(record, historical, ctx, cursor) {
    rememberedView.current = !historical && record.element.kind.startsWith('dataset.')
      ? { owner: ctx.resumeOwner, scope: ctx.cacheIdentity, id: record.element.id } : null
    const request = ++viewGeneration.current
    const readIdentity = JSON.stringify([ctx.cacheIdentity, record.revision, record.element])
    const retain = state.readIdentity === readIdentity && state.read && ['ready', 'empty', 'partial', 'stale'].includes(state.read.status)
    ctx.action?.controller.abort()
    ctx.action = null
    setState((old) => ({ ...old, selected: record, historical, read: retain ? old.read : null, readIdentity, previousRead: Boolean(retain), action: null }))
    if (record.element.kind === 'integration.action') {
      if (historical || !['owner', 'editor'].includes(state.scope?.projectRole)) return
      const binding = { record, ctx, request, controller: new AbortController(), grantRevision: null, availability: 'unavailable' }
      ctx.action = binding
      setState((old) => ({ ...old, action: { binding, availability: 'unavailable' } }))
      await refreshAction(binding)
      return
    }
    if (retain) {
      await guardSchedulePreview(ctx.controller.signal)
      if (!current(ctx) || request !== viewGeneration.current) return
    }
    const read = await client.queryDataset(projectId, record.element.dataset_ref,
      DatasetQueryV1.parse({ ...record.element.query, ...(cursor ? { cursor } : {}) }), { signal: ctx.controller.signal })
    if (!current(ctx) || request !== viewGeneration.current) return
    if (read.status === 'forbidden') { fail({ status: 403 }, ctx); return }
    if (retain && ['failed', 'unavailable', 'invalid'].includes(read.status)) throw new Error('The dataset could not be loaded.')
    setState((old) => ({ ...old, read, previousRead: false, error: '', errorOperation: null }))
  }
  function loadView(record, historical, cursor) {
    operation(cursor ? 'Loading view data…' : 'Loading view…', (ctx) => readView(record, historical, ctx, cursor), false, 'view')
  }
  function retryView() {
    if (state.errorOperation?.owner === 'view') state.errorOperation.retry()
    else loadView(state.selected, state.historical)
  }
  function loadHistoricalRevision(selected, requestedRevision) {
    operation('Loading historical recipe…', async (ctx) => {
      setState((old) => ({ ...old, ...(requestedRevision !== selected.revision
        ? { read: null, readIdentity: null, previousRead: false }
        : { previousRead: Boolean(old.read && ['ready', 'empty', 'partial', 'stale'].includes(old.read.status)) }) }))
      const record = await client.getElement(projectId, selected.element.id, { revision: requestedRevision, signal: ctx.controller.signal })
      if (current(ctx)) { setForm(blankForm()); await readView(record, true, ctx) }
    }, false, 'view')
  }
  const currentAction = (binding) => current(binding.ctx) && binding.ctx.action === binding &&
    viewGeneration.current === binding.request && !binding.controller.signal.aborted
  function actionAvailability(binding, availability) {
    if (!currentAction(binding)) return
    binding.availability = availability
    setState((old) => ({ ...old, action: { binding, availability } }))
  }
  function checkConnection(binding, connection) {
    const element = binding.record.element
    if (connection.id !== element.connection_id || connection.state !== 'active' ||
        !connection.operation_ids?.includes(element.operation_ref.id)) return 'forbidden'
    if (referenceKey(connection.connector_ref) !== referenceKey(element.connector_ref) ||
        !Number.isSafeInteger(connection.grant_revision) || connection.grant_revision <= 0 ||
        (binding.grantRevision !== null && connection.grant_revision !== binding.grantRevision)) return 'stale'
    return 'ready'
  }
  async function refreshAction(binding) {
    try {
      if (!currentAction(binding)) return
      if (binding.grantRevision !== null) {
        const scope = await client.getScope(projectId, { signal: binding.controller.signal })
        if (!currentAction(binding)) return
        if (scopeCacheIdentity(scope, binding.ctx.sessionGeneration) !== binding.ctx.cacheIdentity) {
          actionAvailability(binding, 'stale')
          return
        }
      }
      const connection = await client.getConnection(projectId, binding.record.element.connection_id, { signal: binding.controller.signal })
      if (!currentAction(binding)) return
      const availability = checkConnection(binding, connection)
      if (availability === 'ready' && binding.grantRevision === null) binding.grantRevision = connection.grant_revision
      actionAvailability(binding, availability)
    } catch (error) {
      actionAvailability(binding, error?.status === 403 ? 'forbidden' : 'unavailable')
    }
  }
  async function actionCallback(binding, callerSignal, mode, submission, runId) {
    let submissionStarted = false
    const controller = new AbortController()
    const abort = () => controller.abort()
    const signals = [callerSignal, binding.controller.signal, binding.ctx.controller.signal]
    signals.forEach((signal) => signal.addEventListener('abort', abort, { once: true }))
    const assertCurrent = () => {
      if (signals.some((signal) => signal.aborted) || controller.signal.aborted || !currentAction(binding)) {
        controller.abort()
        throw new DOMException('Aborted', 'AbortError')
      }
    }
    try {
      assertCurrent()
      if (binding.availability !== 'ready') throw new Error('This action is unavailable.')
      const scope = await client.getScope(projectId, { signal: controller.signal })
      assertCurrent()
      if (scopeCacheIdentity(scope, binding.ctx.sessionGeneration) !== binding.ctx.cacheIdentity || !['owner', 'editor'].includes(scope.projectRole)) {
        actionAvailability(binding, 'stale')
        throw new Error('This action has changed. Refresh it before continuing.')
      }
      const connection = await client.getConnection(projectId, binding.record.element.connection_id, { signal: controller.signal })
      assertCurrent()
      const availability = checkConnection(binding, connection)
      if (availability !== 'ready') {
        actionAvailability(binding, availability)
        throw new Error('This action has changed. Refresh it before continuing.')
      }
      const expected = { ...binding.record.element, expected_grant_revision: binding.grantRevision }
      if (submission && !actionRunMatches({ ...submission, grant_revision: submission.expected_grant_revision }, expected)) {
        actionAvailability(binding, 'stale')
        throw new Error('This action has changed. Refresh it before continuing.')
      }
      let result
      if (mode === 'reconcile') result = await client.reconcileAction(projectId, runId, { signal: controller.signal })
      else if (mode === 'status') result = await client.getAction(projectId, runId, { signal: controller.signal })
      else {
        submissionStarted = true
        result = await client.submitAction(projectId, submission, { signal: controller.signal })
      }
      assertCurrent()
      if (result.run && !actionRunMatches(result.run, expected)) throw new Error('The integration response or request was invalid.')
      return result
    } catch (error) {
      if (error?.status === 403) actionAvailability(binding, 'forbidden')
      if (mode === 'submit' && submission && !submissionStarted && error?.name !== 'AbortError') throw new IntegrationActionPreflightError()
      throw error
    } finally { signals.forEach((signal) => signal.removeEventListener('abort', abort)) }
  }
  function chooseSaved(record) {
    operation('Loading saved view…', async (ctx) => {
      setRevision('')
      setForm(blankForm())
      await readView(record, false, ctx)
    }, false, 'view')
  }
  function compose(event) {
    event.preventDefault()
    if (savedReadback) return
    operation('Saving view…', async (ctx) => {
      const dataset = state.datasets.find((item) => referenceKey(item.reference) === form.dataset)
      if (!dataset) throw new Error('Select an authorized dataset.')
      const fields = form.kind === 'table' ? form.columns : [...new Set([form.x, form.y])]
      if (fields.some((id) => !dataset.fields.some((field) => field.id === id))) throw new Error('Select authorized fields.')
      if (form.kind === 'line' && (!xKinds.includes(dataset.fields.find((field) => field.id === form.x)?.type.kind) ||
        !yKinds.includes(dataset.fields.find((field) => field.id === form.y)?.type.kind))) throw new Error('Choose compatible line axes.')
      const grouping = form.kind === 'table' && form.grouping ? {
        kind: 'provider_pairs', pair_key_field: form.pairKey, provider_field: form.providerField,
        providers: [form.firstProvider, form.secondProvider],
      } : undefined
      if (grouping && [form.pairKey, form.providerField].some(id => !fields.includes(id) ||
        !['text', 'identifier'].includes(dataset.fields.find(field => field.id === id)?.type.kind))) {
        throw new Error('Choose visible text or identifier columns for pair and provider fields.')
      }
      const windows = namedWindows(dataset)
      if (form.window && !windows.some(window => window.id === form.window)) throw new Error('Select a declared window.')
      const query = dataset.derivation ? { ...(form.query || { limit: 100 }), fields } : { fields, limit: 100 }
      if (windows.length) {
        const retained = (query.filters || []).filter(filter => !(filter.field === 'window_id' && filter.op === 'eq' && windows.some(window => window.id === filter.value)))
        query.filters = [...retained, ...(form.window ? [{ field: 'window_id', op: 'eq', value: form.window }] : [])]
        // Cursors are bound to the previous projection/filter, never a new selection.
        delete query.cursor
      }
      const parsed = DatasetElementV1.safeParse({ schema_version: 1, id: form.id, title: form.title.trim(),
        dataset_ref: dataset.reference, query, kind: `dataset.${form.kind}`,
        props: form.kind === 'table' ? { columns: fields, row_limit: 100, ...(grouping ? { row_grouping: grouping } : {}) } : { x_field: form.x, y_field: form.y } })
      if (!parsed.success) throw new Error('Enter a title, a stable lowercase slug ID, and valid fields.')
      if (!form.editing && state.elements.some((record) => record.element.id === form.id)) throw new Error('Select that saved view and choose Edit current recipe to update it.')
      const saved = await client.saveElement(projectId, parsed.data, { signal: ctx.controller.signal,
        ...(form.editing ? { expectedRevision: form.editing.revision } : {}) })
      if (!current(ctx)) return
      setState((old) => ({ ...old, notice: `Saved ${saved.element.title}, revision ${saved.revision}.`,
        elements: [...old.elements.filter((record) => record.element.id !== saved.element.id), saved] }))
      setForm((old) => ({ ...old, editing: saved }))
      resultRef.current?.focus()
      await readSavedView(saved, ctx)
    }, true)
  }
  async function readSavedView(saved, ctx) {
    try {
      await readView(saved, false, ctx)
      if (current(ctx)) setSavedReadback(null)
    } catch (error) {
      if (!current(ctx) || error?.name === 'AbortError') return
      if ([401, 403].includes(error?.status)) { fail(error, ctx); return }
      setSavedReadback({ record: saved, error: humanizeError(error) })
    }
  }
  function editSelected() {
    const saved = state.selected
    const element = saved.element
    const dataset = state.datasets.find(item => referenceKey(item.reference) === referenceKey(element.dataset_ref))
    const windows = namedWindows(dataset)
    setForm({ dataset: referenceKey(element.dataset_ref), id: element.id, title: element.title, kind: element.kind === 'dataset.table' ? 'table' : 'line',
      columns: element.props.columns || [], x: element.props.x_field || '', y: element.props.y_field || '', editing: saved,
      grouping: Boolean(element.props.row_grouping), pairKey: element.props.row_grouping?.pair_key_field || '',
      providerField: element.props.row_grouping?.provider_field || '', firstProvider: element.props.row_grouping?.providers[0] || '',
      secondProvider: element.props.row_grouping?.providers[1] || '', query: element.query, window: element.query.filters?.find(filter => filter.field === 'window_id' && filter.op === 'eq' && windows.some(window => window.id === filter.value))?.value || '' })
  }
  function loadMore(resource) {
    operation('Loading more…', async (ctx) => {
      const isDataset = resource === 'datasets'
      const page = await client[isDataset ? 'listDatasets' : 'listElements'](projectId, { signal: ctx.controller.signal,
        after: isDataset ? state.datasetCursor : state.elementCursor })
      if (current(ctx)) setState((old) => {
        let records = [...old[resource], ...page.records]
        if (!isDataset) {
          const byId = new Map()
          for (const record of records) {
            const previous = byId.get(record.element.id)
            if (!previous || record.revision > previous.revision) byId.set(record.element.id, record)
          }
          records = [...byId.values()]
        }
        return { ...old, [resource]: records, [isDataset ? 'datasetCursor' : 'elementCursor']: page.nextCursor }
      })
    })
  }
  const dataset = state.datasets.find((item) => referenceKey(item.reference) === form.dataset)
  const pending = Boolean(state.pending)
  const viewError = state.error && state.read && state.previousRead && state.errorOperation?.owner === 'view'
  const message = mock ? 'Integrations are unavailable in demo mode.' : !signedIn ? 'Sign in to view integrations.' :
    !UUID.test(projectId || '') ? 'Open a project to view integrations.' : status !== 'active' ? 'An active session is required to view integrations.' : null
  return <section ref={boardRef} className="studio-ground-ops" data-ground="ops" hidden={!active && !leaving}
    data-studio-shell={studioShell ? 'cockpit' : undefined} aria-label="Ops workspace" aria-hidden={leaving ? 'true' : undefined}
    inert={leaving ? '' : undefined} data-ground-phase={leaving ? 'leaving' : active && leavingGround ? 'entering' : undefined}>
    <div className="ops-workspace" style={windowStyle(win)}>
      <h1>Ops workspace</h1><p>{projectLabel?.trim() || (projectId ? 'Project name unavailable' : 'No project open')}</p>
      {message ? <p role="status">{message}</p> : visible && <>
        <OpsHomePanel active={active} />
        <p role="status" aria-live="polite" tabIndex={-1} ref={resultRef}>{state.notice || state.pending}</p>
        {state.error && !viewError && <div role="alert"><p>{humanizeError(state.error)}</p>{state.retryable && <button disabled={pending} onClick={() => state.errorOperation ? state.errorOperation.retry() : setRetry((value) => value + 1)}>Retry integrations</button>}</div>}
        {state.scope && <details className="ops-advanced" open={!hasLinkedDevice}>
          <summary>Workflows and data</summary>
          {['owner', 'editor'].includes(state.scope.projectRole) && context.current && <WorkflowScheduleDraftBrowser
            key={JSON.stringify(['schedule-drafts', identity.key, context.current.cacheIdentity])}
            contextKey={JSON.stringify([identity.key, context.current.cacheIdentity])} client={client} projectId={projectId}
            signal={context.current.controller.signal} guard={guardWorkflow} mode={mode === 'light' || mode === 'dark' ? mode : undefined}
            onForbidden={() => { const ctx = context.current; if (ctx) fail({ status: 403 }, ctx) }} />}
          {['owner', 'editor'].includes(state.scope.projectRole) && context.current && <WebhookReceiptPanel
            key={JSON.stringify(['webhook-receipt', identity.key, context.current.cacheIdentity])} client={client} projectId={projectId}
            signal={context.current.controller.signal} guard={guardWorkflow}
            onForbidden={() => { const ctx = context.current; if (ctx) fail({ status: 403 }, ctx) }} />}
          {['owner', 'editor'].includes(state.scope.projectRole) && context.current && <ActionDefinitionPanel
            key={JSON.stringify(['action-definition', identity.key, context.current.cacheIdentity])} client={client} projectId={projectId}
            signal={context.current.controller.signal} guard={guardWorkflow}
            onForbidden={() => { const ctx = context.current; if (ctx) fail({ status: 403 }, ctx) }}
            onSaved={(record) => { const ctx = context.current; if (ctx && current(ctx)) setState((old) => ({ ...old,
              elements: [...old.elements.filter((item) => item.element.id !== record.element.id), record],
              notice: `Saved ${record.element.title}, revision ${record.revision}. No action was submitted.` })) }} />}
          {['owner', 'editor', 'reviewer', 'read_only'].includes(state.scope.projectRole) && context.current && <WorkflowSchedulePreviewPanel
            key={JSON.stringify(['schedule-preview', identity.key, context.current.cacheIdentity])} client={client} projectId={projectId}
            signal={context.current.controller.signal} guard={guardSchedulePreview}
            onForbidden={() => { const ctx = context.current; if (ctx) fail({ status: 403 }, ctx) }} />}
          {['owner', 'editor'].includes(state.scope.projectRole) && context.current && <ManualWorkflowDefinitionPanel
            key={JSON.stringify(['manual-workflow-definition', identity.key, context.current.cacheIdentity])} client={client} projectId={projectId}
            signal={context.current.controller.signal} guard={guardWorkflow}
            onForbidden={() => { const ctx = context.current; if (ctx) fail({ status: 403 }, ctx) }} />}
          {['owner', 'editor'].includes(state.scope.projectRole) && context.current && <ManualWorkflowStartPanel
            key={JSON.stringify(['manual-workflow-start', identity.key, context.current.cacheIdentity])} client={client} projectId={projectId}
            signal={context.current.controller.signal} guard={guardWorkflow}
            onForbidden={() => { const ctx = context.current; if (ctx) fail({ status: 403 }, ctx) }}
            onAdmitted={(run) => { const ctx = context.current; if (ctx && current(ctx)) setHandoff({ run, generation: ctx.generation }) }} />}
          {['owner', 'editor'].includes(state.scope.projectRole) && context.current && <ManualWorkflowRunPanel
            key={JSON.stringify(['manual-workflow', identity.key, context.current.cacheIdentity])} client={client} projectId={projectId}
            signal={context.current.controller.signal} guard={guardWorkflow} displayPreferences={preferences}
            requestedRunId={handoff?.generation === context.current.generation ? handoff.run.run_id : undefined}
            admittedBinding={handoff?.generation === context.current.generation ? handoff.run : undefined}
            onForbidden={() => { const ctx = context.current; if (ctx) fail({ status: 403 }, ctx) }} />}
          {state.scope.projectRole === 'owner' && context.current && <IntegrationSetupPanel
            key={JSON.stringify([identity.key, context.current.cacheIdentity])} client={client} projectId={projectId} scope={state.scope}
            signal={context.current.controller.signal} guard={guardSetup}
            onForbidden={() => { const ctx = context.current; if (ctx) fail({ status: 403 }, ctx) }}
            onDatasetReady={refreshSetupDataset} />}
          {state.scope.projectRole === 'owner' && context.current && <ProjectMaterialImport
            key={JSON.stringify(['material-import', identity.key, context.current.cacheIdentity])} projectId={projectId} client={client}
            signal={context.current.controller.signal} guard={guardSetup}
            onForbidden={() => { const ctx = context.current; if (ctx) fail({ status: 403 }, ctx) }}
            onImported={async (_reference, signal) => {
              const ctx = context.current
              const page = await client.listDatasets(projectId, { signal })
              await guardSetup(signal)
              if (ctx && current(ctx) && !signal.aborted) setState((old) => ({ ...old, datasets: page.records, datasetCursor: page.nextCursor }))
            }} />}
          {['owner', 'editor'].includes(state.scope.projectRole) && context.current && state.datasets.some(isImportedMaterialDataset) && <DatasetDerivationPanel
            key={JSON.stringify(['dataset-derivation', identity.key, context.current.cacheIdentity])}
            contextKey={context.current.cacheIdentity} projectId={projectId} client={client} datasets={state.datasets}
            signal={context.current.controller.signal} guard={guardWorkflow} onCreated={adoptDerivedDataset}
            onForbidden={() => { const ctx = context.current; if (ctx) fail({ status: 403 }, ctx) }} />}
          <section aria-label="Integration datasets"><h2>Datasets</h2>
            {!state.datasets.length && <p>{state.scope.projectRole === 'owner'
              ? 'No datasets are available yet. Use integration setup to connect your data.'
              : 'No datasets are available yet. Ask the project owner to connect your data.'}</p>}
            <ul>{state.datasets.map((item) => <li key={`${item.reference.id}:${item.reference.version}`}>{item.title} · version {item.reference.version}
              {item.material_context && <pre aria-label={`Material context for ${item.title}`}>{JSON.stringify(item.material_context, null, 2)}</pre>}</li>)}</ul>
            {state.datasetCursor && <button disabled={pending} onClick={() => loadMore('datasets')}>Load more datasets</button>}
          </section>
          <section aria-label="Saved views"><h2>Saved views</h2>
            {!state.elements.length && <p>No saved views yet.</p>}
            <ul>{state.elements.map((record) => <li key={record.element.id}><button disabled={pending} onClick={() => chooseSaved(record)}>{record.element.title} · revision {record.revision}</button></li>)}</ul>
            {state.elementCursor && <button disabled={pending} onClick={() => loadMore('elements')}>Load more saved views</button>}
          </section>
          <form onSubmit={compose} aria-label="Compose native view"><h2>Compose native view</h2>
            {composeError && <p ref={composeFeedback} tabIndex={-1} role="alert">{composeError} Your draft is retained. Review it and save again.</p>}
            {savedReadback && <div ref={composeFeedback} tabIndex={-1} role="alert">
              <p>Your view was saved, but its data could not be loaded. {savedReadback.error}</p>
              <button type="button" disabled={pending} onClick={() => operation('Loading saved view…', (ctx) => readSavedView(savedReadback.record, ctx))}>Reload view</button>
            </div>}
            <fieldset disabled={pending || Boolean(savedReadback)}><legend>{form.editing ? `Update revision ${form.editing.revision}` : 'Create a view'}</legend>
              <label>Dataset<select required value={form.dataset} onChange={(event) => setForm({ ...blankForm(), dataset: event.target.value })}>
                <option value="">Select dataset</option>{state.datasets.map((item) => <option key={referenceKey(item.reference)} value={referenceKey(item.reference)}>{item.title} · version {item.reference.version}</option>)}
              </select></label>
              <label>Title<input required maxLength={120} value={form.title} onChange={(event) => setForm({ ...form, title: event.target.value })} /></label>
              <label>Stable ID<input required maxLength={128} pattern="[a-z0-9]+((_|-)[a-z0-9]+)*" disabled={Boolean(form.editing)} value={form.id} onChange={(event) => setForm({ ...form, id: event.target.value })} /></label>
              <label>View type<select value={form.kind} onChange={(event) => setForm({ ...form, kind: event.target.value })}><option value="table">Table</option><option value="line">Line</option></select></label>
              {namedWindows(dataset).length > 0 && <label>Named window<select value={form.window} onChange={event => setForm({ ...form, window: event.target.value })}>
                <option value="">All windows</option>{namedWindows(dataset).map(window => <option key={window.id} value={window.id}>{window.id}{window.filters.length === 0 ? ' (all admitted history)' : ''}</option>)}
              </select></label>}
              {form.kind === 'table' ? <fieldset><legend>Table columns</legend>{dataset?.fields.map((field) => <label key={field.id}><input type="checkbox" checked={form.columns.includes(field.id)} onChange={(event) => setForm({ ...form, columns: event.target.checked ? [...form.columns, field.id] : form.columns.filter((id) => id !== field.id) })} />{field.label}</label>)}</fieldset> : <>
                {['x', 'y'].map((axis) => <label key={axis}>{axis.toUpperCase()} field<select required value={form[axis]} onChange={(event) => setForm({ ...form, [axis]: event.target.value })}>
                  <option value="">Select field</option>{dataset?.fields.filter((field) => (axis === 'x' ? xKinds : yKinds).includes(field.type.kind)).map((field) => <option key={field.id} value={field.id}>{field.label}</option>)}
                </select></label>)}
              </>}
              {form.kind === 'table' && <fieldset><legend>Provider pair presentation</legend>
                <label><input type="checkbox" checked={Boolean(form.grouping)} onChange={event => setForm({ ...form, grouping: event.target.checked })} />Group verified provider pairs</label>
                {form.grouping && <>
                  <p>Pair rows only when their exact nonempty key and two provider labels match in a complete result. Other rows stay separate.</p>
                  {[['pairKey', 'Pair key field'], ['providerField', 'Provider field']].map(([key, label]) => <label key={key}>{label}<select required value={form[key]} onChange={event => setForm({ ...form, [key]: event.target.value })}>
                    <option value="">Select a visible field</option>{dataset?.fields.filter(field => form.columns.includes(field.id) && ['text', 'identifier'].includes(field.type.kind)).map(field => <option key={field.id} value={field.id}>{field.label}</option>)}
                  </select></label>)}
                  {[['firstProvider', 'First provider'], ['secondProvider', 'Second provider']].map(([key, label]) => <label key={key}>{label}<input required maxLength={120} value={form[key]} onChange={event => setForm({ ...form, [key]: event.target.value })} /></label>)}
                </>}
              </fieldset>}
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
            {state.selected.element.kind !== 'integration.action' && <DatasetDerivationEvidence
              dataset={state.datasets.find(item => referenceKey(item.reference) === referenceKey(state.selected.element.dataset_ref))} />}
            <p>Recipe revision {state.selected.revision}{state.historical ? ' · Historical recipe' : ''}. {state.selected.element.kind === 'integration.action'
              ? (state.historical ? 'Historical actions are read-only.' : 'Actions execute with current human authority.')
              : 'Views query live data; this is not an immutable evidence snapshot.'}</p>
            {state.selected.element.kind !== 'integration.action' && !state.historical && <button disabled={pending || !state.datasets.some((item) => referenceKey(item.reference) === referenceKey(state.selected.element.dataset_ref))} onClick={editSelected}>Edit current recipe</button>}
            <form onSubmit={(event) => { event.preventDefault(); loadHistoricalRevision(state.selected, Number(revision)) }}><label>Earlier recipe revision<input type="number" min="1" step="1" required value={revision} onChange={(event) => setRevision(event.target.value)} /></label><button disabled={pending}>Read revision</button></form>
            {state.selected.element.kind === 'integration.action' ? <>
              {!state.historical && !['owner', 'editor'].includes(state.scope.projectRole) && <p role="status">You do not have permission to perform this action.</p>}
              {state.action && <>
                {state.action.binding.grantRevision !== null ? <IntegrationActionForm
                  element={state.selected.element} contextKey={JSON.stringify([context.current?.cacheIdentity, sessionGeneration, state.selected.revision, state.action.binding.request])}
                  grantRevision={state.action.binding.grantRevision} availability={state.action.availability} displayPreferences={preferences}
                  onSubmit={(submission, signal) => actionCallback(state.action.binding, signal, 'submit', submission)}
                  onCheckStatus={(runId, signal) => actionCallback(state.action.binding, signal, 'status', null, runId)}
                  onReconcile={(runId, signal) => actionCallback(state.action.binding, signal, 'reconcile', null, runId)} />
                  : <p role="status">{state.action.availability === 'forbidden' ? 'You do not have permission to perform this action.' :
                    state.action.availability === 'stale' ? 'This action has changed. Refresh it before continuing.' : 'This action is unavailable.'}</p>}
                {state.action.availability !== 'ready' && <button disabled={pending} onClick={() => refreshAction(state.action.binding)}>Retry action authority</button>}
              </>}
            </> : <>
            {state.read && state.previousRead && <p role="status">Previous result. {pending ? 'Refreshing view…' : 'The latest read failed; showing the last successful result.'}</p>}
            {viewError && <div role="alert"><p>{humanizeError(state.error)}</p>{state.retryable && <button disabled={pending} onClick={retryView}>Retry view</button>}</div>}
            {(state.read || pending) && <DatasetElement element={state.selected.element} read={state.read} preferences={preferences} isLoading={pending}
              onRetry={retryView}
              onLoadMore={(cursor) => loadView(state.selected, state.historical, cursor)} />}
            {!state.read && !pending && !viewError && <button onClick={retryView}>Retry view</button>}
            </>}
          </section>}
        </details>}
      </>}
    </div>
  </section>
}
