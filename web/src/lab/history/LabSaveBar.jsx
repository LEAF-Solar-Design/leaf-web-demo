import { useEffect, useRef, useState } from 'react'
import { isValidDocId, labArtifactsClient } from './labArtifactsClient.js'

export default function LabSaveBar({ document, ready, client = labArtifactsClient, onTakeTheirs }) {
  const [, refresh] = useState(0)
  const [pending, setPending] = useState(false)
  const [status, setStatus] = useState('')
  const [docId, setDocId] = useState(() => client.currentDocId())
  const operation = useRef(0)
  const pickerSwitch = useRef(null)
  const generation = client.currentGeneration()
  useEffect(() => {
    setDocId(client.currentDocId())
    const switched = pickerSwitch.current
    // Keep the picker alive across its own switch; external switches cancel it.
    if (switched?.client === client && switched.generation === generation && switched.token === operation.current) return
    operation.current += 1; setPending(false); setStatus('')
  }, [client, generation])
  useEffect(() => client.subscribe(() => refresh(value => value + 1)), [client])
  const state = client.state
  const sorted = [...state.saveTimes].sort((a, b) => a - b)
  const percentile = rank => sorted.length ? Math.round(sorted[Math.ceil(sorted.length * rank) - 1]) : 0
  async function pickDocument(kind) {
    const id = docId
    if (!isValidDocId(id) || pending) return
    const token = ++operation.current
    let activeGeneration = client.currentGeneration()
    let graphToken = client.beginGraphOperation()
    const valid = () => token === operation.current && activeGeneration === client.currentGeneration() && client.isCurrentOperation(graphToken)
    const switchDocument = () => {
      client.setDocId(id)
      activeGeneration = client.currentGeneration()
      graphToken = client.beginGraphOperation()
      pickerSwitch.current = { client, generation: activeGeneration, token }
      setDocId(client.currentDocId())
    }
    setPending(true)
    setStatus('')
    try {
      if (kind === 'Open') switchDocument()
      const head = await client.head(id)
      if (!valid()) return
      if (kind === 'New') {
        if (head.ok && head.revision != null) setStatus(`A graph named ${id} already exists. Choose Open.`)
        else if (head.status === 404) {
          switchDocument()
          setStatus(`New graph ${id}. Save to keep it.`)
        } else setStatus(`New failed: ${head.detail || 'No saved revision.'}`)
      } else if (head.ok && head.revision != null) {
        const result = await client.revision(id, head.revision)
        if (!valid()) return
        if (result.ok) { onTakeTheirs?.(result.graph); setStatus(`Opened ${id} r${head.revision}`) }
        else setStatus(`Open failed: ${result.detail}`)
      } else if (head.status === 404) setStatus(`No saved graph named ${id}. Choose New to start it.`)
      else setStatus(`Open failed: ${head.detail || 'No saved revision.'}`)
    } catch (error) {
      if (valid()) setStatus(`${kind} failed: ${error.message || error}`)
    } finally {
      if (token === operation.current && activeGeneration === client.currentGeneration()) setPending(false)
    }
  }
  async function save(graph, head) {
    const token = ++operation.current
    const valid = () => token === operation.current && generation === client.currentGeneration()
    setPending(true)
    try {
      const result = await client.save(client.currentDocId(), graph, head)
      if (valid()) setStatus(result.ok ? `Saved r${result.revision}` : result.conflict ? 'Save needs a choice.' : `Save failed: ${result.detail}`)
    } finally { if (valid()) setPending(false) }
  }
  function keepMine() {
    if (document != null && !client.state.conflict.captured) client.captureMine(document)
    const { mine, serverHead } = client.state.conflict
    onTakeTheirs?.(mine)
    void save(mine, serverHead)
  }
  async function takeTheirs() {
    client.captureMine(document)
    const graphToken = client.beginGraphOperation()
    const token = ++operation.current
    const valid = () => token === operation.current && client.isCurrentOperation(graphToken)
    setPending(true)
    try {
      const result = await client.revision(client.currentDocId(), state.conflict.serverHead)
      if (!valid()) return
      if (result.ok) { onTakeTheirs?.(result.graph); setStatus(`Loaded r${result.revision}. Your edits are kept.`) }
      else setStatus(`Load failed: ${result.detail}`)
    } finally { if (token === operation.current && generation === client.currentGeneration()) setPending(false) }
  }
  return <section aria-label="Save Strategy">
    <div>
      <label>Graph document <input type="text" maxLength={128} value={docId} onChange={event => setDocId(event.target.value)} /></label>
      <button type="button" disabled={!isValidDocId(docId) || pending} onClick={() => pickDocument('Open')}>Open</button>
      <button type="button" disabled={!isValidDocId(docId) || pending} onClick={() => pickDocument('New')}>New</button>
    </div>
    <button type="button" title={ready && !state.headLoaded ? 'Loading head' : undefined} disabled={!ready || !document || pending || !state.headLoaded} onClick={() => save(document, state.head)}>Save</button>
    {state.headLookupFailed && <button type="button" onClick={() => client.head(client.currentDocId())}>Head unknown, retry</button>}
    <span>{state.head == null ? 'Unsaved' : `Head r${state.head}`}</span>
    {state.published != null && <span>Published r{state.published}</span>}
    {state.lastSaved && <p>Saved r{state.lastSaved.revision} · {state.lastSaved.sha256?.slice(0, 8)}</p>}
    {state.conflict && <div>
      <p role="status">Someone saved r{state.conflict.serverHead} while you edited.</p>
      <button type="button" disabled={pending || state.conflict.serverHead == null} onClick={keepMine}>Keep mine</button>
      <button type="button" disabled={pending || state.conflict.serverHead == null} onClick={takeTheirs}>Take theirs</button>
      <button type="button" disabled={pending || !state.conflict.captured} onClick={() => { client.beginGraphOperation(); onTakeTheirs?.(state.conflict.mine); setStatus('Your edits restored.') }}>Restore my edits</button>
    </div>}
    <p role="status">{status}</p>
    <p>Save p50 {percentile(.5)} ms · p99 {percentile(.99)} ms</p>
  </section>
}
