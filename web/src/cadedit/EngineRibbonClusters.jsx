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

export const DRAW_REASONS = Object.freeze({
  noDocument: 'opens on an imported DXF',
  crashed: 'engine stopped: open a drawing again',
  busy: 'engine busy',
})

// W4d Slice B: the Draw group. Each button creates ONE primitive from the
// numeric operands in the row below (no canvas rubber-banding in this slice;
// that is a later interaction wave). The engine validates again and refuses
// with a typed reason; the selection lands on what was just drawn.
const DRAW_OPS = Object.freeze([
  { op: 'createLine', label: 'line', title: 'Draw a line from x,y to x2,y2' },
  { op: 'createPolyline', label: 'polyline', title: 'Draw a polyline through the points listed (x,y pairs)' },
  { op: 'createCircle', label: 'circle', title: 'Draw a circle at x,y with radius r' },
  { op: 'createArc', label: 'arc', title: 'Draw an arc at x,y with radius r from start to end (degrees)' },
])

/** Why the Draw group is unavailable right now, or '' when it is live. Creation needs no selection. */
export function drawReason(session) {
  if (!session) return DRAW_REASONS.noDocument
  if (session.errorKind === SESSION_ERROR.CRASHED) return DRAW_REASONS.crashed
  if (!session.engineParsed) return DRAW_REASONS.noDocument
  if (session.busy) return DRAW_REASONS.busy
  return ''
}

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
  const draw = drawReason(session)
  const save = saveReason(session, canSave)
  const { applyEdit, create } = session.actions

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

  const inputField = (key, label, mode, disabled, wide = false) => (
    <label key={key}>
      {label}
      <input
        className={`ribbon-input${wide ? ' wide' : ''}`}
        type="text"
        inputMode={mode}
        value={inputs[key]}
        onChange={(event) => setInput(key, event.target.value)}
        aria-label={`ribbon ${label}`}
        disabled={disabled}
      />
    </label>
  )
  const modifyInputsOff = !!modify && modify !== MODIFY_REASONS.noSelection
  const drawInputsOff = !!draw

  return (
    <>
      <RibbonCluster id="drawing" label="Drawing">
        {drawingTools.map((tool) => <RibbonTool key={tool.id} tool={tool} />)}
      </RibbonCluster>
      <RibbonCluster id="draw" label="Draw" note={draw || null}>
        {DRAW_OPS.map(({ op, label, title }) => (
          <RibbonTool
            key={op}
            tool={{
              id: `draw:${op}`,
              label,
              title,
              write: true,
              disabled: !!draw,
              reason: draw,
              onClick: () => create(op, inputs),
            }}
          />
        ))}
      </RibbonCluster>
      <RibbonCluster id="modify" label="Modify" note={modify || null}>
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
      {/* ONE operand line under the band (seating: the reference's ribbon
          is a dense row of clusters; operands never live inside a group).
          Both groups read the same record; each half disables with its
          group's own gate. */}
      <div className="ribbon-operands" data-testid="ribbon-operands">
        <span className="ribbon-operands-tag" aria-hidden="true">draw</span>
        {inputField('x', 'x', 'decimal', drawInputsOff)}
        {inputField('y', 'y', 'decimal', drawInputsOff)}
        {inputField('x2', 'x2', 'decimal', drawInputsOff)}
        {inputField('y2', 'y2', 'decimal', drawInputsOff)}
        {inputField('r', 'r', 'decimal', drawInputsOff)}
        {inputField('a0', 'start', 'decimal', drawInputsOff)}
        {inputField('a1', 'end', 'decimal', drawInputsOff)}
        {inputField('pts', 'points', 'text', drawInputsOff, true)}
        <label>
          closed
          <input
            type="checkbox"
            checked={inputs.closed === 'true'}
            onChange={(event) => setInput('closed', event.target.checked ? 'true' : 'false')}
            aria-label="ribbon closed"
            disabled={drawInputsOff}
          />
        </label>
        {inputField('layer', 'layer', 'text', drawInputsOff)}
        <span className="ribbon-operands-tag" aria-hidden="true">modify</span>
        {inputField('dx', 'dx', 'decimal', modifyInputsOff)}
        {inputField('dy', 'dy', 'decimal', modifyInputsOff)}
        {inputField('vertexIndex', 'vertex', 'numeric', modifyInputsOff)}
        {inputField('layer', 'set layer', 'text', modifyInputsOff)}
      </div>
    </>
  )
}
