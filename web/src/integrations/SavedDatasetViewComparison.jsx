function canonical(value) {
  return JSON.stringify(value, (_key, item) => {
    if (!item || typeof item !== 'object' || Array.isArray(item)) return item
    return Object.fromEntries(Object.keys(item).sort().map(key => [key, item[key]]))
  })
}

function pin(reference) {
  return `${reference.id} · version ${reference.version} · ${reference.digest.slice(0, 12)}`
}

function complete(read) {
  return read.next_cursor === null && read.query_coverage?.kind === 'complete_materialization' &&
    read.query_coverage.offset === 0 && read.query_coverage.matching_rows === read.rows.length
}

function describeRows(earlier, latest) {
  if ([earlier, latest].some(read => !['ready', 'empty'].includes(read.status))) {
    return 'Returned rows could not be compared because at least one result was partial, stale, or unavailable.'
  }
  const projection = read => canonical(read.fields.map(field => [field.id, field.type]).sort(([left], [right]) =>
    left < right ? -1 : Number(left > right)))
  if (projection(earlier) !== projection(latest)) return 'Returned rows use different fields or field types.'
  const sameRows = canonical(earlier.rows) === canonical(latest.rows)
  const result = sameRows ? 'Returned rows match in the same order.' : 'Returned rows or their order differ.'
  const counts = `Historical: ${earlier.rows.length}. Current: ${latest.rows.length}.`
  const coverage = complete(earlier) && complete(latest)
    ? 'Both responses cover their complete query results.'
    : 'Comparison covers only the rows returned by the first request for each recipe.'
  return `${result} ${counts} ${coverage}`
}

export function SavedDatasetViewComparison({ historical, latest, earlierRead, latestRead }) {
  const earlier = historical.element
  const current = latest.element
  const titleChanged = earlier.title !== current.title
  const pinChanged = canonical(earlier.dataset_ref) !== canonical(current.dataset_ref)
  const queryChanged = canonical(earlier.query) !== canonical(current.query)
  const displayChanged = earlier.kind !== current.kind || canonical(earlier.props) !== canonical(current.props)

  return <section aria-label="Saved view comparison">
    <h3>Saved view comparison</h3>
    <p>Historical revision {historical.revision} and current revision {latest.revision}. Both recipes query live data now.</p>
    <ul>
      <li>{titleChanged ? <>Title changed from <strong>{earlier.title}</strong> to <strong>{current.title}</strong>.</> : 'Title unchanged.'}</li>
      <li>{pinChanged
        ? <>Dataset pin changed from <span title={earlier.dataset_ref.digest}>{pin(earlier.dataset_ref)}</span> to <span title={current.dataset_ref.digest}>{pin(current.dataset_ref)}</span>.</>
        : <>Dataset pin unchanged. <span title={current.dataset_ref.digest}>{pin(current.dataset_ref)}</span>.</>}</li>
      <li>{queryChanged ? 'Query changed.' : 'Query unchanged.'}</li>
      <li>{displayChanged ? 'Display settings changed.' : 'Display settings unchanged.'}</li>
    </ul>
    <p role="status">{describeRows(earlierRead, latestRead)}</p>
  </section>
}
