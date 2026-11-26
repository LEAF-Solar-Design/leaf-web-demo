import { useEffect, useMemo, useRef, useState } from 'react'
import VersionList from '../../components/VersionList.jsx'
import LabDiffView from './LabDiffView.jsx'
import { labArtifactsClient, SAVED_REVISION_EVENT } from './labArtifactsClient.js'

function StoredResult({ jobId, producer, blockId }) {
  const { corpus, suite, policy } = producer?.context ?? {}
  const report = producer?.result
  const pins = [
    ['Block', blockId ?? policy?.block_id],
    ['Producer worker', producer?.worker_id],
    ['Corpus material', corpus?.material_id],
    ['Corpus digest', corpus?.sha256],
    ['Suite material', suite?.material_id],
    ['Saved suite digest', suite?.sha256],
    ['Evaluated suite digest', producer?.evaluated_suite_sha256],
    ['Policy digest', policy?.digest],
    ['Result digest', producer?.result_sha256],
  ].filter(([, value]) => typeof value === 'string' && value)
  const reasons = Array.isArray(report?.reasons) ? report.reasons.filter(reason => typeof reason === 'string') : []
  return <details>
    <summary>Producer {jobId}{typeof report?.verdict === 'string' && ` · ${report.verdict}`}</summary>
    {Number.isSafeInteger(producer?.attempt) && <p>Producer attempt {producer.attempt}</p>}
    {reasons.length > 0 && <ul>{reasons.map((reason, index) => <li key={index}>{reason}</li>)}</ul>}
    {pins.length ? <dl style={{ overflowWrap: 'anywhere' }}>
      {pins.map(([label, value]) => <div key={label}><dt>{label}</dt><dd><code>{value}</code></dd></div>)}
    </dl> : <p>This result has no stored producer input pins.</p>}
  </details>
}

function storedResultBlocks(events, results) {
  const blocks = new Map()
  for (const event of events) {
    if (event.kind !== 'value' || typeof event.block_id !== 'string') continue
    let summary = event.payload?.result?.data
    if (typeof summary === 'string') {
      try { summary = JSON.parse(summary) } catch { continue }
    }
    if (typeof summary?.job_id === 'string' && Object.hasOwn(results, summary.job_id)) {
      blocks.set(summary.job_id, event.block_id)
    }
  }
  return blocks
}

function OpenedBundle({ bundle }) {
  const { identity, evaluations } = bundle.parts
  return <>
    <p role="status">Opened bundle {identity.doc_id} r{identity.revision}
      {' · '}{evaluations.length} stored evaluations. Graph is ready to edit.</p>
    {evaluations.length > 0 && <section aria-label="Stored evaluation lineage">
      <h3>Stored evaluation lineage</h3>
      {evaluations.map(({ run, events, complete_results: results }, index) => {
        const producers = typeof results === 'object' && !Array.isArray(results) ? Object.entries(results) : []
        const blocks = producers.length ? storedResultBlocks(events, results) : new Map()
        const status = typeof run.state === 'string' ? run.state : 'status unavailable'
        const reason = events.findLast(event => ['failed', 'cancelled', 'suspended'].includes(event.kind)
          && typeof event.payload?.reason === 'string')?.payload.reason
        return <details key={index}>
          <summary>Run {typeof run.run_id === 'string' ? run.run_id : 'ID unavailable'} · {status}</summary>
          <p>Saved graph r{identity.revision} <code style={{ overflowWrap: 'anywhere' }}>{identity.sha256}</code></p>
          {reason && <p>{reason}</p>}
          {producers.length ? producers.map(([jobId, producer]) =>
            <StoredResult key={jobId} jobId={jobId} producer={producer} blockId={blocks.get(jobId)} />)
            : <p>No complete producer result was stored for this run.</p>}
        </details>
      })}
    </section>}
  </>
}

export default function HistoryView({ hidden = false, client = labArtifactsClient }) {
  const [, refresh] = useState(0)
  const [pending, setPending] = useState(false)
  const [loading, setLoading] = useState(false)
  const operation = useRef(0)
  const loadOperation = useRef(0)
  const [status, setStatus] = useState('')
  const [before, setBefore] = useState('')
  const [after, setAfter] = useState('')
  const [diff, setDiff] = useState(null)
  const [preview, setPreview] = useState(null)
  const [exportRevision, setExportRevision] = useState('')
  const state = client.state
  const times = [...state.requestTimes].sort((a, b) => a - b)
  const percentile = rank => times.length ? Math.round(times[Math.ceil(times.length * rank) - 1]) : 0
  const docId = client.currentDocId()
  const generation = client.currentGeneration()
  const valid = token => generation === client.currentGeneration() && token === operation.current
  useEffect(() => client.subscribe(() => refresh(value => value + 1)), [client])
  useEffect(() => {
    operation.current += 1
    setBefore(''); setAfter(''); setDiff(null); setPreview(null); setExportRevision(''); setStatus(''); setPending(false)
  }, [client, generation])
  useEffect(() => {
    if (hidden) return undefined
    let active = true
    const load = async () => {
      const token = ++loadOperation.current
      setLoading(true)
      const results = await Promise.all([client.revisions(docId), client.head(docId)])
      const failure = results.find(result => !result.ok && result.status !== 404)
      if (active && generation === client.currentGeneration() && token === loadOperation.current) {
        setLoading(false)
        if (failure) setStatus(`History failed: ${failure.detail}`)
      }
    }
    const saved = event => { if (event.detail.doc_id === docId) void load() }
    void load()
    globalThis.addEventListener?.(SAVED_REVISION_EVENT, saved)
    return () => { active = false; globalThis.removeEventListener?.(SAVED_REVISION_EVENT, saved) }
  }, [client, hidden, docId, generation])
  const rows = useMemo(() => state.revisions.map(row => ({ ...row, v: row.revision, tool: 'artifact',
    when: row.created_at * 1000, sha: row.sha256 })), [state.revisions])
  async function run(execute, kind) {
    const token = ++operation.current
    setPending(true)
    try {
      const result = await execute()
      if (!valid(token)) return result
      setStatus(result.ok ? kind === 'Publish' ? `Published r${result.new_head} · ${result.receipt_id} · ${result.sha256}`
        : `Restored as r${result.revision}` : result.conflict ? `Someone saved r${result.head}. Reload history and try again.` : `${kind} failed: ${result.detail}`)
      return result
    } finally { if (valid(token)) setPending(false) }
  }
  const busy = pending || loading
  return <div hidden={hidden}>
    <h2>History</h2>
    {loading && <p role="status">Loading history…</p>}
    <p role="status">{state.head == null ? 'Unsaved' : `Head r${state.head}`}</p>
    <VersionList key={generation} variant="drawer" versions={rows} head={state.head} previewingVersion={preview} onPreview={setPreview}
      rowTitle={row => new Date(row.when).toISOString()}
      rowSub={row => row.v === state.published ? `Published r${row.v}` : `${row.author} · ${row.sha?.slice(0, 8)}`}
      restore={{ run: v => run(() => client.restore(docId, v, state.head), 'Restore'), mode: 'restore',
        eligible: (row, isHead) => !isHead, disabled: busy }} rewind={null} />
    <button type="button" disabled={busy || !rows.length} onClick={async () => {
      const token = ++loadOperation.current
      setLoading(true)
      try {
        const result = await client.revisions(docId, { before: Math.min(...rows.map(row => row.v)) })
        if (generation === client.currentGeneration() && token === loadOperation.current && !result.ok) setStatus(`History failed: ${result.detail}`)
      } finally { if (generation === client.currentGeneration() && token === loadOperation.current) setLoading(false) }
    }}>Load older</button>
    <label>Before revision<select aria-label="Before revision" value={before} onChange={event => setBefore(event.target.value)}>
      <option value="">Choose revision</option>{rows.map(row => <option key={row.v} value={row.v}>r{row.v}</option>)}
    </select></label>
    <label>After revision<select aria-label="After revision" value={after} onChange={event => setAfter(event.target.value)}>
      <option value="">Choose revision</option>{rows.map(row => <option key={row.v} value={row.v}>r{row.v}</option>)}
    </select></label>
    <button type="button" disabled={busy || !before || !after} onClick={async () => {
      const token = ++operation.current
      setPending(true)
      try {
        const result = await client.diff(docId, before, after)
        if (!valid(token)) return
        if (result.ok) { setDiff(result); setStatus('Compared revisions.') }
        else setStatus(`Compare failed: ${result.detail}`)
      } finally { if (valid(token)) setPending(false) }
    }}>Compare</button>
    {diff && <LabDiffView {...diff} />}
    <label>Export revision<select aria-label="Export revision" value={exportRevision || preview || state.head || ''}
      disabled={busy} onChange={event => setExportRevision(event.target.value)}>
      <option value="">Choose revision</option>{rows.map(row => <option key={row.v} value={row.v}>r{row.v}</option>)}
    </select></label>
    <button type="button" disabled={busy || !(exportRevision || preview || state.head)} onClick={async () => {
      const revision = Number(exportRevision || preview || state.head)
      const token = ++operation.current
      setPending(true)
      try {
        const result = await client.exportBundle(docId, revision, { preserveText: true })
        if (!valid(token)) return
        if (!result.ok) { setStatus(`Export failed: ${result.detail}`); return }
        const urlApi = URL
        const url = urlApi.createObjectURL(new Blob([result.text], { type: 'application/json' }))
        const link = document.createElement('a')
        try {
          link.href = url
          link.download = `lab-${docId}-r${revision}.json`
          document.body.appendChild(link)
          link.click()
        } finally { link.remove(); setTimeout(() => urlApi.revokeObjectURL(url), 1000) }
        setStatus(`Exported r${revision}.`)
      } catch { if (valid(token)) setStatus('Export failed: Could not download the bundle.') }
      finally { if (valid(token)) setPending(false) }
    }}>Export</button>
    <label>Reopen bundle<input type="file" accept="application/json,.json" aria-label="Reopen bundle" disabled={busy}
      onChange={async event => {
        const file = event.target.files?.[0]
        event.target.value = ''
        if (!file) return
        const token = ++operation.current
        const graphOperation = client.beginGraphOperation()
        setPending(true)
        try {
          if (file.size > 64 * 1024 * 1024) throw new Error('Bundle is too large.')
          const text = typeof file.text === 'function' ? await file.text() : await new Promise((resolve, reject) => {
            const reader = new FileReader()
            reader.onload = () => resolve(reader.result)
            reader.onerror = () => reject(new Error('Could not read the bundle.'))
            reader.readAsText(file)
          })
          if (!valid(token) || !client.isCurrentOperation(graphOperation)) return
          const result = await client.reopenBundle(text)
          if (valid(token)) setStatus(result.ok ? `Reopened r${result.revision}.` : `Reopen failed: ${result.detail}`)
        } catch (error) { if (valid(token)) setStatus(`Reopen failed: ${error.message}`) }
        finally { if (valid(token)) setPending(false) }
      }} /></label>
    {state.openedBundle && <OpenedBundle key={state.openedBundle.manifest.parts.evaluations} bundle={state.openedBundle} />}
    <button type="button" disabled={busy || state.head == null} onClick={() => run(() => client.publish(docId, state.head), 'Publish')}>Publish r{state.head ?? '?'}</button>
    <p role="status">{status}</p>
    <p>History p50 {percentile(.5)} ms · p99 {percentile(.99)} ms</p>
  </div>
}
