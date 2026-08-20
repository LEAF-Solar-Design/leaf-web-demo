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

export function ProductSurfaceFrame({ activeSurface, states, projectSlot, onOpenCad }) {
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
          <h2>{surface.label} adds</h2>
          <ul>{surface.additions.map((addition) => <li key={addition}>{addition}</li>)}</ul>
        </div>
      </div>
      {surface.id === 'browser' && <p className="tc-product-note">Project-scoped files, conversation, and browser composition are live on the shared identity and project rail.</p>}
      {surface.id === 'solar' && <p className="tc-product-note">The versioned LEAF solar template is not loaded yet. No shared seed is mutable from this preview.</p>}
      {surface.id === 'ios' && <p className="tc-product-note">A mounted Apple grant and terminal ship-lane readiness receipt are required. This browser never asks for Apple credentials.</p>}
      <button type="button" className="tc-product-cad-link" onClick={onOpenCad}>Open the live CAD workspace</button>
    </section>
  )
}
