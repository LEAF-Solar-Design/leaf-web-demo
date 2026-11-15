const DISPOSITIONS = new Set(['bound', 'detached', 'font remapped', 'image omitted', 'underlay omitted', 'proxy omitted'])

export default function DrawingDependencyReport({ report }) {
  const available = Array.isArray(report?.dependencies)
  const dependencies = available ? report.dependencies : []
  return <section aria-label="Drawing intake report">
    <h3>Drawing intake report</h3>
    <p>{typeof report?.coverage_note === 'string' && report.coverage_note
      ? report.coverage_note : 'Raw dependency coverage is not confirmed.'}</p>
    {!available ? <p>Dependency report unavailable.</p> : <>
    {dependencies.length ? <div className="table-scroll"><table>
      <thead><tr><th>Kind</th><th>Dependency</th><th>Disposition</th></tr></thead>
      <tbody>{dependencies.map((row, index) => {
        const kind = typeof row?.kind === 'string' ? row.kind.replaceAll('_', ' ') : 'Unknown kind'
        const name = typeof row?.name === 'string' && row.name ? row.name : 'Unnamed dependency'
        const disposition = typeof row?.disposition === 'string' && DISPOSITIONS.has(row.disposition)
          ? row.disposition : 'Unknown disposition'
        const target = disposition === 'font remapped' && typeof row?.remapped_to === 'string' ? row.remapped_to : ''
        return <tr key={`${kind}:${name}:${index}`}>
          <td>{kind}</td><td>{name}</td>
          <td>{disposition}{target ? ` to ${target}` : ''}</td>
        </tr>
      })}</tbody>
    </table></div> : <p>No dependencies were present in the local intake records.</p>}
    </>}
  </section>
}
