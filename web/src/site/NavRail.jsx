// ---------------------------------------------------------------------------
// THE CONSOLE NAV RAIL (standardization slice 4a). App.jsx's inline
// `<aside className="nav">` (App.jsx:2705-2813 before this slice), lifted out
// whole with BYTE-IDENTICAL DOM, class names and testids. Nothing here is a
// redesign: every element, attribute and string below is the one App rendered,
// and src/site/surfaceFrame.render.test.jsx asserts the element sequence
// against a fixture captured from the untouched tree.
//
// The rail is FULLY CONTROLLED: all three folds stay props, against the slice
// plan's line that it should own them locally. Every one of them is driven
// from OUTSIDE the rail today, so hiding any of them in here would have cut a
// live wire while every gate stayed green:
//   - `authorOpen`: SEVEN build-lane paths in App call setAuthorOpen(true)
//     (App.jsx:601, 1027, 1606, 1636, 1744, 1821, 2464), which is how the
//     author panel opens itself when a build starts. A rail-local useState
//     would have left those seven calls referencing a deleted binding, and
//     nothing but a real render would have said so.
//   - `toolsOpen` and `openFamilies`: App reads BOTH outside the rail to pick
//     the keyboard retry target (App.jsx:2037-2053: `anyFamilyOpen` folds
//     openFamilies, and rTarget reads toolsOpen). `openFamilies` is not even
//     App's, the catalog controller owns it. Moving either would have silently
//     moved the R-key's retry rung.
//
// The `«` collapse button keeps its cockpit gate, read from the Surface
// Contract rather than passed as a loose boolean, so the rail decides its own
// chrome from the manifest like every other shell file (slice 2's house rule,
// pinned by src/site/surfaceGates.test.js: no surface id is compared to a
// string literal here).
// ---------------------------------------------------------------------------
import AuthorPanel from '../components/AuthorPanel.jsx'
import ToolsPanel from '../components/ToolsPanel.jsx'

import { surfaceContract } from './productSurfaces.js'

// Collapsible left-rail section (keeps the classic catalog reachable but
// secondary to the prompt box — the primary path). Moved here from App.jsx:190
// with its comment: all three of its mounts are in this rail.
export function Section({ title, count, open, onToggle, children, innerRef, className = '' }) {
  return (
    <div className={`section ${className} ${open ? '' : 'collapsed'}`.replace(/\s+/g, ' ').trim()} ref={innerRef}>
      <button className="section-head" onClick={onToggle} aria-expanded={open}>
        <span>{title}{count != null ? <span className="n"> · {count}</span> : null}</span>
        <span className="chev">{open ? 'hide' : 'show'}</span>
      </button>
      {open && <div className="section-body">{children}</div>}
    </div>
  )
}

export default function NavRail({
  activeSurface,
  studio = null,
  navSpine = false,
  railFamilies = [],
  catalogFamilyCount = 0,
  capCount = 0,
  catalogSource = null,
  catalogErr = null,
  signedOut = false,
  onRetryCatalog = null,
  retryTarget = null,
  tools = [],
  toolsErr = null,
  onRetryTools = null,
  writeLocked = false,
  writeEntitled = false,
  running = false,
  selectedTool = null,
  onRequestRun = null,
  onOpenTool = null,
  onReviseTool = null,
  toolsOpen = false,
  onToggleTools = null,
  openFamilies = null,
  onToggleFamily = null,
  authorOpen = false,
  onToggleAuthor = null,
  onCollapse = null,
  authorSectionRef = null,
  onAuthor = null,
  onPublish = null,
  onUseAuthored = null,
  authorSeed = null,
  authorSignal = null,
  authorAutoSubmit = false,
  authorTargetTool = null,
  onCancelAuthorRevision = null,
  authorStage = null,
  onResumeAuthor = null,
  claudeNotLinked = false,
  onLinkClaude = null,
  buildEntitled = false,
}) {
  // The `«` collapse affordance belongs to the cockpit posture: it exists only
  // where the contract declares a cockpit, ANDed with the studio rail exactly
  // as App.jsx:2721 spelled `studioGround && drafting`.
  const cockpit = !!studio && surfaceContract(activeSurface).chrome.cockpit
  const families = openFamilies || {}
  return (
    <aside className="nav" data-spine={navSpine ? 'hidden' : undefined} aria-hidden={navSpine || undefined}>
      {/* W4c-V1 spine, re-seated in W4d Slice D: on drafting surfaces under
          the studio the rail HIDES behind the band (the reference cockpit
          has no left rail; the band carries the whole catalog and the
          affordances the spine used to carry: `expand` in its Rail group,
          and every family label opens that family). The aside stays in
          the DOM at zero width so the grid keeps its cells. Rail OFF:
          navSpine is false by construction and this branch never renders. */}
      {navSpine ? null : (
      <>
      <div className="fam-title">
        Catalog · {railFamilies.length} famil{railFamilies.length === 1 ? 'y' : 'ies'} · {studio ? railFamilies.reduce((n, f) => n + f.capabilities.length, 0) : capCount} caps
        {catalogSource === 'flat-fallback' ? ' · flat' : ''}
        {cockpit && (
          <button
            type="button"
            className="spine-btn spine-collapse"
            aria-label="Collapse the tool rail to a spine"
            title="Collapse to spine"
            onClick={onCollapse}
          >
            «
          </button>
        )}
      </div>
      {catalogErr && !signedOut && (
        <>
          <div className="inline-error" style={{ margin: '0 4px 4px' }}>
            Couldn’t load families: {catalogErr}
            <button type="button" className="chip-act" onClick={onRetryCatalog}>Retry</button>
            {retryTarget === 'catalog' && <span className="key" aria-hidden="true">R</span>}
          </div>
          <div className="dim" style={{ margin: '0 4px 8px', fontSize: 11.5 }}>Showing the flat tool list instead.</div>
          <Section title="Tools" count={tools.length} open={toolsOpen} onToggle={onToggleTools}>
            <ToolsPanel
              tools={tools}
              writeLocked={writeLocked}
              writeEntitled={writeEntitled}
              error={toolsErr}
              onRetry={onRetryTools}
              retryKey={retryTarget === 'tools'}
              running={running}
              selectedTool={selectedTool}
              onRequestRun={onRequestRun}
              onOpenTool={onOpenTool}
            />
          </Section>
        </>
      )}
      {/* The skeleton reads the WHOLE catalog, not the surface-filtered rail:
          a surface whose familyIds match nothing is honestly empty, not
          loading. App.jsx spelled this `catalog.families.length === 0`. */}
      {!catalogErr && catalogFamilyCount === 0 && (
        // Loading = static content-shaped skeleton rows (no spinner, no text note).
        <div className="skeleton-stack" aria-hidden="true">
          <div className="skeleton-row" />
          <div className="skeleton-row" />
          <div className="skeleton-row" />
        </div>
      )}
      {railFamilies.map((fam) => (
        <Section
          key={fam.family_id}
          title={fam.label}
          count={fam.capabilities.length}
          open={!!families[fam.family_id]}
          onToggle={() => onToggleFamily?.(fam.family_id)}
        >
          <ToolsPanel
            tools={fam.capabilities}
            writeLocked={writeLocked}
            writeEntitled={writeEntitled}
            subtitle={fam.description}
            error={toolsErr}
            onRetry={onRetryTools}
            retryKey={retryTarget === 'tools'}
            running={running}
            selectedTool={selectedTool}
            onRequestRun={onRequestRun}
            onOpenTool={onOpenTool}
            onReviseTool={fam.family_id === 'custom-authored' || fam.family_id === 'custom'
              ? onReviseTool
              : undefined}
          />
        </Section>
      ))}
      <Section
        title="Author a tool"
        className="author-section"
        open={authorOpen}
        onToggle={onToggleAuthor}
        innerRef={authorSectionRef}
      >
        <AuthorPanel
          onAuthor={onAuthor}
          onPublish={onPublish}
          onUseAuthored={onUseAuthored}
          seed={authorSeed}
          seedSignal={authorSignal}
          seedAutoSubmit={authorAutoSubmit}
          targetToolName={authorTargetTool}
          onCancelRevision={onCancelAuthorRevision}
          stageActivity={authorStage}
          onResumeAuthor={onResumeAuthor}
          notLinked={claudeNotLinked}
          onLinkClaude={onLinkClaude}
          buildEntitled={buildEntitled}
        />
      </Section>
      </>
      )}
    </aside>
  )
}
