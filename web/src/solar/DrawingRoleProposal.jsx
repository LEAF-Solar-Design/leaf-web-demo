import { useEffect, useState } from 'react'

export default function DrawingRoleProposal({ projectClient, solarBinding, authorityKey, isCurrent }) {
  const key = JSON.stringify([authorityKey, solarBinding])
  const [view, setView] = useState(null)
  useEffect(() => {
    if (typeof projectClient?.readContext !== 'function' || typeof isCurrent !== 'function' || !isCurrent()) return
    const abort = new AbortController()
    let active = true
    const current = () => active && !abort.signal.aborted && isCurrent()
    Promise.resolve().then(() => projectClient.readContext({ signal: abort.signal })).then(response => {
      if (!current()) return
      const data = response?.ok === true ? response.data?.role_discovery : null
      setView({ key, data: data?.schema === 'leaf.drawing-role-discovery.v1' ? data : null })
    }).catch(() => { if (current()) setView({ key, data: null }) })
    return () => { active = false; abort.abort() }
  }, [key, projectClient, isCurrent])
  if (typeof projectClient?.readContext !== 'function' || !isCurrent?.()) return null
  if (view?.key !== key) return <p role="status">Reading drawing roles.</p>
  const proposal = view.data
  if (!proposal) return <p role="status">Drawing roles could not be read.</p>
  const first = proposal.module?.first_choice
  const candidates = Array.isArray(proposal.module?.candidates) ? proposal.module.candidates : []
  return <section aria-label="Drawing roles">
    <h3>Drawing roles</h3>
    {first ? <p>Module first choice: <strong>{first.block}</strong> on layer <strong>{first.layer}</strong>.</p>
      : <p>No module set was identified.</p>}
    {proposal.module?.competing && <p>Multiple module sets need review.</p>}
    {candidates.length > 0 && <table><caption>Module sets</caption>
      <thead><tr><th>Block</th><th>Layer</th><th>Count</th><th>Insertion bounds</th></tr></thead>
      <tbody>{candidates.map(row => <tr key={JSON.stringify([row.block, row.layer])}>
        <td>{row.block}</td><td>{row.layer}</td><td>{row.count}</td>
        <td>{row.insertion_bounds?.map(value => Number(value).toFixed(2)).join(', ')}</td>
      </tr>)}</tbody></table>}
    {['roof', 'obstruction'].map(role => <p key={role}>{role === 'roof' ? 'Roof' : 'Obstruction'} first choice: {
      proposal[role]?.length ? `${proposal[role][0].block} on layer ${proposal[role][0].layer}` : 'not identified'}.</p>)}
  </section>
}
