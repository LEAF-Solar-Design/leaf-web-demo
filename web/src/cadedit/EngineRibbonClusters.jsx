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
 * read "no drawing in the browser engine yet" (or, since W4g-1b, what the
 * head opener is doing about it) until a document is open, then name the
 * next thing missing (a selection, a busy engine, a crashed worker). The
 * reference's tools this engine has no operation for (rectangle, copy,
 * mirror, ...) are present, disabled, with "not in the browser engine yet".
 */
import { useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'

import { RibbonCluster, RibbonTool } from '../site/DraftingRibbon.jsx'
import { QuickButton, QUICK_FILE_SLOT_ID } from '../site/CockpitTopBand.jsx'

import { DRAW_REASONS, MODIFY_REASONS, drawReason, forGroup, modifyReason } from '../lib/actionRegistry.js'

import { buildCreatePayload, buildEditPayload, readNumber } from './engineSession.js'
import { useEngineSessionContext } from './EngineSessionProvider.jsx'
import { isPointExpression, pointExpressionRefusal, resolvePointExpression } from './pointExpression.js'

// W4f-6: the store's own number reading (a field the store would take as a
// number), so the outline and the sentence agree; W4f-9 made that reading
// strict ("10abc" is outlined, not read as 10).
const readsAsNumber = (raw) => readNumber(raw) !== null

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

// The Draw and Modify vocabulary and their reason ladders moved to the action
// registry with slice 10a (one record behind the ribbon, the engine ops, the
// slash picker and the key ladder). Re-exported unchanged: every importer and
// every pinned assertion in engineSessionProvider.test.jsx reads the same
// frozen objects and the same pure functions it always did. SAVE_REASONS stays
// here — the File panel's save is not an action record (see the registry's
// header for what is deliberately absent).
export { DRAW_REASONS, MODIFY_REASONS, drawReason, modifyReason } from '../lib/actionRegistry.js'

export const SAVE_REASONS = Object.freeze({
  noDocument: 'no drawing in the browser engine yet',
  nothingEdited: 'edit something first',
  noTarget: 'download-only here: no project target',
  busy: 'engine busy: wait for the current edit',
})

const NOT_IN_ENGINE = 'not in the browser engine yet'

// W4d Slice B: the Draw group. Each button creates ONE primitive from the
// numeric operands (no canvas rubber-banding in this slice; that is a later
// interaction wave). The engine validates again and refuses with a typed
// reason; the selection lands on what was just drawn. The four ops are
// registry records now (`forGroup('draw')`), not a literal table here.
// The reference's small Draw column, not in this engine yet.
const DRAW_OFF = Object.freeze([
  { id: 'draw:ellipse', label: 'Ellipse', icon: 'ellipse' },
  { id: 'draw:point', label: 'Point', icon: 'point' },
])

// The entity operations the compiled engine performs are registry records
// too (`forGroup('modify')`): the six original ones, COPY, MIRROR, ROTATE,
// SCALE and EXPLODE (W4g-4), OFFSET (W4g-5a) and ARRAY's two forms (W4g-5b).
// The reference's Modify tools this engine still lacks (the intersection
// verbs, W4g-6) stay honest placeholders.
const MODIFY_OFF = Object.freeze([
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
  createRectangle: { verb: 'RECTANG', steps: [
    { ask: 'Specify first corner point:', fields: [['x', 'x'], ['y', 'y']] },
    { ask: 'Specify other corner point:', fields: [['x2', 'x2'], ['y2', 'y2']] },
    { ask: 'Layer:', fields: [['layer', 'layer', 'text']] },
  ] },
  move: { verb: 'MOVE', steps: [{ ask: 'Specify displacement:', fields: [['dx', 'dx'], ['dy', 'dy']] }] },
  // W4g-4: the reference's verbs, in its words.
  copy: { verb: 'COPY', steps: [{ ask: 'Specify displacement:', fields: [['dx', 'dx'], ['dy', 'dy']] }] },
  mirror: { verb: 'MIRROR', steps: [
    { ask: 'Specify first point of mirror line:', fields: [['x1', 'x1'], ['y1', 'y1']] },
    { ask: 'Specify second point of mirror line:', fields: [['x2', 'x2'], ['y2', 'y2']] },
    { ask: 'Keep source:', fields: [['keep', 'keep source', 'checkbox']] },
  ] },
  rotate: { verb: 'ROTATE', steps: [
    { ask: 'Specify base point:', fields: [['cx', 'cx'], ['cy', 'cy']] },
    { ask: 'Specify rotation angle:', fields: [['deg', 'angle']] },
  ] },
  scale: { verb: 'SCALE', steps: [
    { ask: 'Specify base point:', fields: [['cx', 'cx'], ['cy', 'cy']] },
    { ask: 'Specify scale factor:', fields: [['factor', 'factor']] },
  ] },
  // W4g-5: the reference asks for the distance, then a point on the side the
  // copy goes; the click IS the side, so there is no third step.
  offset: { verb: 'OFFSET', steps: [
    { ask: 'Specify offset distance:', fields: [['dist', 'distance']] },
    { ask: 'Specify point on side to offset:', fields: [['x', 'x'], ['y', 'y']] },
  ] },
  // W4g-5b: the reference asks for the grid, then the spacing; the polar
  // form asks how many and how far round. The count INCLUDES the source in
  // both, which is what a drafter means by "4 copies around a circle".
  arrayRect: { verb: 'ARRAYRECT', steps: [
    { ask: 'Enter number of rows and columns:', fields: [['rows', 'rows', 'numeric'], ['cols', 'columns', 'numeric']] },
    { ask: 'Specify distance between rows and columns:', fields: [['rowGap', 'row spacing'], ['colGap', 'column spacing']] },
  ] },
  arrayPolar: { verb: 'ARRAYPOLAR', steps: [
    { ask: 'Specify centre point of array:', fields: [['cx', 'cx'], ['cy', 'cy']] },
    { ask: 'Enter number of items, including the source:', fields: [['count', 'items', 'numeric']] },
    { ask: 'Specify angle to fill:', fields: [['totalDeg', 'angle to fill']] },
  ] },
  // W4g-5c: PASTE asks where to put it. The record's anchor (a centre for
  // a circle or an arc, the first vertex otherwise) lands on this point.
  pasteClip: { verb: 'PASTE', steps: [
    { ask: 'Specify insertion point:', fields: [['x', 'x'], ['y', 'y']] },
  ] },
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
  const { session, inputs, setInput, canSave, armed, setArmed, ortho, setOrtho, osnap, setOsnap, reach } = useEngineSessionContext()
  const modify = modifyReason(session, reach)
  const draw = drawReason(session, reach)
  const save = saveReason(session, canSave)
  const { applyEdit, create, copyToClipboard, pasteFromClipboard } = session.actions
  const quickSlot = useSlot(QUICK_FILE_SLOT_ID)
  const promptSlot = useSlot(PROMPT_SLOT_ID)
  // W4g-5c: the reference puts Clipboard LAST, and the ribbon renders these
  // engine children BEFORE App's cluster list, so a cluster rendered here
  // would sit third and push the row (it did: the prompt seat moved 148px).
  // App renders the panel at the end and this fills it with the real tools.
  const clipboardSlot = useSlot('cockpit-clipboard-slot')
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
  // W4f-8: a point step's FIRST field may hold the command line's point
  // grammar ("x,y", "@dx,dy", "dist<angle", "@dist<angle") instead of a
  // number. It resolves against the step's anchor (the previous point: the
  // chain point for a first point, the first point for a next point, the
  // origin for a displacement) into both fields' EFFECTIVE values, which is
  // what validation and the run read; the typed text stays in the field
  // until the run commits the numbers, so the record and the chain carry
  // plain numbers, as a pick would. A step is a point step when it asks for
  // exactly two decimal operands.
  const pointSteps = prompt
    ? prompt.steps.filter((step) => step.fields.length === 2 && step.fields.every(([, , mode = 'decimal']) => mode === 'decimal'))
    : []
  const effective = { ...inputs }
  let expressionRefusal = ''
  // The fields whose expression did not resolve: outlined by name (a
  // malformed pair still parseFloats to its first number, so the numeric
  // test alone would not blame it).
  const failedExpression = new Set()
  let previousPoint = armed && armed.from ? [armed.from[0], armed.from[1]] : null
  for (const step of pointSteps) {
    const [[kx], [ky]] = step.fields
    const isDelta = kx === 'dx'
    const anchor = isDelta ? [0, 0] : previousPoint
    const raw = inputs[kx]
    if (isPointExpression(raw)) {
      const point = resolvePointExpression(raw, anchor)
      if (point) { effective[kx] = String(point[0]); effective[ky] = String(point[1]) } else {
        failedExpression.add(kx)
        if (!expressionRefusal) expressionRefusal = `${prompt.verb} refused: ${pointExpressionRefusal(raw, anchor)}`
      }
    }
    if (!isDelta) {
      const px = Number.parseFloat(effective[kx])
      const py = Number.parseFloat(effective[ky])
      if (Number.isFinite(px) && Number.isFinite(py)) previousPoint = [px, py]
    }
  }
  const waitingStep = prompt && !expressionRefusal
    ? prompt.steps.find((step) => step.fields.some(([key, , mode = 'decimal']) => mode === 'decimal' && String(effective[key] ?? '').trim() === ''))
    : null
  const liveRefusal = prompt && !promptReason && !waitingStep
    ? (expressionRefusal || (armedGroup === 'draw'
      ? buildCreatePayload(armedOp, effective)
      : buildEditPayload(armedOp, session.selectedId, effective)).refusal || '')
    : ''
  const runOff = promptOff || !!liveRefusal || !!waitingStep
  const runReason = promptReason || liveRefusal
  const runHold = runReason || (waitingStep ? waitingStep.ask : '')
  const toggleArmed = (group, op) => setArmed(armedOp === op ? null : { group, op })
  // The one context the Draw and Modify records read: the session their reason
  // ladders judge, and the single activation handler they name. Arming vs.
  // running is the CONSUMER's decision (a tool with operands opens the command
  // prompt; one without runs on click), which is why the record hands the op
  // back rather than dispatching itself.
  const engineCtx = {
    session,
    // W4g-1b: the reach state, so a record's reason says what the panel
    // note says while the console's own drawing is opening (or failed to).
    reach,
    onActivate: (group, op) => {
      if (PROMPTS[op]) { toggleArmed(group, op); return }
      if (group === 'draw') create(op, inputs)
      // W4g-5c: CUT and COPY touch no engine op at all, so they are neither
      // a create nor an edit; PASTE arms its base-point prompt above.
      else if (op === 'copyClip' || op === 'cutClip') copyToClipboard(op === 'cutClip')
      else applyEdit(op, inputs)
    },
  }
  // W4f-3: LINE chains. A run remembers where the segment ends; once the
  // engine has drawn it, that end becomes the next segment's first point.
  const chainRef = useRef(null)
  const run = () => {
    if (!prompt || runOff) return
    // Commit resolved expressions as numbers before the engine sees them:
    // the fields, the record and the chain all carry what was drawn.
    for (const step of pointSteps) {
      const [[kx], [ky]] = step.fields
      if (isPointExpression(inputs[kx])) { setInput(kx, effective[kx]); setInput(ky, effective[ky]) }
    }
    chainRef.current = armedOp === 'createLine' ? { x: effective.x2, y: effective.y2 } : null
    if (armedGroup === 'draw') create(armedOp, effective)
    else if (armedOp === 'pasteClip') pasteFromClipboard(effective)
    else applyEdit(armedOp, effective)
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
    ? MODIFY_REASONS.noDocument
    : session.busy ? MODIFY_REASONS.busy : ''
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
    const invalid = mode === 'decimal' && !!liveRefusal && (failedExpression.has(key) || !readsAsNumber(effective[key]))
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
          {forGroup('draw').map((action) => {
            // The record's reason, read ONCE per record per render: it is the
            // disabled flag and the sentence both.
            const reason = action.when(engineCtx)
            return (
              <RibbonTool
                key={action.op}
                tool={{
                  id: action.id,
                  label: action.label,
                  text: action.text,
                  icon: action.icon,
                  size: action.size,
                  title: action.title(engineCtx),
                  write: action.write,
                  disabled: !!reason,
                  reason,
                  ...armedAttrs(action.op),
                  onClick: () => action.run(engineCtx),
                }}
              />
            )
          })}
          {DRAW_OFF.map((tool) => <RibbonTool key={tool.id} tool={offTool(tool)} />)}
        </RibbonCluster>
      )}
      {show.has('modify') && (
        <RibbonCluster id="modify" label="Modify" note={modify || null}>
          {forGroup('modify').map((action) => {
            // The record's reason, read ONCE per record per render: it is the
            // disabled flag and the sentence both.
            const reason = action.when(engineCtx)
            return (
              <RibbonTool
                key={action.op}
                tool={{
                  id: action.id,
                  label: action.label,
                  text: action.text,
                  icon: action.icon,
                  size: action.size,
                  title: action.title(engineCtx),
                  write: action.write,
                  disabled: !!reason,
                  reason,
                  ...armedAttrs(action.op),
                  onClick: () => action.run(engineCtx),
                }}
              />
            )
          })}
          {MODIFY_OFF.map((tool) => <RibbonTool key={tool.id} tool={offTool(tool)} />)}
        </RibbonCluster>
      )}
      {show.has('clipboard') && clipboardSlot && createPortal(
        forGroup('clipboard').map((action) => {
          const reason = action.when(engineCtx)
          return (
            <RibbonTool
              key={action.op}
              tool={{
                id: action.id,
                label: action.label,
                text: action.text,
                icon: action.icon,
                size: action.size,
                title: action.title(engineCtx),
                write: action.write,
                disabled: !!reason,
                reason,
                ...armedAttrs(action.op),
                onClick: () => action.run(engineCtx),
              }}
            />
          )
        }),
        clipboardSlot,
      )}
      {promptRow && (promptSlot ? createPortal(promptRow, promptSlot) : promptRow)}
    </>
  )
}
