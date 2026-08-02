import ToolsPanel from './ToolsPanel.jsx'

export default function CapabilityCatalog({
  catalog,
  catalogError,
  openFamilies,
  onToggleFamily,
  onRetryCatalog,
  tools,
  toolsError,
  onRetryTools,
  running,
  selectedTool,
  onRequestRun,
  onOpenTool,
  onReviseTool,
  writeLocked,
  writeEntitled,
}) {
  const families = catalog?.families || []
  const capabilityCount = families.reduce(
    (count, family) => count + (family.capabilities?.length || 0),
    0,
  )

  return (
    <div className="capability-catalog">
      <div className="catalog-summary">
        {families.length} {families.length === 1 ? 'family' : 'families'} · {capabilityCount} capabilities
        {catalog?.source === 'flat-fallback' ? ' · flat fallback' : ''}
      </div>
      {catalogError && (
        <div className="catalog-fallback">
          <div className="inline-error">
            <span>Couldn’t load capability families · {catalogError}</span>
            <button type="button" className="chip-act" onClick={onRetryCatalog}>Retry families</button>
          </div>
          <p className="panel-sub">Showing the flat registered tool list.</p>
          <ToolsPanel
            tools={tools}
            error={toolsError}
            onRetry={onRetryTools}
            running={running}
            selectedTool={selectedTool}
            onRequestRun={onRequestRun}
            onOpenTool={onOpenTool}
            writeLocked={writeLocked}
            writeEntitled={writeEntitled}
          />
        </div>
      )}
      {!catalogError && families.length === 0 && (
        <div className="skeleton-stack" aria-label="Loading capability families">
          <div className="skeleton-row" />
          <div className="skeleton-row" />
          <div className="skeleton-row" />
        </div>
      )}
      {!catalogError && families.map((family) => {
        const open = !!openFamilies?.[family.family_id]
        const count = family.capabilities?.length || 0
        return (
          <section
            className={`section catalog-family ${open ? '' : 'collapsed'}`}
            key={family.family_id}
            aria-labelledby={`catalog-family-${family.family_id}`}
          >
            <button
              id={`catalog-family-${family.family_id}`}
              type="button"
              className="section-head catalog-family-head"
              aria-expanded={open}
              onClick={() => onToggleFamily(family.family_id)}
            >
              <span>{family.label} <span className="n">· {count}</span></span>
              <span className="chev">{open ? 'hide' : 'show'}</span>
            </button>
            {open && (
              <div className="section-body">
                <ToolsPanel
                  tools={family.capabilities || []}
                  subtitle={family.description}
                  error={toolsError}
                  onRetry={onRetryTools}
                  running={running}
                  selectedTool={selectedTool}
                  onRequestRun={onRequestRun}
                  onOpenTool={onOpenTool}
                  onReviseTool={family.family_id === 'custom-authored' || family.family_id === 'custom'
                    ? onReviseTool
                    : undefined}
                  writeLocked={writeLocked}
                  writeEntitled={writeEntitled}
                />
              </div>
            )}
          </section>
        )
      })}
    </div>
  )
}
