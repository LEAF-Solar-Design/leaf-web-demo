import ToolsPanel from './ToolsPanel.jsx'
import { ToolHistoryProvider } from './ToolHistory.jsx'
import { useLoadingPhase } from '../lib/loadingTiming.js'
import { humanizeError } from '../errorHumanize.js'

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
  onCatalogChanged,
  writeLocked,
  writeLockNote = null,
  writeEntitled,
  runDisabled = false,
  runDisabledNote = null,
}) {
  const fallback = catalog?.source === 'flat-fallback'
  const families = catalog?.families || []
  const loadingPhase = useLoadingPhase(!catalogError && families.length === 0)
  const showSkeleton = !catalogError && (loadingPhase === 'shown' || loadingPhase === 'long')
  const capabilityCount = families.reduce(
    (count, family) => count + (family.capabilities?.length || 0),
    0,
  )

  return (
    <ToolHistoryProvider enabled={catalog?.source === 'endpoint' || catalog?.source === 'flat-fallback'}>
    <div className="capability-catalog">
      <div className="catalog-summary">
        {families.length} {families.length === 1 ? 'group' : 'groups'} · {capabilityCount} capabilities
      </div>
      {fallback && <p role="status">Showing general tools. Availability for this drawing could not be checked. Running tools is paused until availability is confirmed. <button type="button" className="chip-act" onClick={onRetryCatalog}>Retry availability</button></p>}
      {catalogError && (
        <div className="catalog-fallback">
          <div className="inline-error">
            <span>Couldn’t load tool groups. {humanizeError(catalogError)}</span>
            <button type="button" className="chip-act" onClick={onRetryCatalog}>Retry families</button>
          </div>
          <p className="panel-sub">Showing general tools. Availability for this drawing could not be checked.</p>
          <ToolsPanel
            tools={tools}
            onCatalogChanged={onCatalogChanged}
            error={toolsError ? humanizeError(toolsError) : null}
            onRetry={onRetryTools}
            running={running}
            selectedTool={selectedTool}
            onRequestRun={onRequestRun}
            onOpenTool={onOpenTool}
            writeLocked={writeLocked}
            writeLockNote={writeLockNote}
            writeEntitled={writeEntitled}
            runDisabled
            runDisabledNote="Availability for this drawing could not be checked. Retry loading the tool groups."
          />
        </div>
      )}
      {showSkeleton && (
        // Match a collapsed family row: 44px header plus its two 1px borders.
        <div className="skeleton-stack" aria-label="Loading capability families" style={{ '--skeleton-h': '46px' }}>
          <div className="skeleton-row" />
          <div className="skeleton-row" />
          <div className="skeleton-row" />
        </div>
      )}
      {!catalogError && !showSkeleton && families.map((family) => {
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
              <span>{fallback ? 'General tools' : family.label} <span className="n">· {count}</span></span>
              <span className="chev">{open ? 'hide' : 'show'}</span>
            </button>
            {open && (
              <div className="section-body">
                <ToolsPanel
                  tools={family.capabilities || []}
                  onCatalogChanged={onCatalogChanged}
                  subtitle={fallback ? 'General tools. Availability for this drawing has not been confirmed.' : family.description}
                  error={toolsError ? humanizeError(toolsError) : null}
                  onRetry={onRetryTools}
                  running={running}
                  selectedTool={selectedTool}
                  onRequestRun={onRequestRun}
                  onOpenTool={onOpenTool}
                  onReviseTool={family.family_id === 'custom-authored' || family.family_id === 'custom'
                    ? onReviseTool
                    : undefined}
                  writeLocked={writeLocked}
                  writeLockNote={writeLockNote}
                  writeEntitled={writeEntitled}
                  runDisabled={runDisabled || fallback}
                  runDisabledNote={fallback ? 'Availability for this drawing could not be checked. Retry availability.' : runDisabledNote}
                />
              </div>
            )}
          </section>
        )
      })}
    </div>
    </ToolHistoryProvider>
  )
}
