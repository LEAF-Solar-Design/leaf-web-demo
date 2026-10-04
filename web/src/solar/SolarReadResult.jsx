import { useEffect, useMemo, useRef, useState } from 'react'
import { authHeaders, config, noteUnauthorized } from '../api.js'
import { createSolarImportClient } from './solarImportClient.js'
import { ARTIFACT_UNVERIFIED, DOWNLOAD_REASONS, READ_RESULT_UNREADABLE, downloadReason, readResultView } from './solarReadResultModel.js'

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

export default function SolarReadResult({ data, download, save = saveArtifact }) {
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
