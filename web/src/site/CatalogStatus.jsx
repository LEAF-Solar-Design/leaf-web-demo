export default function CatalogStatus({ state, query, onRetry, onClear }) {
  if (state === 'loading') {
    return (
      <div role="status" aria-live="polite" className="catalog-status skeleton-stack" style={{ '--skeleton-h': '46px' }}>
        <span className="sr-only" style={{ position: 'absolute', width: 1, height: 1, padding: 0, margin: -1, overflow: 'hidden', clip: 'rect(0, 0, 0, 0)', whiteSpace: 'nowrap', border: 0 }}>Loading tools</span>
        <div className="skeleton-row" aria-hidden="true" />
        <div className="skeleton-row" aria-hidden="true" />
        <div className="skeleton-row" aria-hidden="true" />
      </div>
    )
  }
  if (state === 'empty') {
    return <p className="catalog-status muted">No tools yet. Add a tool to get started.</p>
  }
  if (state === 'no-match') {
    return query ? (
      <>
        <p className="catalog-status muted">No tools match "{query}".</p>
        {onClear && <button type="button" className="chip-neutral dp-focus" onClick={onClear}>Clear filter</button>}
      </>
    ) : <p className="catalog-status muted">No tools for this surface yet.</p>
  }
  if (state === 'failed') {
    return (
      <div role="alert" className="catalog-status">
        <p>The tool list did not load. Your saved tools are still available below.</p>
        <button type="button" className="chip-act dp-focus" onClick={onRetry}>Retry</button>
      </div>
    )
  }
  return null
}
