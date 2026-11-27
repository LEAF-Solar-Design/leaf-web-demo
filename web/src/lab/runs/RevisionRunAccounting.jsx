import { useEffect, useId, useState } from 'react'
import { discoverRuns } from './runStream.js'

const REVISION_LIMIT = 25

function elapsed(run) {
  if (run.state === 'queued' || run.state === 'running') return 'Pending'
  const start = typeof run.created_at === 'string' ? Date.parse(run.created_at) : NaN
  const end = typeof run.finished_at === 'string' ? Date.parse(run.finished_at) : NaN
  return Number.isFinite(start) && Number.isFinite(end) && end >= start
    ? `${Math.round((end - start) / 1000)} s` : 'Unavailable'
}

function RunWindow({ client, docId, projectId }) {
  const [result, setResult] = useState(null)
  const [retry, setRetry] = useState(0)
  useEffect(() => {
    const controller = new AbortController()
    const load = async () => {
      const runs = await discoverRuns({ docId, projectId, signal: controller.signal })
      if (controller.signal.aborted) return
      if (!runs.ok) { setResult({ unavailable: true }); return }
      const saved = await client.revisions(docId, { limit: REVISION_LIMIT + 1 })
      if (controller.signal.aborted) return
      if (!saved.ok || !Array.isArray(saved.revisions) || saved.revisions.length > REVISION_LIMIT + 1
        || saved.revisions.some(row => row?.doc_id !== docId || !Number.isSafeInteger(row.revision) || row.revision < 1)) {
        setResult({ unavailable: true })
        return
      }
      const revisions = new Map(saved.revisions.slice(0, REVISION_LIMIT).map(row => [row.revision, []]))
      for (const run of runs.runs) {
        if (!revisions.has(run.graph_revision)) revisions.set(run.graph_revision, [])
        revisions.get(run.graph_revision).push(run)
      }
      setResult({ revisions: [...revisions].sort(([a], [b]) => b - a),
        moreRuns: runs.has_more, moreRevisions: saved.revisions.length > REVISION_LIMIT })
    }
    void load().catch(() => {
      if (!controller.signal.aborted) setResult({ unavailable: true })
    })
    return () => controller.abort()
  }, [client, docId, projectId, retry])

  if (!result) return <p role="status">Loading saved revision runs…</p>
  if (result.unavailable) {
    return <><p role="status">Saved revision runs are unavailable.</p><button type="button" onClick={() => {
      setResult(null)
      setRetry(value => value + 1)
    }}>Retry saved revision runs</button></>
  }
  return <>
    <p>Elapsed time includes any wait in the queue. The wall-time cap applies to the worker attempt.</p>
    <p>The report covers submissions visible to your account in this project. Older submissions without a receipt may be absent.</p>
    {result.moreRuns && <p role="status">More runs exist outside this window, including possible failures.</p>}
    {result.moreRevisions && <p>Showing the latest {REVISION_LIMIT} saved revisions and any earlier revision with a visible run.</p>}
    <div style={{ overflowX: 'auto' }}><table>
      <caption>Saved revision runs</caption>
      <thead><tr><th scope="col">Revision</th><th scope="col">Run</th><th scope="col">State</th>
        <th scope="col">Elapsed</th><th scope="col">Attempt cap</th></tr></thead>
      <tbody>{result.revisions.flatMap(([revision, runs]) => runs.length ? runs.map(run => (
        <tr key={run.run_id}>
          <th scope="row">r{revision}</th><td><code>{run.run_id}</code></td>
          <td>{run.wall_cap_hit === true && run.state === 'failed' ? 'Failed · wall-time cap reached' : run.state}</td>
          <td>{elapsed(run)}</td>
          <td>{Number.isSafeInteger(run.attempt_wall_cap_seconds) && run.attempt_wall_cap_seconds > 0
            ? `${run.attempt_wall_cap_seconds} s` : 'Unknown'}</td>
        </tr>
      )) : <tr key={`revision-${revision}`}><th scope="row">r{revision}</th>
        <td colSpan={4}>No run visible in this window.</td></tr>)}</tbody>
    </table></div>
  </>
}

export default function RevisionRunAccounting({ client, docId, projectId }) {
  const [open, setOpen] = useState(false)
  const panel = useId()
  return <section aria-label="Saved revision run accounting">
    <button type="button" aria-expanded={open} aria-controls={panel} onClick={() => setOpen(value => !value)}>
      {open ? 'Hide revision runs' : 'Compare revision runs'}
    </button>
    <div id={panel} hidden={!open}>
      {open && <RunWindow client={client} docId={docId} projectId={projectId} />}
    </div>
  </section>
}
