// The drafting ribbon (W4c-V1): a strip of tool clusters across the top of
// the drawing window, in the utility-CAD cockpit grammar. STUDIO-ONLY by
// construction — App mounts it behind `studioGround && groundShowsDrawing`,
// so the old shell's DOM is byte-for-byte without it.
//
// Every button is a REAL command: it arms the exact run-decision path the
// catalog rail's "Review & run" uses (onRequestRun -> commitCatalogDecision,
// source 'ribbon'), which materializes the confirm strip with the tool's
// schema defaults — nothing auto-runs, nothing is a stub. NEVER dispatchSlash
// here: that stamps slash provenance into the P2 funnel and silently no-ops
// on gated writes (traced: armDecision returns undefined and the controller
// publishes nothing).
//
// Write tools disable under the SAME gates as ToolsPanel (locked by the
// single-writer checkout, or unentitled) with the reason on the title — a
// disabled control with no reason is the gap ToolsPanel's lock-note closed,
// and the ribbon must not reopen it.
import { familyMonogram } from '../lib/surfaceRails.js'

export default function DraftingRibbon({
  families,
  onRequestRun,
  running = false,
  writeLocked = false,
  writeEntitled = true,
  writeLockNote = '',
}) {
  const list = Array.isArray(families) ? families : []
  return (
    <div className="drafting-ribbon" role="toolbar" aria-label="Drafting tools" data-testid="drafting-ribbon">
      {list.length === 0 && (
        // Honest empty (the mock catalog has no solar families): a sentence,
        // never a fabricated cluster.
        <span className="ribbon-empty">No tools for this surface yet.</span>
      )}
      {list.map((fam) => (
        <div key={fam.family_id} className="ribbon-cluster" data-family={fam.family_id}>
          <span className="ribbon-cluster-label" aria-hidden="true">
            <span className="ribbon-monogram">{familyMonogram(fam.label)}</span>
            {fam.label}
          </span>
          <div className="ribbon-cluster-tools">
            {(fam.capabilities || []).map((tool) => {
              const isWrite = (tool.capabilities || []).includes('drawing.write')
              const locked = !!writeLocked && isWrite
              const entBlocked = isWrite && !writeEntitled
              const reason = locked
                ? (writeLockNote || 'another session holds the edit lock')
                : entBlocked
                  ? 'your plan does not include editing tools'
                  : tool.description || tool.name
              return (
                <button
                  key={tool.name}
                  type="button"
                  className={`ribbon-tool${isWrite ? ' write' : ''}`}
                  disabled={running || locked || entBlocked}
                  title={reason}
                  aria-label={`${tool.name}${locked || entBlocked ? ` (unavailable: ${reason})` : ''}`}
                  onClick={() => onRequestRun(
                    tool,
                    null,
                    'Ribbon selection. Confirm the exact tool and parameters before it runs.',
                    'ribbon',
                  )}
                >
                  {tool.name}
                </button>
              )
            })}
          </div>
        </div>
      ))}
    </div>
  )
}
