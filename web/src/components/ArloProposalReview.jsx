import { useEffect, useRef, useState } from 'react'
import { getArloReviews, saveArloReview } from '../api.js'
import './ArloProposalReview.css'

const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i
const number = (value, digits = 2) => Number.isFinite(value) ? value.toLocaleString(undefined, { maximumFractionDigits: digits }) : 'Unavailable'
const point = p => p && ['x', 'y', 'z'].every(axis => Number.isFinite(p[axis]))

function RoutePreview({ proposal }) {
  const routes = (proposal.routes || []).map(route => (route.points || []).filter(point)).filter(points => points.length > 1)
  const placements = (proposal.placements || []).filter(p => point(p.position))
  const points = [...routes.flat(), ...placements.flatMap(p => [p.position, ...(point(p.attachment_position) ? [p.attachment_position] : [])])]
  if (!points.length) return <p>Geometry preview unavailable.</p>
  const project = p => [p.x - .7 * p.y, .42 * (p.x + p.y) - p.z]
  const projected = points.map(project)
  const min = [0, 1].map(axis => Math.min(...projected.map(p => p[axis])))
  const max = [0, 1].map(axis => Math.max(...projected.map(p => p[axis])))
  const scale = Math.min(540 / Math.max(.1, max[0] - min[0]), 190 / Math.max(.1, max[1] - min[1]))
  const xy = p => project(p).map((v, i) => 30 + (v - min[i]) * scale)
  return <figure className="arlo-geometry">
    <svg viewBox="0 0 600 250" role="img" aria-label="Projected solver feeder routes, boxes and support attachments">
      {routes.map((points, i) => <polyline key={i} points={points.map(p => xy(p).join(',')).join(' ')} fill="none" stroke="currentColor" strokeWidth="3" />)}
      {placements.map(p => { const [x,y] = xy(p.position); return <g key={p.id}>
        {p.kind === 'support' && point(p.attachment_position) && <line x1={x} y1={y} x2={xy(p.attachment_position)[0]} y2={xy(p.attachment_position)[1]} stroke="currentColor" strokeWidth="2" />}
        {p.kind === 'box' ? <rect x={x-6} y={y-6} width="12" height="12" fill="none" stroke="currentColor" strokeWidth="2" /> : <circle cx={x} cy={y} r="3" fill="currentColor" />}
      </g> })}
    </svg><figcaption>Projected solver geometry. Squares mark boxes; dots and attachment lines mark supports. Native CAD verification is separate.</figcaption>
  </figure>
}

export default function ArloProposalReview({ envelope, context }) {
  const result = envelope?.solver_result
  const proposals = Array.isArray(result?.proposals) ? result.proposals : []
  const [selectedId, setSelectedId] = useState(proposals[0]?.proposal_id || '')
  const proposal = proposals.find(p => p.proposal_id === selectedId) || proposals[0]
  const [reviews, setReviews] = useState([])
  const [note, setNote] = useState('')
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const [saved, setSaved] = useState('')
  const alive = useRef(false)
  const attempt = useRef(null)
  const bound = ['org_id','project_id','job_id','input_version_id'].every(k => uuid.test(context?.[k] || ''))
    && ['organization_id','project_id','input_version_id'].every((k, i) => envelope?.solver_input?.[k] === context?.[['org_id','project_id','input_version_id'][i]])
  useEffect(() => {
    alive.current = true
    let cancelled = false
    setReviews([]); setSaved(''); setError(''); setLoading(true)
    if (!bound) setLoading(false)
    else getArloReviews(context).then(data => {
      if (cancelled) return
      if (data.job_id !== context.job_id || data.result_sha256 !== envelope.result_sha256) throw new Error('The stored result differs. Reopen this job before reviewing.')
      setReviews(data.reviews); setLoading(false)
    }).catch(cause => { if (!cancelled) { setError(cause.message || 'Review history could not be loaded.'); setLoading(false) } })
    return () => { cancelled = true; alive.current = false }
  }, [bound, context?.job_id, envelope.result_sha256])
  const decide = async decision => {
    const body = { proposal_id: proposal.proposal_id, result_sha256: envelope.result_sha256, decision, note }
    const fingerprint = JSON.stringify(body)
    if (attempt.current?.fingerprint !== fingerprint) attempt.current = { fingerprint, key: crypto.randomUUID() }
    setSaving(true); setError(''); setSaved('')
    try {
      const response = await saveArloReview(context, body, attempt.current.key)
      if (!alive.current) return
      const row = response.review
      if (row?.payload?.jobId !== context.job_id || row.payload.resultHash !== envelope.result_sha256 || row.payload.proposalId !== proposal.proposal_id || row.payload.decision !== decision) throw new Error('The saved decision did not match this proposal. Reload its history.')
      setReviews(previous => [...previous.filter(r => r.operation_id !== row.operation_id), row])
      setSaved(decision === 'accept' ? 'Proposal accepted for the next CAD step.' : 'Proposal rejected.')
      attempt.current = null
    } catch (cause) { if (alive.current) setError(`Decision not confirmed saved. ${cause.message || 'Try again.'}`) }
    finally { if (alive.current) setSaving(false) }
  }
  if (!proposal) return <div className="arlo-review"><h4>Feeder design incomplete</h4><p>No complete proposal is available to review.</p><details><summary>Solver diagnostics</summary><pre>{JSON.stringify(result?.diagnostics || {}, null, 2)}</pre></details></div>
  const selectedReviews = reviews.filter(r => r.payload.proposalId === proposal.proposal_id)
  const complete = result.status === 'complete' && proposal.production_valid === false && Array.isArray(proposal.violations) && proposal.violations.length === 0
  return <div className="arlo-review">
    <p className="dim">Electrical design / Proposal review</p><h4>Review the complete feeder</h4>
    <label>Feeder proposal <select value={proposal.proposal_id} disabled={saving} onChange={e => { setSelectedId(e.target.value); setSaved(''); setNote('') }}>
      {proposals.map((p,i) => <option value={p.proposal_id} key={p.proposal_id}>Proposal {i+1}, {number(p.estimated_installed_cost)} cost units</option>)}
    </select></label>
    <RoutePreview proposal={proposal} />
    <table className="counts"><thead><tr><th>Part</th><th>Quantity</th><th>Installed cost</th></tr></thead><tbody>
      {(proposal.quantities || []).map((q,i) => <tr key={i}><td>{q.kind}</td><td>{number(q.quantity)} {q.unit}</td><td>{number(q.installed_cost)}</td></tr>)}
    </tbody></table><p>Total: <strong>{number(proposal.estimated_installed_cost)} catalog cost units</strong>. Currency was not supplied.</p>
    <p>Accepting records your proposal choice. It does not apply CAD changes or grant engineering approval.</p>
    {!bound && <p>Open this result from its canonical project job to save a decision.</p>}
    {loading && <p role="status">Loading saved review history…</p>}
    {error && <p role="alert">{error}</p>}
    {saved && <p role="status">{saved}</p>}
    <label>Review note <textarea value={note} maxLength={1000} disabled={saving || !bound} onChange={e => setNote(e.target.value)} /></label>
    <div className="arlo-actions"><button type="button" className="btn" disabled={!bound || !complete || loading || saving} onClick={() => decide('accept')}>Accept proposal</button>
      <button type="button" className="btn ghost" disabled={!bound || !complete || loading || saving} onClick={() => decide('reject')}>Reject proposal</button></div>
    <h4>Saved decisions</h4>{selectedReviews.length ? <ol>{selectedReviews.map(row => <li key={row.operation_id}><strong>{row.payload.decision === 'accept' ? 'Accepted' : 'Rejected'}</strong>{row.payload.note && <>: {row.payload.note}</>}<br /><span className="dim">{row.created_at}</span></li>)}</ol> : <p>No saved decision for this proposal.</p>}
    <details><summary>Complete solver decision trace</summary><pre>{JSON.stringify(result.trace, null, 2)}</pre></details>
    <details><summary>Proposal and source detail</summary><pre>{JSON.stringify({ source:proposal.source, proposal_id:proposal.proposal_id, native_verification:proposal.native_verification, result_sha256:envelope.result_sha256 },null,2)}</pre></details>
  </div>
}
