import { useEffect, useMemo, useRef, useState } from 'react'
import { authHeaders, config, noteUnauthorized } from '../api.js'
import { createSolarImportClient } from './solarImportClient.js'
import { ARTIFACT_UNVERIFIED, DOWNLOAD_REASONS, READ_RESULT_UNREADABLE, downloadReason, readResultView } from './solarReadResultModel.js'
import { SOLAR_PROJECT_READ_RESULT_SCHEMA, deriveProjectReadResultState, projectReadReason } from './solarReadResultModel.js'
import { createSolarProjectArtifactClient, SOLAR_PROJECT_ARTIFACT_TIMEOUT_MS } from './solarProjectArtifactClient.js'
import { sameSolarDrawingBinding, solarDrawingBindingKey } from './solarDrawingBinding.js'

function saveArtifact(bytes, mediaType, filename) {
  const url = URL.createObjectURL(new Blob([bytes], { type: mediaType }))
  const link = document.createElement('a')
  try {
    link.href = url
    link.download = filename
    document.body.appendChild(link)
    link.click()
  } finally {
    link.remove()
    setTimeout(() => URL.revokeObjectURL(url), 0)
  }
}

function Fields({ fields }) {
  if (!fields.length) return null
  return <table className="kv solar-read-fields"><tbody>
    {fields.map(({ key, label, text }) => <tr key={key}><th scope="row">{label}</th><td>{text}</td></tr>)}
  </tbody></table>
}

function ReadTable({ table }) {
  return <section className="solar-read-table" aria-label={table.label}>
    <h4>{table.label}</h4>
    <table className="counts grid">
      <thead><tr>{table.columns.map(({ key, label }) => <th key={key} scope="col">{label}</th>)}</tr></thead>
      <tbody>{table.rows.map((row, index) => <tr key={index}>{row.map((text, column) => <td key={column}>{text}</td>)}</tr>)}</tbody>
    </table>
    {table.omittedRows > 0 && <p className="dim">{table.omittedRows} more rows are not shown.</p>}
    {table.omittedColumns > 0 && <p className="dim">{table.omittedColumns} more columns are not shown.</p>}
  </section>
}

function StandaloneReadResult({ data, download, save = saveArtifact }) {
  const view = useMemo(() => readResultView(data), [data])
  const client = useRef(null)
  const mounted = useRef(false)
  const inFlight = useRef(false)
  const controller = useRef(null)
  const [running, setRunning] = useState(false)
  const [message, setMessage] = useState('')
  useEffect(() => {
    mounted.current = true
    return () => { mounted.current = false; controller.current?.abort() }
  }, [])

  const startDownload = async () => {
    if (inFlight.current || view.artifact?.state !== 'ready') return
    inFlight.current = true
    setRunning(true)
    setMessage('')
    const activeController = new AbortController()
    controller.current = activeController
    try {
      let transport = download
      if (!transport) {
        if (!client.current) client.current = createSolarImportClient({
          fetchImpl: (...args) => fetch(...args), apiBase: config.apiBase,
          headers: () => ({ 'X-Tenant-Id': config.tenant, ...authHeaders() }),
          onResponse: (response, url, sentAuth) => noteUnauthorized(response, url, sentAuth),
        })
        transport = client.current.downloadArtifact
      }
      const result = await transport({ drawingId: view.drawingId, ref: view.artifact.ref, signal: activeController.signal })
      if (!mounted.current) return
      if (result.ok) {
        try {
          save(result.value.bytes, result.value.mediaType, result.value.filename)
          if (mounted.current) setMessage(`Downloaded ${result.value.filename}.`)
        } catch { if (mounted.current) setMessage(DOWNLOAD_REASONS.SAVE_FAILED) }
      } else if (result.code !== 'SOLAREDGE_CLIENT_ABORTED') setMessage(downloadReason(result.code, result.status))
    } catch { if (mounted.current) setMessage(DOWNLOAD_REASONS.FALLBACK) }
    finally {
      inFlight.current = false
      if (mounted.current) setRunning(false)
    }
  }

  return <div className="solar-read-result" data-testid="solar-read-result" data-tool={view.tool ?? ''}>
    {view.refusal ? <p className="result-empty">{READ_RESULT_UNREADABLE}</p> : <>
      {view.headline !== null && <p className="solar-read-headline" data-testid="solar-read-headline">{view.headline}</p>}
      {view.oneLiner !== null && <p className="solar-read-summary">{view.oneLiner}</p>}
      <Fields fields={view.fields} />
      {view.omittedFields > 0 && <p className="dim">{view.omittedFields} more values are not shown.</p>}
      {view.tables.map((table) => <ReadTable key={table.key} table={table} />)}
      {view.sections.map((section) => <section key={section.key} className="solar-read-section" aria-label={section.label}>
        <h4>{section.label}</h4>
        <Fields fields={section.fields} />
        {section.tables.map((table) => <ReadTable key={table.key} table={table} />)}
      </section>)}
      {view.artifact?.state === 'unverified' && <p className="dim" data-testid="solar-read-download-status">{ARTIFACT_UNVERIFIED}</p>}
      {view.artifact?.state === 'ready' && <>
        <button type="button" className="btn ghost" data-testid="solar-read-download" disabled={running} onClick={startDownload}>
          {running ? `Downloading ${view.artifact.filename}` : `Download ${view.artifact.filename} (${view.artifact.sizeText})`}
        </button>
        {message && <p role="status" data-testid="solar-read-download-status">{message}</p>}
      </>}
    </>}
    {view.details.length > 0 && <details className="solar-read-details"><summary>Read details</summary>
      <table className="kv"><tbody>{view.details.map(({ label, text }) => <tr key={label}><th scope="row">{label}</th><td>{text}</td></tr>)}</tbody></table>
    </details>}
  </div>
}

function projectBranch(data, solarBinding) {
  if (solarBinding !== undefined) return true
  try {
    if (!data || typeof data !== 'object') return false
    const descriptor = Object.getOwnPropertyDescriptor(data, 'schema')
    return !!descriptor && (!Object.hasOwn(descriptor, 'value') || descriptor.value === SOLAR_PROJECT_READ_RESULT_SCHEMA)
  } catch { return true }
}

export default function SolarReadResult(props) {
  return projectBranch(props.data, props.solarBinding)
    ? <ProjectReadResult {...props} /> : <StandaloneReadResult {...props} />
}

function ProjectReadResult({ data, solarBinding, authorityKey, projectClient, catalogDigest,
  projectCurrent = true, save = saveArtifact }) {
  let bindingKey
  try { bindingKey = solarDrawingBindingKey(solarBinding) } catch { bindingKey = null }
  const state = useMemo(() => deriveProjectReadResultState({ binding: solarBinding, data,
    catalogDigest, current: projectCurrent }), [bindingKey, data, catalogDigest, projectCurrent])
  const latest = useRef(null)
  const generation = useRef(0)
  const operation = useRef(null)
  const mounted = useRef(false)
  const previous = latest.current
  if (!previous || previous.bindingKey !== bindingKey || previous.data !== data ||
      previous.authorityKey !== authorityKey || previous.projectClient !== projectClient ||
      previous.catalogDigest !== catalogDigest || previous.projectCurrent !== projectCurrent) {
    generation.current += 1
    operation.current?.controller.abort()
    operation.current = null
  }
  latest.current = { bindingKey, data, authorityKey, projectClient, catalogDigest, projectCurrent, save }
  const token = generation.current
  const [downloadState, setDownloadState] = useState({ phase: 'idle', outcome: null, token })
  const visible = downloadState.token === token ? downloadState : { phase: 'idle', outcome: null }
  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
      operation.current?.controller.abort()
      operation.current = null
    }
  }, [])
  let unavailable = false
  try {
    unavailable = typeof authorityKey !== 'string' || !authorityKey.trim() ||
      !projectClient || !['downloadArtifact', 'getSnapshot', 'subscribe', 'captureCatalogScope'].every((key) => typeof projectClient[key] === 'function')
  } catch { unavailable = true }
  const refusal = state.mode === 'refused' ? state.reason : unavailable ? 'SIP_R12_CLIENT_UNAVAILABLE' : null

  const startDownload = async () => {
    if (refusal || operation.current || state.view.artifact?.state !== 'ready') return
    const controller = new AbortController()
    const active = { controller, token, deadline: Date.now() + SOLAR_PROJECT_ARTIFACT_TIMEOUT_MS }
    operation.current = active
    const capturedProps = latest.current
    const locallyCurrent = () => mounted.current && generation.current === token &&
      operation.current === active && latest.current.data === capturedProps.data &&
      latest.current.bindingKey === capturedProps.bindingKey && latest.current.projectClient === projectClient &&
      latest.current.authorityKey === authorityKey && latest.current.catalogDigest === catalogDigest &&
      latest.current.projectCurrent === projectCurrent
    let capturedScope, unsubscribe
    const scopeCurrent = () => {
      try {
        const snapshot = projectClient.getSnapshot()
        return locallyCurrent() && !controller.signal.aborted && capturedScope() === true &&
          sameSolarDrawingBinding(snapshot.binding, state.binding) && snapshot.authorityKey === authorityKey
      } catch { return false }
    }
    const current = () => scopeCurrent() && Date.now() < active.deadline
    const timeout = () => {
      if (scopeCurrent()) setDownloadState({ phase: 'refused', token,
        outcome: { ok: false, status: null, reason: 'SIP_R12_TIMEOUT', envelope: null } })
    }
    const stale = () => {
      if (locallyCurrent()) setDownloadState({ phase: 'refused', token,
        outcome: { ok: false, status: null, reason: 'SIP_R12_SCOPE_STALE', envelope: null } })
      controller.abort()
    }
    try {
      capturedScope = projectClient.captureCatalogScope()
      if (!current()) { stale(); return }
      unsubscribe = projectClient.subscribe(() => { if (!scopeCurrent()) stale() })
      if (!current()) { stale(); return }
      setDownloadState({ phase: 'downloading', outcome: null, token })
      const wrapper = createSolarProjectArtifactClient({ projectClient })
      const outcome = await wrapper.downloadArtifact({ binding: state.binding, authorityKey,
        ref: state.view.artifact.ref, inputIntakeSha256: state.inputIntakeSha256, graphSha256: state.graphSha256,
        current: projectCurrent, signal: controller.signal, isCurrent: scopeCurrent })
      if (!scopeCurrent()) return
      if (Date.now() >= active.deadline) { timeout(); return }
      if (!outcome.ok) { setDownloadState({ phase: 'refused', outcome, token }); return }
      // Save is the first browser side effect, after byte verification and both scope guards.
      if (!current()) { timeout(); return }
      try {
        capturedProps.save(outcome.data.bytes, outcome.data.mediaType, outcome.data.filename)
        if (current()) setDownloadState({ phase: 'downloaded', outcome, token })
      } catch {
        if (current()) setDownloadState({ phase: 'refused', token,
          outcome: { ok: false, status: null, reason: 'SIP_R12_SAVE_FAILED', envelope: null } })
      }
    } catch {
      if (scopeCurrent() && Date.now() >= active.deadline) timeout()
      else if (current()) setDownloadState({ phase: 'refused', token,
        outcome: { ok: false, status: null, reason: 'SIP_R7_TRANSPORT_FAILED', envelope: null } })
    } finally {
      try { unsubscribe?.() } catch { /* observer isolation */ }
      if (operation.current === active) {
        operation.current = null
        if (mounted.current && generation.current === token) setDownloadState((value) =>
          value.token === token && value.phase === 'downloading' ? { phase: 'idle', outcome: null, token } : value)
      }
    }
  }

  if (refusal) return <div className="solar-read-result" data-testid="solar-read-result">
    <p className="result-empty">{projectReadReason(refusal)}</p>
  </div>
  const view = state.view
  const running = visible.phase === 'downloading'
  const message = visible.phase === 'downloaded' ? `Downloaded ${visible.outcome.data.filename}.` :
    visible.phase === 'refused' ? projectReadReason(visible.outcome.reason, visible.outcome.status) : ''
  return <div className="solar-read-result" data-testid="solar-read-result" data-tool={view.tool ?? ''}>
    {data.is_head === false && <p className="dim">Historical version.</p>}
    {view.headline !== null && <p className="solar-read-headline" data-testid="solar-read-headline">{view.headline}</p>}
    {view.oneLiner !== null && <p className="solar-read-summary">{view.oneLiner}</p>}
    <Fields fields={view.fields} />
    {view.omittedFields > 0 && <p className="dim">{view.omittedFields} more values are not shown.</p>}
    {view.tables.map((table) => <ReadTable key={table.key} table={table} />)}
    {view.sections.map((section) => <section key={section.key} className="solar-read-section" aria-label={section.label}>
      <h4>{section.label}</h4><Fields fields={section.fields} />
      {section.tables.map((table) => <ReadTable key={table.key} table={table} />)}
    </section>)}
    {view.artifact?.state === 'unverified' && <p className="dim" data-testid="solar-read-download-status">{projectReadReason('SIP_R12_ARTIFACT_REF_INVALID')}</p>}
    {view.artifact?.state === 'ready' && <>
      <button type="button" className="btn ghost" data-testid="solar-read-download" disabled={running} onClick={startDownload}>
        {running ? `Downloading ${view.artifact.filename}` : `Download ${view.artifact.filename} (${view.artifact.sizeText})`}
      </button>
      {message && <p role="status" data-testid="solar-read-download-status">{message}</p>}
    </>}
    {view.details.length > 0 && <details className="solar-read-details"><summary>Read details</summary>
      <table className="kv"><tbody>{view.details.map(({ label, text }) => <tr key={label}><th scope="row">{label}</th><td>{text}</td></tr>)}</tbody></table>
    </details>}
  </div>
}
