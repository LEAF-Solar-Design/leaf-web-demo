import { useLayoutEffect, useRef, useState } from 'react'
import { boundedIntegrationBody, validateDatasetRegistration } from './client.js'
import { ConnectorDefinitionV1 } from '../../../vendor/assistant-contract/src/integration'

const kinds = ['text', 'boolean', 'identifier', 'count', 'instant', 'date', 'number', 'ratio', 'money', 'duration']
const newField = () => ({ id: '', label: '', kind: '', nullable: false, sensitivity: 'internal', pointer: '', conversion: 'identity',
  key: false, unit: '', precision: '', scale: '', currency: '' })
const sameRef = (a, b) => a && b && ['id', 'version', 'digest'].every((key) => a[key] === b[key])
const supported = (op) => op.method === 'GET' && op.effect === 'read' && op.pagination.kind === 'none' &&
  op.pagination.records_pointer === '' && !/[{}?]/.test(op.path)
const purposeFor = (auth) => auth === 'bearer' ? 'access_token' : auth === 'api_key' ? 'api_key' : null
const refText = (ref) => `${ref.id} · version ${ref.version} · ${ref.digest}`
const validSlug = (value) => /^[a-z0-9]+(?:[-_][a-z0-9]+)*(?![\s\S])/.test(value) && value.length <= 128
const validName = (value) => value.length > 0 && value.length <= 120
const fixedIssues = {
  unsupported_auth: 'This authentication scheme is unsupported.', unsupported_version: 'Use OpenAPI 3.0 or 3.1 JSON.',
  unsupported_operation: 'This operation cannot be used in this setup.', invalid_document: 'The document could not be imported.',
  'invalid-document': 'Use a bounded OpenAPI 3.0 or 3.1 JSON document with paths.',
  'invalid-origin': 'Provide one valid HTTPS root server or an explicit origin override.',
  'unsupported-server': 'Server variables and path or operation server overrides are unsupported here.',
  'unsupported-operation': 'Only static GET operations are supported here.',
  'unsupported-parameters': 'Required request parameters are unsupported here.',
  'unsupported-reference': 'The document contains an external, unsafe, missing or cyclic reference.',
  'needs-auth-adapter': 'Use a consistent no-auth, bearer, or header API key scheme.',
  'needs-records-mapping': 'A top-level JSON array response could not be established.',
  'generated-operation-id': 'The host generated a normalized operation ID. Review and select that ID below.',
  'default-limits': 'The host supplied draft limits. This setup synchronizes at most 250 records / 128 KiB.',
  'needs-publication-review': 'Review the normalized destination, authentication and read operation before publishing.',
  'no-supported-operations': 'The document has no supported operation. Choose different supported JSON.',
}

export function datasetRegistration(form, connection, operationRef) {
  return validateDatasetRegistration({ definition: { schema_version: 1, id: form.id, title: form.title, version: Number(form.version),
    fields: form.fields.map((field) => ({ id: field.id, label: field.label, nullable: field.nullable, sensitivity: field.sensitivity,
      type: { kind: field.kind,
        ...(field.kind === 'number' ? { ...(field.unit ? { unit: field.unit } : {}), ...(field.precision !== '' ? { precision: Number(field.precision) } : {}) } : {}),
        ...(['ratio', 'money'].includes(field.kind) ? { scale: field.scale } : {}),
        ...(field.kind === 'money' ? { currency: field.currency } : {}),
        ...(field.kind === 'duration' ? { unit: field.unit } : {}),
      } })), key_fields: form.fields.filter((field) => field.key).map((field) => field.id),
    ...(form.timeField ? { time_field: form.timeField } : {}), reporting_time_zone: form.timezone,
    retention_days: Number(form.retention), lineage: { connection_id: connection.id, operation_ref: operationRef },
  }, mapping: { schema_version: 1, fields: form.fields.map((field) => ({ field_id: field.id, pointer: field.pointer, conversion: field.conversion })) } })
}

// The parent supplies its full scope/session guard. No independent weaker identity is maintained here.
export function IntegrationSetupPanel({ client, projectId, scope, signal, guard, onForbidden, onDatasetReady }) {
  const [lifetime, setLifetime] = useState(null)
  const password = useRef(null)
  const busy = useRef(false)
  const [pending, setPending] = useState('')
  const [message, setMessage] = useState('')
  const [error, setError] = useState('')
  const [source, setSource] = useState({ text: '', id: '', title: '', origin: '' })
  const [issues, setIssues] = useState([])
  const [draft, setDraft] = useState(null)
  const [operationId, setOperationId] = useState('')
  const [published, setPublished] = useState(null)
  const [connectionForm, setConnectionForm] = useState({ id: '', title: '', origin: '' })
  const [connection, setConnection] = useState(null)
  const [custody, setCustody] = useState(false)
  const [form, setForm] = useState({ id: '', title: '', version: '1', timezone: 'UTC', retention: '30', timeField: '', fields: [newField()] })
  const [dataset, setDataset] = useState(null)
  const [result, setResult] = useState(null)
  const [unknown, setUnknown] = useState(null)
  const [recovery, setRecovery] = useState(null)
  const [recoveryVersion, setRecoveryVersion] = useState('1')
  const [abandoned, setAbandoned] = useState([])
  const alive = () => !signal.aborted && Boolean(lifetime) && !lifetime.signal.aborted
  useLayoutEffect(() => {
    const controller = new AbortController()
    const dispose = () => { if (password.current) password.current.value = ''; controller.abort() }
    setLifetime(controller)
    if (signal.aborted) dispose()
    else signal.addEventListener('abort', dispose, { once: true })
    return () => { dispose(); signal.removeEventListener('abort', dispose) }
  }, [signal])
  const options = { signal: lifetime?.signal }
  const definition = draft?.draft_definition
  const selected = definition?.operations.find((op) => op.id === operationId)
  const operationRef = published?.operationReferences.find((ref) => ref.id === operationId)
  const purpose = purposeFor(connection?.auth_kind)

  async function run(label, task, attempt = null) {
    if (busy.current || !alive() || scope.projectRole !== 'owner' || (attempt && unknown)) return
    busy.current = true
    setPending(label); setError(''); setMessage('')
    let dispatched = false
    try {
      // Read-only recovery also checks authority before exposing resource metadata.
      await guard(lifetime.signal)
      if (!alive()) return
      const send = async (callback) => {
        dispatched = true
        try { return await callback() }
        catch (failure) { if (failure?.dispatched === false) dispatched = false; throw failure }
      }
      await task(send)
    } catch (failure) {
      if (!alive()) return
      if (dispatched && attempt) {
        setUnknown(attempt); setRecovery(null)
        setError('Outcome unknown. The request may have taken effect. Read status before choosing an existing resource or a new ID. No request will be repeated automatically.')
      } else setError('Setup could not continue. Check the nonsecret inputs and current authorization, then try again.')
      if (failure?.status === 403) onForbidden?.()
    } finally {
      if (password.current) password.current.value = ''
      busy.current = false
      if (alive()) setPending('')
    }
  }
  function localError() { setError('Enter valid IDs, titles, JSON, typed fields, keys and mapping within the displayed limits. Nothing was submitted.') }
  function importDocument(event) {
    event.preventDefault()
    let input
    try {
      if (!validSlug(source.id) || !validName(source.title) || abandoned.includes(`connector:${source.id}`)) throw new Error()
      if (source.origin && !ConnectorDefinitionV1.shape.origins.element.safeParse(source.origin).success) throw new Error()
      const document = JSON.parse(source.text)
      if (!document || Array.isArray(document) || !/^3\.(0|1)\.[0-9]+$/.test(document.openapi || '')) throw new Error()
      input = { document, options: { id: source.id, title: source.title, ...(source.origin ? { origin: source.origin } : {}) } }
      boundedIntegrationBody(input)
    } catch { localError(); return }
    run('Importing JSON…', async (send) => {
      const imported = await send(() => client.importConnector(projectId, input, options))
      if (!alive()) return
      setIssues(imported.issues)
      if (!imported.reference) { setMessage('Import was not accepted. Review the diagnostics and choose supported JSON.'); return }
      // Import settled, but a failed read must not invite a duplicate import.
      setUnknown({ kind: 'import', id: source.id })
      const resource = await client.getConnector(projectId, source.id, options)
      if (!alive()) return
      if (!sameRef(resource.reference, imported.reference) || resource.revision !== imported.revision) throw new Error()
      setDraft(resource); setUnknown(null); setOperationId('')
      setMessage('Imported. Review the normalized destination, authentication and operations.')
    }, { kind: 'import', id: source.id })
  }
  async function chooseFile(event) {
    const file = event.target.files?.[0]
    if (!file || busy.current) return
    if (file.size > 262144) { localError(); event.target.value = ''; return }
    try {
      const text = await file.text()
      if (!alive() || busy.current) return
      if (new TextEncoder().encode(text).byteLength > 262144) { localError(); return }
      setSource((old) => ({ ...old, text })); setError('')
    } catch { if (alive()) localError() }
  }
  function publish() {
    if (!selected || !supported(selected)) { localError(); return }
    const attempt = { kind: 'publish', id: draft.id, version: definition.version, operationId, definition }
    run('Publishing selected read…', async (send) => {
      const saved = await send(() => client.publishConnector(projectId, draft.id, { version: definition.version,
        expectedRevision: draft.revision, readOperationIds: [operationId] }, options))
      if (!alive()) return
      attempt.reference = saved.reference
      attempt.revision = saved.revision
      setUnknown(attempt)
      const resource = await client.getConnectorVersion(projectId, draft.id, definition.version, options)
      if (!alive()) return
      if (!sameRef(saved.reference, resource.reference) || saved.revision !== resource.revision || !publicationMatches(resource, attempt)) throw new Error()
      usePublication(resource); setUnknown(null); setMessage('Published the selected read operation. Create its connection next.')
    }, attempt)
  }
  function publicationMatches(resource, attempt) {
    const op = resource.definition.operations[0]
    const original = attempt.definition.operations.find((item) => item.id === attempt.operationId)
    return (!attempt.reference || (sameRef(resource.reference, attempt.reference) && resource.revision === attempt.revision)) &&
      resource.definition.operations.length === 1 && op.id === attempt.operationId && supported(op) &&
      JSON.stringify(op) === JSON.stringify(original) && JSON.stringify(resource.definition.auth) === JSON.stringify(attempt.definition.auth) &&
      JSON.stringify(resource.definition.origins) === JSON.stringify(attempt.definition.origins)
  }
  function usePublication(resource) {
    setPublished(resource)
    setConnectionForm((old) => ({ ...old, origin: resource.definition.origins.length === 1 ? resource.definition.origins[0] : '' }))
  }
  function connectionMatches(resource, submitted) {
    return resource.title === submitted.title && sameRef(resource.connector_ref, submitted.connectorRef) && resource.origin === submitted.origin &&
      resource.auth_kind === published.auth_kind && resource.operation_ids.length === 1 && resource.operation_ids[0] === submitted.operationIds[0] &&
      !['revoked', 'disabled'].includes(resource.state)
  }
  function createConnection(event) {
    event.preventDefault()
    if (!validSlug(connectionForm.id) || !validName(connectionForm.title) || !published.definition.origins.includes(connectionForm.origin) || abandoned.includes(`connection:${connectionForm.id}`)) { localError(); return }
    const input = { ...connectionForm, connectorRef: published.reference, operationIds: [operationId] }
    run('Creating connection…', async (send) => {
      const resource = await send(() => client.createConnection(projectId, input, options))
      if (!alive()) return
      if (!connectionMatches(resource, input)) throw new Error()
      setConnection(resource); setCustody(resource.auth_kind === 'none'); setMessage('Connection created. No provider request has been made.')
    }, { kind: 'connection', id: input.id, input })
  }
  function saveCredential(event) {
    event.preventDefault()
    if (busy.current || unknown || !alive()) return
    // Keep entry in the DOM during authority preflight, so disposal can clear it immediately.
    if (!password.current?.value || new TextEncoder().encode(password.current.value).byteLength > 16384) {
      if (password.current) password.current.value = ''
      localError(); return
    }
    run('Saving private credential…', async (send) => {
      let value = password.current?.value || ''
      if (password.current) password.current.value = ''
      try {
        const resource = await send(() => client.setConnectionCredential(projectId, connection.id,
          { purpose, value, expectedRevision: connection.revision }, options))
        if (!alive()) return
        if (!connectionMatches(resource, { ...connectionForm, connectorRef: published.reference, operationIds: [operationId] })) throw new Error()
        setConnection(resource); setCustody(true); setMessage('Credential custody configured. Provider authentication will be observed only at synchronization.')
      } finally { value = '' }
    }, { kind: 'credential', id: connection.id })
  }
  function register(event) {
    event.preventDefault()
    let input
    try {
      if (abandoned.includes(`dataset:${form.id}:${form.version}`)) throw new Error()
      input = datasetRegistration(form, connection, operationRef)
    } catch { localError(); return }
    run('Registering typed dataset…', async (send) => {
      const saved = await send(() => client.registerDataset(projectId, input, options))
      if (!alive()) return
      setDataset(saved); setMessage('Dataset registered. Explicitly synchronize to request provider data.')
    }, { kind: 'dataset', id: input.definition.id, version: input.definition.version, input })
  }
  function sync() {
    run('Synchronizing up to 250 records / 128 KiB…', async (send) => {
      setResult(null)
      const synced = await send(() => client.syncDataset(projectId, dataset.reference, options))
      if (!alive()) return
      if (!sameRef(synced.receipt.connector_ref, published.reference) || !sameRef(synced.receipt.operation_ref, operationRef) ||
        synced.receipt.actor_id !== scope.platformUserId || synced.receipt.binding_id !== scope.bindingId) throw new Error()
      setResult(synced); setMessage('Synchronization settled. Review its outcome below.')
      try { await onDatasetReady(dataset.reference, lifetime.signal) }
      catch { if (alive()) setError('Synchronization settled, but the composer refresh was unavailable. Read dataset status to refresh it without synchronizing again.') }
    }, { kind: 'sync', reference: dataset.reference })
  }
  function readStatus() {
    const attempt = unknown
    run('Reading existing resource status…', async () => {
      let resource, compatible = false
      if (attempt.kind === 'import') {
        resource = await client.getConnector(projectId, attempt.id, options)
        compatible = resource.state === 'draft' && Boolean(resource.draft_definition)
      } else if (attempt.kind === 'publish' || attempt.kind === 'existingPublish') {
        resource = await client.getConnectorVersion(projectId, attempt.id, attempt.version, options)
        compatible = attempt.kind === 'publish' ? publicationMatches(resource, attempt) :
          resource.definition.operations.length === 1 && supported(resource.definition.operations[0])
      } else if (attempt.kind === 'connection' || attempt.kind === 'credential') {
        resource = await client.getConnectionSummary(projectId, attempt.id, options)
        compatible = connectionMatches(resource, attempt.input || { ...connectionForm, connectorRef: published.reference, operationIds: [operationId] }) &&
          (attempt.kind !== 'credential' || resource.credential_purposes.includes(purpose))
      } else if (attempt.kind === 'dataset') {
        resource = await client.getDatasetVersion(projectId, attempt.id, attempt.version, options)
        compatible = resource.title === attempt.input.definition.title && JSON.stringify(resource.fields) === JSON.stringify(attempt.input.definition.fields)
      } else {
        resource = await client.getDataset(projectId, attempt.reference, options)
        compatible = true
      }
      if (!alive()) return
      setRecovery({ kind: attempt.kind, resource, compatible })
      setMessage('Status read completed. Existence alone does not confirm the original request.')
    })
  }
  async function acceptRecovery() {
    const { kind, resource } = recovery
    run('Using displayed existing resource…', async () => {
      if (kind === 'import') { setDraft(resource); setOperationId('') }
      if (kind === 'publish') usePublication(resource)
      if (kind === 'existingPublish') { setOperationId(resource.definition.operations[0].id); setDraft({ id: resource.id, title: resource.title, revision: resource.revision }); usePublication(resource) }
      if (kind === 'connection' || kind === 'credential') { setConnection(resource); setCustody(kind === 'credential' || resource.auth_kind === 'none') }
      if (kind === 'dataset') setDataset({ reference: resource.reference, revision: resource.revision })
      if (kind === 'sync') {
        setResult(resource.lastAttempt ? { receipt: resource.lastAttempt, status: 'readback' } : null)
        try { await onDatasetReady(resource.reference, lifetime.signal) }
        catch { if (alive()) setError('Status was read, but the composer refresh was unavailable. Read status again without synchronizing.') }
        if (!alive()) return
      }
      setUnknown(null); setRecovery(null); setMessage('Using the displayed existing resource by your explicit choice.')
    })
  }
  function chooseNewId() {
    if (busy.current) return
    const kind = unknown.kind
    setAbandoned((old) => [...old, kind === 'dataset' ? `dataset:${unknown.id}:${unknown.version}` :
      ['connection', 'credential'].includes(kind) ? `connection:${unknown.id}` : `connector:${unknown.id}`])
    if (['import', 'publish', 'existingPublish'].includes(kind)) { setDraft(null); setPublished(null); setSource((old) => ({ ...old, id: '' })) }
    if (['import', 'publish', 'existingPublish', 'connection', 'credential'].includes(kind)) {
      setConnection(null); setCustody(false); setConnectionForm((old) => ({ ...old, id: '' }))
    }
    setDataset(null); setResult(null); setForm((old) => ({ ...old, id: '' }))
    setUnknown(null); setRecovery(null); setError(''); setMessage('Choose a new stable ID. The previous resource may still exist.')
  }
  function recoverChosen(kind) {
    let attempt
    try {
      if (kind === 'import' || kind === 'existingPublish') {
        if (!validSlug(source.id) || !Number.isSafeInteger(Number(recoveryVersion)) || Number(recoveryVersion) < 1) throw new Error()
        attempt = { kind, id: source.id, version: Number(recoveryVersion) }
      } else if (kind === 'connection') {
        if (!validSlug(connectionForm.id)) throw new Error()
        attempt = { kind, id: connectionForm.id, input: { ...connectionForm, connectorRef: published.reference, operationIds: [operationId] } }
      } else {
        const input = datasetRegistration(form, connection, operationRef)
        attempt = { kind: 'dataset', id: form.id, version: Number(form.version), input }
      }
    } catch { localError(); return }
    setUnknown(attempt); setRecovery(null); setError(''); setMessage('Read the chosen resource status. This does not submit or retry a setup mutation.')
  }
  function updateField(index, patch) { setForm((old) => ({ ...old, fields: old.fields.map((field, i) => i === index ? { ...field, ...patch } : field) })) }
  if (scope.projectRole !== 'owner' || signal.aborted) return null
  const locked = Boolean(pending || unknown)
  return <section className="integration-setup" aria-labelledby="integration-setup-title">
    <h2 id="integration-setup-title">Set up an HTTP dataset</h2>
    <p>OpenAPI 3.0 / 3.1 JSON · one static GET · top-level JSON array · no authentication, bearer token, or header API key.</p>
    <ol className="integration-setup-progress" aria-label="Setup progress">
      {['Import JSON', 'Review and publish', 'Connect privately', 'Define dataset', 'Synchronize'].map((step, i) =>
        <li key={step} aria-current={i === (!draft ? 0 : !published ? 1 : !custody ? 2 : !dataset ? 3 : 4) ? 'step' : undefined}>{step}</li>)}
    </ol>
    <p role="status" aria-live="polite">{pending || message}</p>
    {error && <p role="alert">{error}</p>}
    {!draft ? <form onSubmit={importDocument} aria-label="Import connector JSON"><fieldset disabled={locked}>
      <legend>1. Import supported JSON</legend>
      <label>Connector stable ID<input required maxLength={128} value={source.id} onChange={(e) => setSource({ ...source, id: e.target.value })} /></label>
      <label>Connector title<input required maxLength={120} value={source.title} onChange={(e) => setSource({ ...source, title: e.target.value })} /></label>
      <label>Optional HTTPS origin override<input type="url" value={source.origin} onChange={(e) => setSource({ ...source, origin: e.target.value })} /></label>
      <label>OpenAPI JSON<textarea required rows={7} value={source.text} onChange={(e) => setSource({ ...source, text: e.target.value })} /></label>
      <label>Upload JSON file<input type="file" accept=".json,application/json" onChange={chooseFile} /></label>
      <p>Public request limit: 256 KiB including options. Paste a public specification without credentials. URL fetching and manual operation editing are unavailable.</p>
      <button type="submit">Import JSON</button>
      <div><h3>Recover a chosen connector using reads</h3>
        <p>Enter its stable ID above. Existing resources require review and explicit acceptance.</p>
        <label>Existing connector version<input type="number" min="1" step="1" value={recoveryVersion} onChange={(e) => setRecoveryVersion(e.target.value)} /></label>
        <button type="button" onClick={() => recoverChosen('import')}>Recover chosen draft</button>
        <button type="button" onClick={() => recoverChosen('existingPublish')}>Recover chosen published version</button>
      </div>
    </fieldset></form> : <p>Imported connector: {draft.title} · {draft.id} · revision {draft.revision}.</p>}
    {issues.length > 0 && <div><h3>Import diagnostics</h3><ul>{issues.map((issue, i) => <li key={i}>{Object.hasOwn(fixedIssues, issue.code) ? fixedIssues[issue.code] : 'The importer reported an unsupported or invalid part of this document. Review the supported path above.'}</li>)}</ul></div>}
    {draft && !published && <fieldset disabled={locked}><legend>2. Review normalized connector</legend>
      {definition ? <>
        <p>Destination: {definition.origins.join(', ')}. Authentication: {definition.auth.kind}{definition.auth.kind === 'api_key' ? ` · header ${definition.auth.header}` : ''}.</p>
        <label>Supported read operation<select value={operationId} onChange={(e) => setOperationId(e.target.value)}>
          <option value="">Choose one normalized operation</option>{definition.operations.filter(supported).map((op) => <option key={op.id} value={op.id}>{op.id} · {op.method} {op.path}</option>)}
        </select></label>
        <ul>{definition.operations.map((op) => <li key={op.id}>{op.title} · {op.id} · {op.method} {op.path}{supported(op) ? '' : ' · unsupported in this setup'}</li>)}</ul>
        {!definition.operations.some(supported) && <p>No supported static GET returning a top-level array is available. Import a different document with a new ID.</p>}
        <button type="button" disabled={!selected || !supported(selected)} onClick={publish}>Publish selected read</button>
      </> : <p>This resource has no owner draft definition. Read status or import supported JSON with a new ID.</p>}
      <button type="button" onClick={() => { setDraft(null); setSource((old) => ({ ...old, id: '' })); setOperationId('') }}>Choose another connector ID</button>
    </fieldset>}
    {published && <p>Published connector: {refText(published.reference)}. Selected operation: {operationRef && refText(operationRef)}.</p>}
    {published && !connection && <form onSubmit={createConnection} aria-label="Create connection"><fieldset disabled={locked}>
      <legend>3. Create connection</legend>
      <label>Connection stable ID<input required maxLength={128} value={connectionForm.id} onChange={(e) => setConnectionForm({ ...connectionForm, id: e.target.value })} /></label>
      <label>Connection title<input required maxLength={120} value={connectionForm.title} onChange={(e) => setConnectionForm({ ...connectionForm, title: e.target.value })} /></label>
      <label>Reviewed destination<select required value={connectionForm.origin} onChange={(e) => setConnectionForm({ ...connectionForm, origin: e.target.value })}>
        <option value="">Choose destination</option>{published.definition.origins.map((origin) => <option key={origin}>{origin}</option>)}
      </select></label><button type="submit">Create connection</button>
      <button type="button" onClick={() => recoverChosen('connection')}>Recover chosen connection</button>
    </fieldset></form>}
    {connection && <p>Connection: {connection.title} · {connection.id} · revision {connection.revision} · {connection.state} · health {connection.health.code} · cleanup {connection.cleanup}. Configured custody does not verify provider identity.</p>}
    {connection && purpose && !custody && <form onSubmit={saveCredential} aria-label="Private credential"><fieldset disabled={locked}>
      <legend>Private credential custody</legend>
      <label>{purpose === 'access_token' ? 'Bearer token' : 'Header API key'}<input ref={password} type="password" autoComplete="off" required maxLength={16384} /></label>
      <p>Transient entry, up to 16 KiB UTF-8. Cleared after settlement. Provider authentication is observed at explicit sync.</p>
      <button type="submit">Save private credential</button>
      <button type="button" onClick={() => { if (password.current) password.current.value = ''; setUnknown({ kind: 'credential', id: connection.id }); setRecovery(null) }}>Read configured custody without upload</button>
    </fieldset></form>}
    {connection && custody && !dataset && <form onSubmit={register} aria-label="Define typed dataset"><fieldset disabled={locked}>
      <legend>4. Define typed dataset and mapping</legend>
      <p>Choose meaning explicitly. Reporting timezone belongs to this dataset and is separate from personal display preferences.</p>
      <div className="integration-setup-grid">
        <label>Dataset stable ID<input required maxLength={128} value={form.id} onChange={(e) => setForm({ ...form, id: e.target.value })} /></label>
        <label>Dataset title<input required maxLength={120} value={form.title} onChange={(e) => setForm({ ...form, title: e.target.value })} /></label>
        <label>Dataset version<input type="number" min="1" step="1" required value={form.version} onChange={(e) => setForm({ ...form, version: e.target.value })} /></label>
        <label>Reporting timezone<input required value={form.timezone} onChange={(e) => setForm({ ...form, timezone: e.target.value })} /></label>
        <label>Retention days<input type="number" min="1" max="3650" step="1" required value={form.retention} onChange={(e) => setForm({ ...form, retention: e.target.value })} /></label>
      </div>
      {form.fields.map((field, index) => <fieldset key={index}><legend>Field {index + 1}</legend><div className="integration-setup-grid">
        <label>Field {index + 1} ID<input required maxLength={128} value={field.id} onChange={(e) => updateField(index, { id: e.target.value })} /></label>
        <label>Field {index + 1} label<input required maxLength={120} value={field.label} onChange={(e) => updateField(index, { label: e.target.value })} /></label>
        <label>Field {index + 1} type<select required value={field.kind} onChange={(e) => updateField(index, { kind: e.target.value, unit: '', precision: '', scale: '', currency: '' })}>
          <option value="">Choose semantic type</option>{kinds.map((kind) => <option key={kind}>{kind}</option>)}
        </select></label>
        {field.kind === 'number' && <>
          <label>Field {index + 1} optional unit<input maxLength={40} value={field.unit} onChange={(e) => updateField(index, { unit: e.target.value })} /></label>
          <label>Field {index + 1} optional precision<input type="number" min="0" max="12" step="1" value={field.precision} onChange={(e) => updateField(index, { precision: e.target.value })} /></label>
        </>}
        {['ratio', 'money'].includes(field.kind) && <label>Field {index + 1} scale<select required value={field.scale} onChange={(e) => updateField(index, { scale: e.target.value })}>
          <option value="">Choose scale</option>{(field.kind === 'ratio' ? ['fraction', 'percent'] : ['minor', 'major']).map((scale) => <option key={scale}>{scale}</option>)}
        </select></label>}
        {field.kind === 'money' && <label>Field {index + 1} currency<input required maxLength={3} placeholder="USD" value={field.currency} onChange={(e) => updateField(index, { currency: e.target.value })} /></label>}
        {field.kind === 'duration' && <label>Field {index + 1} duration unit<select required value={field.unit} onChange={(e) => updateField(index, { unit: e.target.value })}>
          <option value="">Choose unit</option>{['ms', 's', 'minutes', 'hours'].map((unit) => <option key={unit}>{unit}</option>)}
        </select></label>}
        <label>Field {index + 1} sensitivity<select value={field.sensitivity} onChange={(e) => updateField(index, { sensitivity: e.target.value })}>{['public', 'internal', 'restricted'].map((value) => <option key={value}>{value}</option>)}</select></label>
        <label>Field {index + 1} JSON pointer<input maxLength={1024} value={field.pointer} onChange={(e) => updateField(index, { pointer: e.target.value })} /></label>
        <label>Field {index + 1} conversion<select value={field.conversion} onChange={(e) => updateField(index, { conversion: e.target.value })}>{['identity', 'string_number', 'iso_instant'].map((value) => <option key={value}>{value}</option>)}</select></label>
        <label><input type="checkbox" checked={field.nullable} onChange={(e) => updateField(index, { nullable: e.target.checked })} />Field {index + 1} nullable</label>
        <label><input type="checkbox" checked={field.key} onChange={(e) => updateField(index, { key: e.target.checked })} />Field {index + 1} stable key</label>
      </div><button type="button" disabled={form.fields.length === 1} onClick={() => setForm({ ...form, fields: form.fields.filter((_, i) => i !== index) })}>Remove field {index + 1}</button></fieldset>)}
      <button type="button" disabled={form.fields.length >= 100} onClick={() => setForm({ ...form, fields: [...form.fields, newField()] })}>Add field</button>
      <label>Optional time field<select value={form.timeField} onChange={(e) => setForm({ ...form, timeField: e.target.value })}>
        <option value="">No time field</option>{form.fields.filter((field) => ['instant', 'date'].includes(field.kind) && field.id).map((field, i) => <option key={i} value={field.id}>{field.label || field.id}</option>)}
      </select></label>
      <p>1 to 100 fields; choose 1 to 8 non-null text, identifier or count keys. JSON pointers: at most 1024 characters and 32 segments. Registration limit: 160 KiB.</p>
      <button type="submit">Register dataset</button>
      <button type="button" onClick={() => recoverChosen('dataset')}>Recover chosen dataset version</button>
    </fieldset></form>}
    {dataset && <section aria-label="First synchronization"><h3>5. Synchronize explicitly</h3>
      <p>Registered dataset: {refText(dataset.reference)} · revision {dataset.revision}.</p>
      <p>This first synchronization reads at most 250 records / 128 KiB from a top-level JSON array. It is bounded and does not claim a complete scan. No pagination or action execution is available.</p>
      <button disabled={locked} onClick={sync}>Synchronize dataset</button>
      <button disabled={locked} onClick={() => { setUnknown({ kind: 'sync', reference: dataset.reference }); setRecovery(null) }}>Read dataset status without sync</button>
      {result && <div role="status" aria-live="polite"><p>Dataset status: {result.status}. {result.status === 'readback' ? 'Latest recorded attempt; this does not prove the unknown request completed.' : ''}</p>
        <p>Outcome: {result.receipt.outcome} · source: {result.receipt.source_outcome} · rows: {result.receipt.row_count}{result.receipt.failure_code ? ` · failure: ${result.receipt.failure_code}` : ''}{result.receipt.retryAfterMs !== undefined ? ` · retry after ${result.receipt.retryAfterMs} ms (manual only)` : ''}.</p>
        <p>Receipt {result.receipt.id} · actor {result.receipt.actor_id} · binding {result.receipt.binding_id} · started {result.receipt.started_at} · finished {result.receipt.finished_at}.</p>
        <p>Dataset: {refText(result.receipt.dataset_ref)}. Connector: {refText(result.receipt.connector_ref)}. Operation: {refText(result.receipt.operation_ref)}. Transform: {refText(result.receipt.transform_ref)}.</p>
        <p>{result.status === 'ready' ? 'Data is ready for the native table or line composer below.' : result.status === 'empty' ? 'The source returned no rows.' : result.status === 'stale' ? 'The last good data is stale; the latest attempt did not refresh it.' : 'Review the recorded outcome before using data or synchronizing again.'}</p>
      </div>}
    </section>}
    {unknown && <section aria-label="Read-only setup recovery"><h3>Read-only status recovery</h3>
      <p>Chosen resource: {unknown.id || unknown.reference.id}{unknown.version ? ` · version ${unknown.version}` : ''}. Checking status sends only reads.</p>
      <button disabled={Boolean(pending)} onClick={readStatus}>Read existing resource status</button>
      {recovery && <>
        <p>Existing resource: {recovery.resource.title || recovery.resource.id || recovery.resource.reference.id} · revision {recovery.resource.revision}{recovery.resource.reference ? ` · ${refText(recovery.resource.reference)}` : ''}.</p>
        {(recovery.resource.definition || recovery.resource.draft_definition) && <>
          <p>Normalized destination: {(recovery.resource.definition || recovery.resource.draft_definition).origins.join(', ')}. Authentication: {(recovery.resource.definition || recovery.resource.draft_definition).auth.kind}.</p>
          <ul>{(recovery.resource.definition || recovery.resource.draft_definition).operations.map((op) => <li key={op.id}>{op.id} · {op.method} {op.path} · {supported(op) ? 'supported read' : 'unsupported in this setup'}</li>)}</ul>
        </>}
        {recovery.resource.connector_ref && <p>Connector: {refText(recovery.resource.connector_ref)} · destination {recovery.resource.origin} · operations {recovery.resource.operation_ids.join(', ')} · state {recovery.resource.state} · credential purposes {recovery.resource.credential_purposes.join(', ') || 'none'}.</p>}
        {recovery.resource.fields && <ul>{recovery.resource.fields.map((field) => <li key={field.id}>{field.id} · {field.label} · {field.type.kind} · {field.nullable ? 'nullable' : 'required'} · {field.sensitivity}</li>)}</ul>}
        {recovery.kind === 'sync' && <p>{recovery.resource.lastAttempt ? `Latest recorded outcome: ${recovery.resource.lastAttempt.outcome}.` : 'No synchronization attempt is recorded.'} This read cannot prove the unknown synchronization completed.</p>}
        <p>{recovery.kind === 'credential' ? 'Configured custody cannot prove the submitted credential value. It does not verify provider identity.' : recovery.kind === 'dataset' ? 'Metadata cannot prove the submitted mapping, keys, reporting timezone, retention or lineage.' : recovery.kind === 'import' ? 'Draft existence cannot prove which JSON document was imported. Review its normalized definition after explicitly choosing it.' : 'Compare the displayed resource and reviewed configuration. Existence alone does not confirm the original request.'}</p>
        {recovery.compatible ? <button disabled={Boolean(pending)} onClick={acceptRecovery}>Use displayed existing resource</button> : <p>The existing resource does not match this setup. Choose a new ID.</p>}
      </>}
      {unknown.kind !== 'sync' && <button disabled={Boolean(pending)} onClick={chooseNewId}>Choose a new resource ID</button>}
    </section>}
  </section>
}
