import { useEffect, useMemo, useState } from 'react'
import VersionList from '../../components/VersionList.jsx'
import LabDiffView from './LabDiffView.jsx'
import { labArtifactsClient, SAVED_REVISION_EVENT } from './labArtifactsClient.js'

export default function HistoryView({ hidden = false, client = labArtifactsClient }) {
  const [, refresh] = useState(0)
  const [pending, setPending] = useState(false)
  const [status, setStatus] = useState('')
  const [before, setBefore] = useState('')
  const [after, setAfter] = useState('')
  const [diff, setDiff] = useState(null)
  const [preview, setPreview] = useState(null)
  const state = client.state
  const times = [...state.requestTimes].sort((a, b) => a - b)
  const percentile = rank => times.length ? Math.round(times[Math.ceil(times.length * rank) - 1]) : 0
  const docId = client.currentDocId()
  useEffect(() => client.subscribe(() => refresh(value => value + 1)), [client])
  useEffect(() => {
    if (hidden) return undefined
    let active = true
    const load = async () => {
      const results = await Promise.all([client.revisions(docId), client.head(docId)])
      const failure = results.find(result => !result.ok && result.status !== 404)
      if (active && failure) setStatus(`History failed: ${failure.detail}`)
    }
    const saved = event => { if (event.detail.doc_id === docId) void load() }
    void load()
    globalThis.addEventListener?.(SAVED_REVISION_EVENT, saved)
    return () => { active = false; globalThis.removeEventListener?.(SAVED_REVISION_EVENT, saved) }
  }, [client, hidden, docId])
  const rows = useMemo(() => state.revisions.map(row => ({ ...row, v: row.revision, tool: 'artifact',
    when: row.created_at * 1000, sha: row.sha256 })), [state.revisions])
  async function run(operation, kind) {
    setPending(true)
    try {
      const result = await operation()
      setStatus(result.ok ? kind === 'Publish' ? `Published r${result.new_head} · ${result.receipt_id} · ${result.sha256}`
        : `Restored as r${result.revision}` : result.conflict ? `Someone saved r${result.head}. Reload history and try again.` : `${kind} failed: ${result.detail}`)
      return result
    } finally { setPending(false) }
  }
  return <div hidden={hidden}>
    <h2>History</h2>
    <p role="status">{state.head == null ? 'Unsaved' : `Head r${state.head}`}</p>
    <VersionList variant="drawer" versions={rows} head={state.head} previewingVersion={preview} onPreview={setPreview}
      rowTitle={row => new Date(row.when).toISOString()}
      rowSub={row => row.v === state.published ? `Published r${row.v}` : `${row.author} · ${row.sha?.slice(0, 8)}`}
      restore={{ run: v => run(() => client.restore(docId, v, state.head), 'Restore'), mode: 'restore',
        eligible: (row, isHead) => !isHead, disabled: pending }} rewind={null} />
    <label>Before revision<select aria-label="Before revision" value={before} onChange={event => setBefore(event.target.value)}>
      <option value="">Choose revision</option>{rows.map(row => <option key={row.v} value={row.v}>r{row.v}</option>)}
    </select></label>
    <label>After revision<select aria-label="After revision" value={after} onChange={event => setAfter(event.target.value)}>
      <option value="">Choose revision</option>{rows.map(row => <option key={row.v} value={row.v}>r{row.v}</option>)}
    </select></label>
    <button type="button" disabled={pending || !before || !after} onClick={async () => {
      setPending(true)
      try {
        const result = await client.diff(docId, before, after)
        if (result.ok) { setDiff(result); setStatus('Compared revisions.') }
        else setStatus(`Compare failed: ${result.detail}`)
      } finally { setPending(false) }
    }}>Compare</button>
    {diff && <LabDiffView {...diff} />}
    <button type="button" disabled={pending || state.head == null} onClick={() => run(() => client.publish(docId, state.head), 'Publish')}>Publish r{state.head ?? '?'}</button>
    <p role="status">{status}</p>
    <p>History p50 {percentile(.5)} ms · p99 {percentile(.99)} ms</p>
  </div>
}
