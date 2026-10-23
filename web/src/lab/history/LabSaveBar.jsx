import { useEffect, useState } from 'react'
import { labArtifactsClient } from './labArtifactsClient.js'

export default function LabSaveBar({ document, ready, client = labArtifactsClient, onTakeTheirs }) {
  const [, refresh] = useState(0)
  const [pending, setPending] = useState(false)
  const [status, setStatus] = useState('')
  useEffect(() => client.subscribe(() => refresh(value => value + 1)), [client])
  const state = client.state
  const sorted = [...state.saveTimes].sort((a, b) => a - b)
  const percentile = rank => sorted.length ? Math.round(sorted[Math.ceil(sorted.length * rank) - 1]) : 0
  async function save(graph, head) {
    setPending(true)
    try {
      const result = await client.save(client.currentDocId(), graph, head)
      setStatus(result.ok ? `Saved r${result.revision}` : result.conflict ? 'Save needs a choice.' : `Save failed: ${result.detail}`)
    } finally { setPending(false) }
  }
  async function takeTheirs() {
    setPending(true)
    try {
      const result = await client.revision(client.currentDocId(), state.conflict.serverHead)
      if (result.ok) { onTakeTheirs?.(result.graph); setStatus(`Loaded r${result.revision}. Your edits are kept.`) }
      else setStatus(`Load failed: ${result.detail}`)
    } finally { setPending(false) }
  }
  return <section aria-label="Save Strategy">
    <button type="button" disabled={!ready || !document || pending} onClick={() => save(document, state.head)}>Save</button>
    <span>{state.head == null ? 'Unsaved' : `Head r${state.head}`}</span>
    {state.published != null && <span>Published r{state.published}</span>}
    {state.lastSaved && <p>Saved r{state.lastSaved.revision} · {state.lastSaved.sha256?.slice(0, 8)}</p>}
    {state.conflict && <div>
      <p role="status">Someone saved r{state.conflict.serverHead} while you edited.</p>
      <button type="button" disabled={pending || state.conflict.serverHead == null} onClick={() => save(state.conflict.mine, state.conflict.serverHead)}>Keep mine</button>
      <button type="button" disabled={pending || state.conflict.serverHead == null} onClick={takeTheirs}>Take theirs</button>
      <button type="button" disabled={pending} onClick={() => { onTakeTheirs?.(state.conflict.mine); setStatus('Your edits restored.') }}>Restore my edits</button>
    </div>}
    <p role="status">{status}</p>
    <p>Save p50 {percentile(.5)} ms · p99 {percentile(.99)} ms</p>
  </section>
}
