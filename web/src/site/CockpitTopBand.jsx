// The reference's top band (W4e slice F): quick-access commands on the left,
// then the ribbon tabs. STUDIO-ONLY by construction: App mounts it inside
// header.top behind `studioGround && drafting`, so rail OFF and every
// non-drafting surface render the header exactly as before.
//
// Honesty: every quick-access button is a real handler App owns, or it is
// disabled with the reason on its title AND accessible name. The engine's
// own Open / Save land in the `#cockpit-quick-file` slot through a portal
// from EngineRibbonClusters (it reads the ONE engine session), so this band
// never touches the session. Tabs that have no real panel set yet are
// disabled with their reason, never a fake tab.
import { moveRovingTab } from '../lib/roving.js'
import CockpitIcon from './CockpitIcon.jsx'

export const QUICK_FILE_SLOT_ID = 'cockpit-quick-file'

export const RIBBON_TABS = Object.freeze([
  { id: 'draw', label: 'Draw' },
  // The reason never says "browser": the product tabs include one named
  // Browser and accessible-name matching is a substring test.
  { id: 'model', label: 'Model', reason: '3D modelling is not in this engine yet' },
  { id: 'insert', label: 'Insert' },
  { id: 'annotate', label: 'Annotate' },
  { id: 'view', label: 'View' },
  { id: 'manage', label: 'Manage' },
])

export function QuickButton({ tool }) {
  const { id, label, icon, title = '', reason = '', disabled = false, expanded, controls, onClick, kind = 'button', dataTool = '' } = tool
  if (kind === 'sep') return <span className="cockpit-quick-sep" aria-hidden="true" />
  const unavailable = disabled && reason
  return (
    <button
      type="button"
      data-quick={id}
      data-tool={dataTool || undefined}
      disabled={disabled}
      title={unavailable ? `${label}: ${reason}` : (title || label)}
      aria-label={unavailable ? `${label} (unavailable: ${reason})` : label}
      aria-expanded={typeof expanded === 'boolean' ? expanded : undefined}
      aria-controls={controls || undefined}
      onClick={onClick}
    >
      <CockpitIcon id={icon} fallback={label} size="quick" />
    </button>
  )
}

export default function CockpitTopBand({ tab = 'draw', onTab, before = [], after = [] }) {
  return (
    <div className="cockpit-band" data-testid="cockpit-band">
      <div className="cockpit-quick" role="toolbar" aria-label="Quick access">
        {before.map((tool) => <QuickButton key={tool.id} tool={tool} />)}
        <span id={QUICK_FILE_SLOT_ID} className="cockpit-quick-slot" />
        {after.map((tool) => <QuickButton key={tool.id} tool={tool} />)}
      </div>
      <div className="cockpit-ribbon-tabs" role="tablist" aria-label="Ribbon" onKeyDown={moveRovingTab}>
        {RIBBON_TABS.map((t) => {
          const selected = t.id === tab
          const off = !!t.reason
          return (
            <button
              key={t.id}
              type="button"
              role="tab"
              id={`ribbon-tab-${t.id}`}
              aria-selected={selected}
              aria-controls="drafting-ribbon"
              tabIndex={selected ? 0 : -1}
              disabled={off}
              title={off ? `${t.label}: ${t.reason}` : t.label}
              aria-label={off ? `${t.label} (unavailable: ${t.reason})` : t.label}
              onClick={() => { if (!off) onTab?.(t.id) }}
            >
              {t.label}
            </button>
          )
        })}
      </div>
    </div>
  )
}
