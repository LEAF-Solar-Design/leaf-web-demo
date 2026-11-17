import { useState } from 'react'
import { ROLE_PAGE_SIZE, useDrawingRoleRead } from './x07DrawingRoleRead.js'

export default function DrawingRoleProposal(props) {
  const read = useDrawingRoleRead(props)
  if (!read.available) return null
  if (read.status === 'loading') return <p role="status">Reading drawing roles.</p>
  if (read.status === 'error') return <div>
    <p role="status">Drawing roles could not be read.</p>
    <button type="button" onClick={read.retry}>Retry reading roles</button>
  </div>
  return <Proposal key={read.readKey} proposal={read.data} />
}

function Proposal({ proposal }) {
  const [page, setPage] = useState(0)
  const first = proposal.module.first_choice
  const candidates = proposal.module.candidates
  const pages = Math.max(1, Math.ceil(candidates.length / ROLE_PAGE_SIZE))
  const rows = candidates.slice(page * ROLE_PAGE_SIZE, (page + 1) * ROLE_PAGE_SIZE)
  return <section aria-label="Drawing roles">
    <h3>Drawing roles</h3>
    {first ? <p>Module first choice: <strong>{first.block}</strong> on layer <strong>{first.layer}</strong>.</p>
      : <p>No module set was identified.</p>}
    {proposal.module.competing && <p>Multiple module sets need review.</p>}
    <p>{candidates.length} module sets</p>
    {candidates.length > 0 && <table><caption>Module sets</caption>
      <thead><tr><th>Block</th><th>Layer</th><th>Count</th><th>Insertion bounds</th></tr></thead>
      <tbody>{rows.map((item, index) => <tr key={page * ROLE_PAGE_SIZE + index}>
        <td>{item.block}</td><td>{item.layer}</td><td>{item.count}</td>
        <td>{item.insertion_bounds.map(value => value.toFixed(2)).join(', ')}</td>
      </tr>)}</tbody></table>}
    {pages > 1 && <nav aria-label="Module set pages">
      <button type="button" disabled={page === 0} onClick={() => setPage(value => value - 1)}>Previous module sets</button>
      <span role="status">Page {page + 1} of {pages}</span>
      <button type="button" disabled={page + 1 === pages} onClick={() => setPage(value => value + 1)}>Next module sets</button>
    </nav>}
    {['roof', 'obstruction'].map(role => <p key={role}>{role === 'roof' ? 'Roof' : 'Obstruction'} first choice: {
      proposal[role].length ? `${proposal[role][0].block} on layer ${proposal[role][0].layer}` : 'not identified'}.</p>)}
  </section>
}
