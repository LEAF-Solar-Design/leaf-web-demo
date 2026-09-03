/**
 * The ribbon's engine panels (W4d Slice A/B, re-seated in W4e): File (open a
 * DXF, save as a version), Draw (four primitives), Modify (the six real
 * entity operations the compiled engine already performs), read from the
 * ONE engine session through context.
 *
 * This is a CONSUMER. It constructs no boundary, spawns nothing, and names
 * no worker path (license fence; engineOwnership.test.js counts both shapes).
 * It renders behind ENV_CAD_EDIT at the call site, like every cadedit
 * surface, so a flag-off build folds it away with the provider.
 *
 * W4e SEATING: the ribbon shows one tab's panels at a time (`panels`), the
 * quick-access Open/Save buttons live in the top band (a portal into
 * CockpitTopBand's slot, because only this consumer may read the session),
 * and the operand line rides above the command line (a portal into the
 * bar-dock's slot) and is hidden while neither group can take input. Where
 * a slot does not exist (unit tests, no cockpit) both render inline, as
 * before.
 *
 * HONEST GATING, stated so nobody "fixes" it into a lie: the engine edits an
 * IMPORTED DXF only — the console's server-loaded drawing never enters it
 * (engine reach is chipped). So on the console's own drawing these groups
 * are unavailable, and they SAY SO: the panel note and every tool's reason
 * read "opens on an imported DXF" until a document is open, then name the
 * next thing missing (a selection, a busy engine, a crashed worker). The
 * reference's tools this engine has no operation for (rectangle, copy,
 * mirror, ...) are present, disabled, with "not in the browser engine yet".
 */
import { useEffect, useState } from 'react'
import { createPortal } from 'react-dom'

import { RibbonCluster, RibbonTool } from '../site/DraftingRibbon.jsx'
import { QuickButton, QUICK_FILE_SLOT_ID } from '../site/CockpitTopBand.jsx'

import { SESSION_ERROR } from './engineSession.js'
import { useEngineSessionContext } from './EngineSessionProvider.jsx'

export const OPERANDS_SLOT_ID = 'cockpit-operands'

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

const NOT_IN_ENGINE = 'not in the browser engine yet'

// W4d Slice B: the Draw group. Each button creates ONE primitive from the
// numeric operands (no canvas rubber-banding in this slice; that is a later
// interaction wave). The engine validates again and refuses with a typed
// reason; the selection lands on what was just drawn.
const DRAW_OPS = Object.freeze([
  { op: 'createLine', label: 'line', text: 'Line', icon: 'line', title: 'Draw a line from x,y to x2,y2' },
  { op: 'createPolyline', label: 'polyline', text: 'Polyline', icon: 'polyline', title: 'Draw a polyline through the points listed (x,y pairs)' },
  { op: 'createCircle', label: 'circle', text: 'Circle', icon: 'circle', title: 'Draw a circle at x,y with radius r' },
  { op: 'createArc', label: 'arc', text: 'Arc', icon: 'arc', title: 'Draw an arc at x,y with radius r from start to end (degrees)' },
])
// The reference's small Draw column, not in this engine yet.
const DRAW_OFF = Object.freeze([
  { id: 'draw:rectangle', label: 'Rectangle', icon: 'rectangle' },
  { id: 'draw:ellipse', label: 'Ellipse', icon: 'ellipse' },
  { id: 'draw:point', label: 'Point', icon: 'point' },
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
  { op: 'delete', label: 'delete', text: 'Delete', icon: 'delete', title: 'Delete the selected entity' },
  { op: 'move', label: 'move', text: 'Move', icon: 'move', title: 'Move the selected entity by dx, dy' },
  { op: 'moveVertex', label: 'move-vertex', text: 'Move vertex', icon: 'move-vertex', title: 'Move one vertex of the selection by dx, dy' },
  { op: 'addVertex', label: 'add-vertex', text: 'Add vertex', icon: 'add-vertex', title: 'Insert a vertex after the given one, at dx, dy' },
  { op: 'deleteVertex', label: 'delete-vertex', text: 'Delete vertex', icon: 'delete-vertex', title: 'Delete one vertex of the selection' },
  { op: 'setLayer', label: 'set-layer', text: 'Set layer', icon: 'set-layer', title: 'Reassign the selection to the layer named' },
])
// The reference's other six Modify tools, not in this engine yet: with the
// six real ones they fill the reference's 3x4 grid.
const MODIFY_OFF = Object.freeze([
  { id: 'modify:copy', label: 'Copy', icon: 'copy' },
  { id: 'modify:mirror', label: 'Mirror', icon: 'mirror' },
  { id: 'modify:rotate', label: 'Rotate', icon: 'rotate' },
  { id: 'modify:scale', label: 'Scale', icon: 'scale' },
  { id: 'modify:trim', label: 'Trim', icon: 'trim' },
  { id: 'modify:extend', label: 'Extend', icon: 'extend' },
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

// A DOM slot by id, resolved after mount (the band and the bar-dock render
// before the card in App's tree, so the node exists by then); null when the
// cockpit is not mounted, which makes the caller render inline.
function useSlot(id) {
  const [node, setNode] = useState(null)
  useEffect(() => {
    if (typeof document === 'undefined') return undefined
    setNode(document.getElementById(id) || null)
    return undefined
  }, [id])
  return node
}

const offTool = ({ id, label, icon }, size = 'small') => ({
  id, label, text: label, icon, size, title: label, disabled: true, reason: NOT_IN_ENGINE, onClick: () => {},
})

export default function EngineRibbonClusters({ importOpen = false, onToggleImport, panels = ['draw', 'modify'] }) {
  const { session, inputs, setInput, canSave } = useEngineSessionContext()
  const modify = modifyReason(session)
  const draw = drawReason(session)
  const save = saveReason(session, canSave)
  const { applyEdit, create } = session.actions
  const quickSlot = useSlot(QUICK_FILE_SLOT_ID)
  const operandsSlot = useSlot(OPERANDS_SLOT_ID)
  const show = new Set(Array.isArray(panels) ? panels : [])

  const fileTools = [
    {
      id: 'import-dxf',
      label: 'import-dxf',
      text: 'Open DXF',
      icon: 'open',
      size: 'large',
      title: 'Open a DXF in the browser engine',
      expanded: !!importOpen,
      controls: 'cockpit-import-pane',
      onClick: () => onToggleImport?.(),
    },
    {
      id: 'save-version',
      label: 'save-version',
      text: 'Save version',
      icon: 'save',
      size: 'large',
      title: 'Save the edited bytes to the project as a new version',
      disabled: !!save,
      reason: save,
      onClick: () => { session.actions.save() },
    },
  ]
  // The same two commands as quick-access buttons in the top band.
  const quick = fileTools.map((tool) => ({ ...tool, id: `quick-${tool.id}`, label: tool.text }))

  const inputField = (key, label, mode, disabled, wide = false) => (
    <label key={`${key}:${label}`}>
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

  const operands = (
    // ONE operand line for both groups (the reference prompts on the command
    // line; W4e slice H moves these into it). Hidden while neither group can
    // take input, so the band never carries a dead row.
    <div className="ribbon-operands" data-testid="ribbon-operands" hidden={drawInputsOff && modifyInputsOff}>
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
  )

  return (
    <>
      {quickSlot
        ? createPortal(quick.map((tool) => <QuickButton key={tool.id} tool={tool} />), quickSlot)
        : null}
      {(show.has('file') || !quickSlot) && (
        <RibbonCluster id="drawing" label="File">
          {fileTools.map((tool) => <RibbonTool key={tool.id} tool={tool} />)}
        </RibbonCluster>
      )}
      {show.has('draw') && (
        <RibbonCluster id="draw" label="Draw" note={draw || null}>
          {DRAW_OPS.map(({ op, label, text, icon, title }) => (
            <RibbonTool
              key={op}
              tool={{
                id: `draw:${op}`,
                label,
                text,
                icon,
                size: 'large',
                title,
                write: true,
                disabled: !!draw,
                reason: draw,
                onClick: () => create(op, inputs),
              }}
            />
          ))}
          {DRAW_OFF.map((tool) => <RibbonTool key={tool.id} tool={offTool(tool)} />)}
        </RibbonCluster>
      )}
      {show.has('modify') && (
        <RibbonCluster id="modify" label="Modify" note={modify || null}>
          {OPS.map(({ op, label, text, icon, title }) => (
            <RibbonTool
              key={op}
              tool={{
                id: `modify:${op}`,
                label,
                text,
                icon,
                size: 'small',
                title,
                write: true,
                disabled: !!modify,
                reason: modify,
                onClick: () => applyEdit(op, inputs),
              }}
            />
          ))}
          {MODIFY_OFF.map((tool) => <RibbonTool key={tool.id} tool={offTool(tool)} />)}
        </RibbonCluster>
      )}
      {operandsSlot ? createPortal(operands, operandsSlot) : operands}
    </>
  )
}
