import { PRODUCT_SURFACES, SHARED_WORKSPACE_CAPABILITIES, productSurface } from '../site/productSurfaces.js'

function moveProductTab(event) {
  if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return
  const tabs = [...event.currentTarget.querySelectorAll('[role="tab"]')]
  if (!tabs.length) return
  const current = Math.max(0, tabs.indexOf(document.activeElement))
  let next = current
  if (event.key === 'ArrowRight') next = (current + 1) % tabs.length
  if (event.key === 'ArrowLeft') next = (current - 1 + tabs.length) % tabs.length
  if (event.key === 'Home') next = 0
  if (event.key === 'End') next = tabs.length - 1
  event.preventDefault()
  tabs[next].focus()
  tabs[next].click()
}

export default function ProductSurfaceTabs({ activeSurface, states, onSelect }) {
  return (
    <nav className="tc-product-nav" data-cast="tool" aria-label="Product workspace">
      <div className="tc-product-tabs" role="tablist" aria-label="Workspace profile" onKeyDown={moveProductTab}>
        {PRODUCT_SURFACES.map((surface) => {
          const selected = surface.id === activeSurface
          const status = states[surface.id]
          return (
            <button
              key={surface.id}
              id={`product-surface-tab-${surface.id}`}
              type="button"
              role="tab"
              aria-label={surface.label}
              aria-selected={selected}
              aria-controls="product-surface-panel"
              tabIndex={selected ? 0 : -1}
              data-surface={surface.id}
              onClick={() => onSelect(surface.id)}
            >
              <span>{surface.label}</span>
              <small data-state={status.state}>{status.label}</small>
            </button>
          )
        })}
      </div>
      <span className="tc-product-shared" title={SHARED_WORKSPACE_CAPABILITIES.join(', ')}>
        One project, shared Claude, tools, approvals, runs, and receipts
      </span>
    </nav>
  )
}

// Live per-surface projection of the tenant capability catalog (F-7): the
// SAME fold the CAD rail renders (GET /api/capabilities families), filtered
// by the surface's familyIds — never a second data source, never hardcoded
// capability strings. Loading renders a quiet skeleton; a catalog error
// degrades to an honest note (no fake list). Fails closed on absent data.
export function SurfaceCapabilities({ surface, catalog, catalogError }) {
  const families = catalog?.families || []
  const featured = surface.familyIds
    ? families.filter((family) => surface.familyIds.includes(family.family_id))
    : families
  const capabilityCount = featured.reduce(
    (count, family) => count + (family.capabilities?.length || 0),
    0,
  )
  if (catalogError) {
    return (
      <div data-testid="surface-capabilities-error" className="tc-product-note">
        Couldn’t load the live capability catalog · {catalogError}
      </div>
    )
  }
  if (!families.length) {
    return (
      <div className="skeleton-stack" aria-label="Loading capability families" data-testid="surface-capabilities-loading">
        <div className="skeleton-row" />
        <div className="skeleton-row" />
      </div>
    )
  }
  // The catalog is live but none of this surface's featured families are in
  // this tenant's fold yet: say so, and still show the live whole-catalog
  // count — never a bare zero that reads as a broken load.
  if (!featured.length) {
    const totalCount = families.reduce(
      (count, family) => count + (family.capabilities?.length || 0),
      0,
    )
    return (
      <div data-testid="surface-capabilities-live">
        <p className="tc-product-catalog-count">
          No {surface.familyIds.join(' or ')} tools are registered in this tenant’s catalog yet
          · {families.length} {families.length === 1 ? 'family' : 'families'} · {totalCount} capabilities live
        </p>
      </div>
    )
  }
  return (
    <div data-testid="surface-capabilities-live">
      <p className="tc-product-catalog-count">
        {featured.length} {featured.length === 1 ? 'family' : 'families'} · {capabilityCount} capabilities · live tenant catalog
      </p>
      <ul>
        {featured.map((family) => (
          <li key={family.family_id} data-family={family.family_id}>
            <strong>{family.label}</strong>
            {(family.capabilities || []).length > 0 && (
              <span> · {(family.capabilities || []).map((c) => c.label || c.name).join(', ')}</span>
            )}
          </li>
        ))}
      </ul>
    </div>
  )
}

export function ProductSurfaceFrame({ activeSurface, states, projectSlot, onOpenCad, catalog, catalogError }) {
  const surface = productSurface(activeSurface)
  const status = states[surface.id]
  return (
    <section
      id="product-surface-panel"
      className="tc-product-frame"
      role="tabpanel"
      aria-labelledby={`product-surface-tab-${surface.id}`}
      data-cast="tool"
      data-surface={surface.id}
    >
      <div className="tc-product-frame-head">
        <span>{surface.eyebrow}</span>
        <strong>{status.label}</strong>
      </div>
      <h1>{surface.title}</h1>
      <p>{surface.description}</p>
      <div className="tc-product-project">{projectSlot}</div>
      <div className="tc-product-columns">
        <div>
          <h2>Shared everywhere</h2>
          <ul>
            {SHARED_WORKSPACE_CAPABILITIES.map((capability) => <li key={capability}>{capability}</li>)}
          </ul>
        </div>
        <div>
          <h2>{surface.label} capabilities</h2>
          <SurfaceCapabilities surface={surface} catalog={catalog} catalogError={catalogError} />
        </div>
      </div>
      {surface.id === 'browser' && <p className="tc-product-note">Project-scoped files, conversation, and browser composition are live on the shared identity and project rail.</p>}
      {surface.id === 'solar' && <p className="tc-product-note">Solar work runs the shared tenant catalog’s stringing and placement families against the versioned LEAF solar template in the CAD workspace.</p>}
      {surface.id === 'ios' && <p className="tc-product-note">A mounted Apple grant and terminal ship-lane readiness receipt are required. This browser never asks for Apple credentials.</p>}
      <button type="button" className="tc-product-cad-link" onClick={onOpenCad}>
        {surface.id === 'solar' ? 'Open the solar template in the CAD workspace' : 'Open the live CAD workspace'}
      </button>
    </section>
  )
}
