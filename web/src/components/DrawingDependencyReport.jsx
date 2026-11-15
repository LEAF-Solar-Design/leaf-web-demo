export default function DrawingDependencyReport({ report }) {
  if (!report) return null
  const dependencies = Array.isArray(report.dependencies) ? report.dependencies : []
  return <section aria-label="Drawing intake report">
    <h3>Drawing intake report</h3>
    <p>{report.coverage_note || 'Dependencies observed during local intake.'}</p>
    {dependencies.length ? <div className="table-scroll"><table>
      <thead><tr><th>Kind</th><th>Dependency</th><th>Disposition</th></tr></thead>
      <tbody>{dependencies.map((row) => <tr key={`${row.kind}:${row.name}`}>
        <td>{row.kind.replaceAll('_', ' ')}</td><td>{row.name}</td>
        <td>{row.disposition}{row.remapped_to ? ` to ${row.remapped_to}` : ''}</td>
      </tr>)}</tbody>
    </table></div> : <p>No dependencies were present in the local intake records.</p>}
  </section>
}
