/**
 * The ribbon's engine clusters (W4d Slice A): Drawing (import, save as a
 * version) and Modify (the six real entity operations the compiled engine
 * already performs), read from the ONE engine session through context.
 *
 * This is a CONSUMER. It constructs no boundary, spawns nothing, and names
 * no worker path (license fence; engineOwnership.test.js counts both shapes).
 * It renders behind ENV_CAD_EDIT at the call site, like every cadedit
 * surface, so a flag-off build folds it away with the provider.
 *
 * HONEST GATING, stated so nobody "fixes" it into a lie: the engine edits an
 * IMPORTED DXF only — the console's server-loaded drawing never enters it
 * (engine reach is the next item, priced in the W4d plan). So on the
 * console's own drawing this group is unavailable, and it SAYS SO: the
 * cluster note and every tool's reason read "opens on an imported DXF" until
 * a document is open, then name the next thing missing (a selection, a
 * busy engine, a crashed worker). A greyed group with no sentence would be
 * the exact gap the askall round refused.
 */
import { RibbonCluster, RibbonTool } from '../site/DraftingRibbon.jsx'

import { SESSION_ERROR } from './engineSession.js'
import { useEngineSessionContext } from './EngineSessionProvider.jsx'

export const MODIFY_REASONS = Object.freeze({
  noDocument: 'opens on an imported DXF',
  crashed: 'engine stopped: open a drawing again',
  busy: 'engine busy',
  noSelection: 'select an entity in the imported DXF',
  readOnlyKind: 'read-only entity kind',
})

export const SAVE_REASONS = Object.freeze({
  noDocument: 'opens on an imported DXF',
  nothingEdited: 'edit something first',
  noTarget: 'download-only here: no project target',
  busy: 'engine busy',
})

const OPS = Object.freeze([
  { op: 'delete', label: 'delete', title: 'Delete the selected entity' },
  { op: 'move', label: 'move', title: 'Move the selected entity by dx, dy' },
  { op: 'moveVertex', label: 'move-vertex', title: 'Move one vertex of the selection by dx, dy' },
  { op: 'addVertex', label: 'add-vertex', title: 'Insert a vertex after the given one, at dx, dy' },
  { op: 'deleteVertex', label: 'delete-vertex', title: 'Delete one vertex of the selection' },
  { op: 'setLayer', label: 'set-layer', title: 'Reassign the selection to the layer named' },
])

/**
 * Why the Modify group is unavailable right now, or '' when it is live.
 * Pure over the session record so the ladder is checkable on its own; the
 * order is the order a user resolves them in.
 */
export function modifyReason(session) {
  if (!session) return MODIFY_REASONS.noDocument
  if (session.errorKind === SESSION_ERROR.CRASHED) return MODIFY_REASONS.crashed
  if (!session.engineParsed) return MODIFY_REASONS.noDocument
  if (session.busy) return MODIFY_REASONS.busy
  if (!session.selected) return MODIFY_REASONS.noSelection
  if (session.selected.editable === false) return MODIFY_REASONS.readOnlyKind
  return ''
}

/** Why "save as a version" is unavailable right now, or '' when it is live. */
export function saveReason(session, canSave) {
  if (!session || !session.engineParsed) return SAVE_REASONS.noDocument
  if (!session.savedBytes) return SAVE_REASONS.nothingEdited
  if (!canSave) return SAVE_REASONS.noTarget
  if (session.busy) return SAVE_REASONS.busy
  return ''
}

export default function EngineRibbonClusters({ importOpen = false, onToggleImport }) {
  const { session, inputs, setInput, canSave } = useEngineSessionContext()
  const modify = modifyReason(session)
  const save = saveReason(session, canSave)
  const { applyEdit } = session.actions

  const drawingTools = [
    {
      id: 'import-dxf',
      label: 'import-dxf',
      title: 'Open a DXF in the browser engine',
      expanded: !!importOpen,
      controls: 'cockpit-import-pane',
      onClick: () => onToggleImport?.(),
    },
    {
      id: 'save-version',
      label: 'save-version',
      title: 'Save the edited bytes to the project as a new version',
      disabled: !!save,
      reason: save,
      onClick: () => { session.actions.save() },
    },
  ]

  const inputField = (key, label, mode) => (
    <label key={key}>
      {label}
      <input
        className="ribbon-input"
        type="text"
        inputMode={mode}
        value={inputs[key]}
        onChange={(event) => setInput(key, event.target.value)}
        aria-label={`ribbon ${label}`}
        disabled={!!modify && modify !== MODIFY_REASONS.noSelection}
      />
    </label>
  )

  return (
    <>
      <RibbonCluster id="drawing" label="Drawing">
        {drawingTools.map((tool) => <RibbonTool key={tool.id} tool={tool} />)}
      </RibbonCluster>
      <RibbonCluster
        id="modify"
        label="Modify"
        note={modify || null}
        extra={(
          <div className="ribbon-cluster-inputs" data-testid="ribbon-modify-inputs">
            {inputField('dx', 'dx', 'decimal')}
            {inputField('dy', 'dy', 'decimal')}
            {inputField('vertexIndex', 'vertex', 'numeric')}
            {inputField('layer', 'layer', 'text')}
          </div>
        )}
      >
        {OPS.map(({ op, label, title }) => (
          <RibbonTool
            key={op}
            tool={{
              id: `modify:${op}`,
              label,
              title,
              write: true,
              disabled: !!modify,
              reason: modify,
              onClick: () => applyEdit(op, inputs),
            }}
          />
        ))}
      </RibbonCluster>
    </>
  )
}
