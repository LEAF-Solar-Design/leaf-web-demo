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
 * and a tool's operands are PROMPTED FOR on the command line (slice H): a
 * click on Line ARMS the command, the line above the command input reads
 * "LINE  Specify first point:" with the fields, Enter runs it, Esc cancels,
 * a second click on the tool cancels too. A tool with no operands (delete)
 * runs on click, as ERASE does on a picked selection. Where a slot does not
 * exist (unit tests, no cockpit) the prompt renders inline in the band.
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
import { useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'

import { RibbonCluster, RibbonTool } from '../site/DraftingRibbon.jsx'
import { QuickButton, QUICK_FILE_SLOT_ID } from '../site/CockpitTopBand.jsx'

import { SESSION_ERROR, buildCreatePayload, buildEditPayload } from './engineSession.js'
import { useEngineSessionContext } from './EngineSessionProvider.jsx'

// W4f-6: the store's own number reading (a field that parses to a finite
// number is what a run would take), so the outline and the sentence agree.
const readsAsNumber = (raw) => Number.isFinite(Number.parseFloat(raw))

// The bar-dock's slot the prompt portals into, and the prompt's own id (the
// armed tool's aria-controls target).
export const PROMPT_SLOT_ID = 'cockpit-prompt-slot'
export const PROMPT_ID = 'cockpit-prompt'

const ESC_OWNER_SELECTOR = '[data-escape-owner]'

// Some layers leave focus on the button that opened them. An explicit marker
// lets those layers claim Esc without treating every nonmodal dialog as an
// owner (the guided tour is a dialog but deliberately does not claim Esc).
function hasVisibleEscOwner() {
  return [...document.querySelectorAll(ESC_OWNER_SELECTOR)].some((layer) => {
    if (layer.hidden || layer.hasAttribute('inert') || layer.getAttribute('aria-hidden') === 'true') return false
    if (layer instanceof HTMLDialogElement && !layer.open) return false
    const style = window.getComputedStyle(layer)
    return style.display !== 'none' && style.visibility !== 'hidden'
  })
}

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
 * W4e slice H: the command line's prompt grammar, in the reference's own
 * vocabulary (a VERB, then "Specify …:" steps; words only, nothing copied).
 * A tool listed here ARMS on click and the command line prompts for its
 * operands; Enter (or Run) fires the same create/applyEdit the button used
 * to fire directly. A tool absent here (delete) has no operands and runs on
 * click. Field: [inputKey, label, inputMode, wide]; the accessible name is
 * `ribbon <label>`, the locator contract since W4d, so every existing row
 * still finds its field once the command is armed.
 */
export const PROMPTS = Object.freeze({
  createLine: { verb: 'LINE', steps: [
    { ask: 'Specify first point:', fields: [['x', 'x'], ['y', 'y']] },
    { ask: 'Specify next point:', fields: [['x2', 'x2'], ['y2', 'y2']] },
    { ask: 'Layer:', fields: [['layer', 'layer', 'text']] },
  ] },
  createPolyline: { verb: 'PLINE', steps: [
    { ask: 'Specify points (x,y pairs):', fields: [['pts', 'points', 'text', true]] },
    { ask: 'Close:', fields: [['closed', 'closed', 'checkbox']] },
    { ask: 'Layer:', fields: [['layer', 'layer', 'text']] },
  ] },
  createCircle: { verb: 'CIRCLE', steps: [
    { ask: 'Specify center point:', fields: [['x', 'x'], ['y', 'y']] },
    { ask: 'Specify radius:', fields: [['r', 'r']] },
    { ask: 'Layer:', fields: [['layer', 'layer', 'text']] },
  ] },
  createArc: { verb: 'ARC', steps: [
    { ask: 'Specify center:', fields: [['x', 'x'], ['y', 'y']] },
    { ask: 'Specify radius:', fields: [['r', 'r']] },
    { ask: 'Specify start angle:', fields: [['a0', 'start']] },
    { ask: 'Specify end angle:', fields: [['a1', 'end']] },
    { ask: 'Layer:', fields: [['layer', 'layer', 'text']] },
  ] },
  move: { verb: 'MOVE', steps: [{ ask: 'Specify displacement:', fields: [['dx', 'dx'], ['dy', 'dy']] }] },
  moveVertex: { verb: 'MOVE VERTEX', steps: [
    { ask: 'Specify vertex:', fields: [['vertexIndex', 'vertex', 'numeric']] },
    { ask: 'Specify displacement:', fields: [['dx', 'dx'], ['dy', 'dy']] },
  ] },
  addVertex: { verb: 'ADD VERTEX', steps: [
    { ask: 'Insert after vertex:', fields: [['vertexIndex', 'vertex', 'numeric']] },
    { ask: 'Specify offset:', fields: [['dx', 'dx'], ['dy', 'dy']] },
  ] },
  deleteVertex: { verb: 'DELETE VERTEX', steps: [{ ask: 'Specify vertex:', fields: [['vertexIndex', 'vertex', 'numeric']] }] },
  setLayer: { verb: 'SET LAYER', steps: [{ ask: 'Specify layer:', fields: [['layer', 'set layer', 'text']] }] },
})

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
  const { session, inputs, setInput, canSave, armed, setArmed, ortho, setOrtho, osnap, setOsnap } = useEngineSessionContext()
  const modify = modifyReason(session)
  const draw = drawReason(session)
  const save = saveReason(session, canSave)
  const { applyEdit, create } = session.actions
  const quickSlot = useSlot(QUICK_FILE_SLOT_ID)
  const promptSlot = useSlot(PROMPT_SLOT_ID)
  const show = new Set(Array.isArray(panels) ? panels : [])

  // The armed command (provider state, so it outlives the ribbon's tab
  // remounts). A second click on the armed tool cancels it: a toggle, like
  // the import pane's button. The prompt takes the group's own reason
  // ladder: "select an entity" keeps the FIELDS live (type the operands,
  // pick an entity, run) and gates only Run; busy / no document / crashed
  // disable fields and Run alike, with the sentence. (kimi, #965 review:
  // the first cut disabled the fields for every reason, against this
  // comment; engineSessionProvider.test pins the split.)
  const armedOp = armed ? armed.op : ''
  const armedGroup = armed ? armed.group : ''
  const prompt = armedOp ? PROMPTS[armedOp] : null
  const promptReason = armedGroup === 'draw' ? draw : armedGroup === 'modify' ? modify : ''
  const promptOff = !!promptReason
  const fieldsOff = promptOff && promptReason !== MODIFY_REASONS.noSelection
  // W4f-6: live validation. The store's own payload builders judge the
  // operands as they are typed, with the same sentence a run would refuse
  // with, so Run lights only when the command would go through and the
  // drafter never learns of a bad operand from a refused run. Only while
  // the group itself is live (its reason ladder comes first); the store
  // judges again when the command runs.
  // A numeric operand not yet given (an empty field: the next point of a
  // chained LINE, a cleared radius) is a step still waiting, not a mistake:
  // Run waits quietly with that step's ask as its title, no sentence, no
  // outline, as the reference's prompt simply keeps asking.
  const waitingStep = prompt
    ? prompt.steps.find((step) => step.fields.some(([key, , mode = 'decimal']) => mode === 'decimal' && String(inputs[key] ?? '').trim() === ''))
    : null
  const liveRefusal = prompt && !promptReason && !waitingStep
    ? ((armedGroup === 'draw'
      ? buildCreatePayload(armedOp, inputs)
      : buildEditPayload(armedOp, session.selectedId, inputs)).refusal || '')
    : ''
  const runOff = promptOff || !!liveRefusal || !!waitingStep
  const runReason = promptReason || liveRefusal
  const runHold = runReason || (waitingStep ? waitingStep.ask : '')
  const toggleArmed = (group, op) => setArmed(armedOp === op ? null : { group, op })
  // W4f-3: LINE chains. A run remembers where the segment ends; once the
  // engine has drawn it, that end becomes the next segment's first point.
  const chainRef = useRef(null)
  const run = () => {
    if (!prompt || runOff) return
    chainRef.current = armedOp === 'createLine' ? { x: inputs.x2, y: inputs.y2 } : null
    if (armedGroup === 'draw') create(armedOp, inputs)
    else applyEdit(armedOp, inputs)
  }
  const cancel = () => {
    const toolId = armed ? `${armed.group}:${armed.op}` : ''
    setArmed(null)
    // Focus returns to the tool that armed the command, where the pointer
    // or Tab was before the prompt took it.
    if (toolId && typeof document !== 'undefined') {
      document.querySelector(`.drafting-ribbon [data-tool="${toolId}"]`)?.focus()
    }
  }
  const promptRef = useRef(null)
  useEffect(() => {
    // Arming puts the caret in the first field the way the reference's
    // command line takes typing the moment a command starts.
    chainRef.current = null
    if (!armedOp) return undefined
    promptRef.current?.querySelector('input:not([disabled])')?.focus()
    return undefined
  }, [armedOp])
  useEffect(() => {
    // W4f-2: a run makes the engine busy, which disables Run and the fields,
    // and the browser drops focus to the body. When the engine answers, the
    // caret comes back to the prompt so the next command (or Esc) is one
    // keystroke away. Only when nothing else took the focus in between (the
    // Command bar, a ribbon tool): those keep it.
    if (!armedOp || session.busy || typeof document === 'undefined') return undefined
    // W4f-3: LINE chains, as the reference's LINE keeps asking "Specify next
    // point:" until Esc. After a segment is drawn its end becomes the next
    // segment's first point (the fields and the picker's rubber band alike,
    // through the armed command's chain point) and the caret waits in the
    // next-point field. A refused edit chains nothing.
    const chain = chainRef.current
    chainRef.current = null
    let nextField = 'input:not([disabled])'
    if (chain && armedOp === 'createLine' && session.errorKind === null) {
      const x = Number.parseFloat(chain.x)
      const y = Number.parseFloat(chain.y)
      if (Number.isFinite(x) && Number.isFinite(y)) {
        setInput('x', chain.x)
        setInput('y', chain.y)
        // The next point is not given yet: empty fields, so the prompt keeps
        // asking "Specify next point:" instead of calling the leftover end a
        // degenerate line (W4f-6 live validation).
        setInput('x2', '')
        setInput('y2', '')
        setArmed({ group: 'draw', op: 'createLine', from: [x, y] })
        nextField = '[aria-label="ribbon x2"]:not([disabled])'
      }
    }
    const active = document.activeElement
    if (active && active !== document.body && !promptRef.current?.contains(active)) return undefined
    promptRef.current?.querySelector(nextField)?.focus()
    return undefined
  }, [armedOp, session.busy])
  const cancelRef = useRef(cancel)
  cancelRef.current = cancel
  useEffect(() => {
    // W4f-2: Esc cancels the armed command from ANYWHERE, as the reference's
    // command line drops a command on Esc wherever the pointer is (the
    // drawing, a ribbon tool, the body after a run). Capture phase on the
    // window, so App's window-level Esc rung never also fires for the same
    // key. Esc inside a text field OUTSIDE the prompt keeps that field's own
    // meaning (the Command bar clears itself); the prompt's own fields are
    // handled by the row below.
    if (!armedOp || typeof window === 'undefined') return undefined
    const onWindowKeyDown = (event) => {
      if (event.key !== 'Escape' || event.defaultPrevented) return
      const target = event.target
      if (promptRef.current?.contains(target)) return
      if (target instanceof HTMLElement && (target.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName))) return
      // An open dialog or drawer owns its Esc. Check the whole visible layer
      // stack too because a layer may leave focus on its outside opener.
      if ((target instanceof Element && target.closest('[role="dialog"], [aria-modal="true"], dialog, .drawer-layer')) || hasVisibleEscOwner()) return
      event.preventDefault()
      event.stopPropagation()
      cancelRef.current()
    }
    window.addEventListener('keydown', onWindowKeyDown, true)
    return () => window.removeEventListener('keydown', onWindowKeyDown, true)
  }, [armedOp])
  const onPromptKeyDown = (event) => {
    if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent?.isComposing) {
      // Enter on Run or Cancel keeps the button's own activation (one
      // click, one action); the row's Enter is for the fields only. Without
      // this the row swallowed the keydown and ran the command the operator
      // was cancelling (kimi, #965).
      if (event.target instanceof HTMLButtonElement) return
      event.preventDefault()
      run()
    } else if (event.key === 'Escape') {
      // The prompt owns this Esc: it must not ALSO climb to App's
      // window-level Esc rung (a drawer or route reacting to the same key).
      event.preventDefault()
      event.stopPropagation()
      cancel()
    }
  }
  // The armed tool exposes the prompt it opened; only a tool this table
  // knows can be expanded, so an out-of-contract op never leaves a dangling
  // aria-controls on a button.
  const armedAttrs = (op) => (PROMPTS[op]
    ? { expanded: prompt !== null && armedOp === op, controls: prompt !== null && armedOp === op ? PROMPT_ID : undefined }
    : {})

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
  // W4f slice F: the engine's own Undo / Redo (a bytes-snapshot stack,
  // distinct from the console's version undo on the View tab), each disabled
  // with its reason when there is nothing to step to. They ride the File
  // panel inline and the top band as quick-access buttons, like Open/Save.
  const historyReason = !session.engineParsed
    ? 'opens on an imported DXF'
    : session.busy ? 'engine busy' : ''
  const undoReason = historyReason || (session.undoDepth ? '' : 'nothing to undo')
  const redoReason = historyReason || (session.redoDepth ? '' : 'nothing to redo')
  fileTools.push(
    {
      id: 'undo-edit', label: 'Undo edit', text: 'Undo edit', icon: 'undo', size: 'small',
      title: `Undo the last engine edit${session.undoDepth ? ` (${session.undoDepth} to undo)` : ''}`,
      disabled: !!undoReason, reason: undoReason, onClick: () => { session.actions.undo() },
    },
    {
      id: 'redo-edit', label: 'Redo edit', text: 'Redo edit', icon: 'redo', size: 'small',
      title: `Redo the undone engine edit${session.redoDepth ? ` (${session.redoDepth} to redo)` : ''}`,
      disabled: !!redoReason, reason: redoReason, onClick: () => { session.actions.redo() },
    },
  )
  // The same commands as quick-access buttons in the top band (data-tool
  // "quick-<id>", the band's locator contract).
  const quick = fileTools.map((tool) => ({ ...tool, id: `quick-${tool.id}`, dataTool: `quick-${tool.id}`, label: tool.text }))

  // One field of the prompt: the SAME operator record the pane's fields bind
  // to (provider `inputs`), named `ribbon <label>` for the locator contract.
  const field = ([key, label, mode = 'decimal', wide = false]) => {
    if (mode === 'checkbox') {
      return (
        <label key={`${key}:${label}`} className="cp-field">
          <input
            type="checkbox"
            checked={inputs[key] === 'true'}
            onChange={(event) => setInput(key, event.target.checked ? 'true' : 'false')}
            aria-label={`ribbon ${label}`}
            disabled={fieldsOff}
          />
          {label}
        </label>
      )
    }
    // A numeric field that does not read as a number while the command is
    // refused is the one to fix: outlined, and named by the note.
    const invalid = mode === 'decimal' && !!liveRefusal && !readsAsNumber(inputs[key])
    return (
      <input
        key={`${key}:${label}`}
        className={`cp-input${wide ? ' wide' : ''}`}
        type="text"
        inputMode={mode}
        value={inputs[key]}
        onChange={(event) => setInput(key, event.target.value)}
        aria-label={`ribbon ${label}`}
        aria-invalid={invalid ? 'true' : undefined}
        placeholder={label}
        title={label}
        disabled={fieldsOff}
      />
    )
  }

  const promptRow = prompt ? (
    <div
      id={PROMPT_ID}
      ref={promptRef}
      className="cockpit-prompt"
      data-testid="cockpit-prompt"
      data-op={armedOp}
      role="group"
      aria-label={`${prompt.verb} command`}
      onKeyDown={onPromptKeyDown}
    >
      <span className="cp-verb">{prompt.verb}</span>
      {prompt.steps.map((step) => (
        <span key={step.ask} className="cp-step">
          <span className="cp-ask">{step.ask}</span>
          {step.fields.map(field)}
        </span>
      ))}
      {runReason ? <span className="cp-note" data-testid="cockpit-prompt-note">{runReason}</span> : null}
      <span className="cp-actions">
        {/* W4f-4: the drafting mode the picks obey, the reference's F8. A
            pressed toggle on the prompt (the picker owns the key). */}
        <button
          type="button"
          className="cp-mode"
          data-testid="cockpit-ortho"
          aria-pressed={ortho}
          onClick={() => setOrtho(!ortho)}
          title={`Ortho ${ortho ? 'on' : 'off'}: picks snap to the axis of the larger move from the last point (F8)`}
        >
          ORTHO
        </button>
        {/* W4f-5: object snap, the reference's F3: picks land on the
            document's endpoints, midpoints and centres within reach. */}
        <button
          type="button"
          className="cp-mode"
          data-testid="cockpit-osnap"
          aria-pressed={osnap}
          onClick={() => setOsnap(!osnap)}
          title={`Object snap ${osnap ? 'on' : 'off'}: picks land on endpoints, midpoints and centres within reach (F3)`}
        >
          OSNAP
        </button>
        <button
          type="button"
          className="cp-run"
          data-testid="cockpit-prompt-run"
          onClick={run}
          disabled={runOff}
          title={runOff ? runHold : `Run ${prompt.verb.toLowerCase()} (Enter)`}
          aria-label={runOff ? `Run (unavailable: ${runHold})` : 'Run'}
        >
          Run <kbd className="key">Enter</kbd>
        </button>
        <button type="button" className="cp-cancel" onClick={cancel} title="Cancel the command (Esc)" aria-label="Cancel">
          Cancel <kbd className="key">Esc</kbd>
        </button>
      </span>
    </div>
  ) : null

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
                ...armedAttrs(op),
                onClick: () => (PROMPTS[op] ? toggleArmed('draw', op) : create(op, inputs)),
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
                ...armedAttrs(op),
                onClick: () => (PROMPTS[op] ? toggleArmed('modify', op) : applyEdit(op, inputs)),
              }}
            />
          ))}
          {MODIFY_OFF.map((tool) => <RibbonTool key={tool.id} tool={offTool(tool)} />)}
        </RibbonCluster>
      )}
      {promptRow && (promptSlot ? createPortal(promptRow, promptSlot) : promptRow)}
    </>
  )
}
