/**
 * EditSurface — the dormant editing-path skeleton behind cad_edit.
 *
 * Card C1-6 acceptance:
 *   - Mounts only with cad_edit on; with the flag off it does not mount at
 *     all (not a disabled husk).
 *   - Renders the ribbon/command skeleton: the OpenCADStudio interaction
 *     model (grouped ribbon tabs of commands) reimplemented natively for
 *     Leaf. No OpenCADStudio source, assets, or dependency is used here —
 *     this is a from-scratch Leaf Automation component that mirrors the interaction
 *     shape only (see card C1-1's license fence).
 *   - Every command stub reports "not yet enabled" truthfully when invoked:
 *     nothing here performs an edit, and nothing claims to.
 *
 * `enabled` defaults to the VITE_CAD_EDIT build flag (off unless explicitly
 * baked in) so a host page can also drive it directly, e.g. for a test fence.
 */
import { useState } from 'react'

const ENV_CAD_EDIT = import.meta.env?.VITE_CAD_EDIT === '1'

// Ribbon skeleton: tabs of grouped commands, matching the ribbon interaction
// model without any engine wiring behind it — every command is a stub.
const RIBBON = [
  {
    tab: 'Sketch',
    groups: [
      { group: 'Draw', commands: ['Line', 'Rectangle', 'Circle', 'Arc'] },
      { group: 'Constrain', commands: ['Coincident', 'Parallel', 'Dimension'] },
    ],
  },
  {
    tab: 'Modify',
    groups: [
      { group: 'Edit', commands: ['Trim', 'Extend', 'Offset', 'Fillet'] },
    ],
  },
  {
    tab: 'View',
    groups: [
      { group: 'Navigate', commands: ['Fit', 'Pan', 'Zoom'] },
    ],
  },
]

const NOT_YET_ENABLED = (name) => `"${name}" is not yet enabled.`

export default function EditSurface({ enabled = ENV_CAD_EDIT }) {
  const [activeTab, setActiveTab] = useState(RIBBON[0].tab)
  const [status, setStatus] = useState('')

  // cad_edit off — the editing surface is dormant: it does not mount.
  if (!enabled) return null

  const active = RIBBON.find((entry) => entry.tab === activeTab) ?? RIBBON[0]

  const runCommand = (name) => {
    // Every command stub reports its own truthful status — never a silent
    // no-op, and never a claim that the edit happened.
    setStatus(NOT_YET_ENABLED(name))
  }

  return (
    <section className="cad-edit-surface" aria-label="CAD editing surface">
      <div role="tablist" aria-label="Editing ribbon tabs">
        {RIBBON.map(({ tab }) => (
          <button
            key={tab}
            type="button"
            role="tab"
            aria-selected={tab === activeTab}
            onClick={() => setActiveTab(tab)}
          >
            {tab}
          </button>
        ))}
      </div>
      <div role="toolbar" aria-label={`${active.tab} commands`}>
        {active.groups.map(({ group, commands }) => (
          <div key={group} role="group" aria-label={group}>
            <span className="cad-edit-surface__group-label">{group}</span>
            {commands.map((name) => (
              <button
                key={name}
                type="button"
                title={NOT_YET_ENABLED(name)}
                onClick={() => runCommand(name)}
              >
                {name}
              </button>
            ))}
          </div>
        ))}
      </div>
      <LiveRegion as="p" role="status">{status}</LiveRegion>
    </section>
  )
}
