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

import { RibbonCluster, RibbonTool, RibbonWidget } from '../site/DraftingRibbon.jsx'
import { QuickButton, QUICK_FILE_SLOT_ID } from '../site/CockpitTopBand.jsx'

import { DEFERRED_REASONS, DRAW_REASONS, MODIFY_REASONS, clipboardReason, drawReason, forGroup, modifyReason, propertyReason } from '../lib/actionRegistry.js'

import { ACI_NAMES, LINEWEIGHT_VALUES, admissibleBlockName, buildCreatePayload, buildEditPayload, formatLineweight, readNumber } from './engineSession.js'
import { useEngineSessionContext } from './EngineSessionProvider.jsx'
import { PROMPTS } from './promptKeys.js'
import { isPointExpression } from './pointExpression.js'
import { resolvePromptInputs } from './promptInputs.js'
import ScriptPanel from './ScriptPanel.jsx'

// W4f-6: the store's own number reading (a field the store would take as a
// number), so the outline and the sentence agree; W4f-9 made that reading
// strict ("10abc" is outlined, not read as 10).
const readsAsNumber = (raw) => readNumber(raw) !== null

// The bar-dock's slot the prompt portals into, and the prompt's own id (the
// armed tool's aria-controls target).
export const PROMPT_SLOT_ID = 'cockpit-prompt-slot'
export const PROMPT_ID = 'cockpit-prompt'

const ESC_OWNER_SELECTOR = '[data-escape-owner]'

// W4g-7b-03c-f: the four Modify ops seated in the Properties panel (the
// registry's own `panel === 'properties'` records), the armed prompt's cue
// to read the property ladder instead of the full Modify one.
const PROPERTY_OPS = new Set(forGroup('modify').filter((a) => a.panel === 'properties').map((a) => a.op))

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
// W4g-4b: the reference's small Draw column is engine-backed now (RECTANG,
// ELLIPSE, POINT are registry records); nothing in the Draw panel is a
// placeholder any more.
const DRAW_OFF = Object.freeze([])

// The entity operations the compiled engine performs are registry records
// too (`forGroup('modify')`): the six original ones, COPY, MIRROR, ROTATE,
// SCALE and EXPLODE (W4g-4), OFFSET (W4g-5a), ARRAY's two forms (W4g-5b)
// and the intersection verbs TRIM, EXTEND, FILLET and CHAMFER (W4g-6).
// Nothing in the reference's Modify panel is a placeholder any more.
// W4g-5d: the reference's other Annotation tools stay honest placeholders
// beside the real TEXT (leaders run through APS, W4g-7). W4g-7b-04c: the
// dimensions placeholder leaves now that DIMLINEAR/DIMALIGNED are real
// registry records (draw:dimLinear, draw:dimAligned). W4g-7b-05c: Leader
// carries its own DEFERRED_REASONS sentence, not the generic NOT_IN_ENGINE.
const ANNOTATION_OFF = Object.freeze([
  { id: 'annotation:leader', label: 'Leader', icon: 'leader', size: 'large', reason: DEFERRED_REASONS.leader },
])
// W4g-7b-02c: the reference's Block panel keeps CREATE BLOCK as an honest
// placeholder beside the now-real INSERT BLOCK (ribbonClusters.js drops its
// own placeholder for the latter so the two never both render). W4g-7b-05c:
// its own DEFERRED_REASONS sentence, not the generic NOT_IN_ENGINE.
const BLOCK_OFF = Object.freeze([
  { id: 'block:create', label: 'Create Block', icon: 'block-create', size: 'large', reason: DEFERRED_REASONS.blockCreate },
])
// The datalist id the INSERT name field's `list` attribute points at.
const BLOCK_CATALOGUE_ID = 'cockpit-block-catalogue'

// Shared with the provider so arming resets inputs from this exact grammar.
export { PROMPTS, promptKeys } from './promptKeys.js'

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
  // No dependency list on purpose. A slot that lives inside the tab-switched
  // cluster list (the Clipboard seat, W4g-5c) is unmounted and re-created as
  // a NEW node on every switch away from its tab and back, and a lookup
  // keyed on the id alone kept portaling into the detached original: the
  // panel rendered empty after the first tab switch and the proof's copy
  // click found nothing to click. Resolving after every render and bailing
  // out on the same node costs one getElementById per render and no extra
  // commit; the band and prompt slots are stable nodes and behave as before.
  useEffect(() => {
    if (typeof document === 'undefined') return undefined
    const found = document.getElementById(id) || null
    setNode((prev) => (prev === found ? prev : found))
    return undefined
  })
  return node
}

const offTool = ({ id, label, icon, reason = NOT_IN_ENGINE }, size = 'small') => ({
  id, label, text: label, icon, size, title: label, disabled: true, reason, onClick: () => {},
})

export default function EngineRibbonClusters({ importOpen = false, onToggleImport, panels = ['draw', 'modify'] }) {
  const { session, inputs, setInput, canSave, armed, setArmed, ortho, setOrtho, osnap, setOsnap, reach } = useEngineSessionContext()
  const modify = modifyReason(session, reach)
  // W4g-7b-03c-f: the Properties panel's own ladder, which waives the
  // INSERT-reference rung `modify` still refuses (a property is not
  // geometry; see actionRegistry.js's propertyReason).
  const property = propertyReason(session, reach)
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
  // W4g-7a: the View tab's Script seat, an App cluster the panel portals into.
  const scriptSlot = useSlot('cockpit-script-slot')
  // W4g-4b: the reference's Properties panel sits after Layers and Block;
  // App renders it there (its ByLayer fields still honest placeholders) with
  // a slot the real Match tool is portaled into, the Clipboard idiom.
  const propertiesSlot = useSlot('cockpit-properties-slot')
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
  // W4g-7b-04c-8: every prompt field shares the provider's bounds and arm reset.
  const promptInputs = inputs
  const setPromptInput = setInput
  // W4g-5c: a clipboard arm (PASTE, typed or clicked) reads the clipboard
  // ladder, so the prompt says "nothing on the clipboard yet" with Run held
  // exactly as the ribbon button is held, rather than a live Run that the
  // store then refuses.
  const promptReason = armedGroup === 'draw'
    ? draw
    : armedGroup === 'modify'
      ? (PROPERTY_OPS.has(armedOp) ? property : modify)
      : armedGroup === 'clipboard' ? clipboardReason(session, reach) : ''
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
  // W4g-7a: the resolution lives in promptInputs.js, shared with the script
  // runner, so a script line and a typed prompt read the same numbers.
  const { effective, expressionRefusal, failedExpression, waitingStep, pointSteps } = resolvePromptInputs(prompt, promptInputs, armed && armed.from ? armed.from : null)
  const liveRefusal = prompt && !promptReason && !waitingStep
    ? (expressionRefusal || (armedGroup === 'draw'
      ? buildCreatePayload(armedOp, effective, session.entities.blocks, session.entities.dimstyles)
      : buildEditPayload(armedOp, session.selectedId, effective, session.entities.linetypes, session.entities)).refusal || '')
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

  // W4g-7b-03c: the Properties panel's three combos. Each RUNS AT ONCE on
  // change (a select is its own prompt, no arm/Run round trip); "index..."
  // is the one value that still needs typed input (1..255), so it arms the
  // COLOR word's own prompt instead of guessing a number. buildEditPayload's
  // parseAci/parseLinetype/parseLineweight are the same validators the typed
  // words and MATCHPROP already go through, so a bad pick refuses the same
  // sentence a bad typed value would.
  const selectedEntity = (session.entities || []).find((entity) => entity.id === session.selectedId) || null
  // W4g-7b-03c-g F4 (declared residual, unchanged): this is the browser's
  // LTYPE table, the drafter's own drawing. The server admits only names an
  // entity already uses plus the three standard names (mutation_plan.py
  // ~56-84); a save the server refuses for a name off that list surfaces its
  // sentence unchanged. The select still offers the full table (honest about
  // what is IN the drawing); the title says what the server accepts.
  const linetypeCatalogue = Array.isArray(session.entities?.linetypes) && session.entities.linetypes.length
    ? session.entities.linetypes
    : ['ByLayer', 'ByBlock', 'Continuous']
  const LINETYPE_TITLE = session.entities?.linetypesTruncated === true
    ? "first 200 of the drawing's linetypes; type another name with LT"
    : 'Linetype (this drawing\'s table; a save may still be refused for a name the server does not yet admit)'
  // W4g-7b-03c-g F8/F9: a head value outside the offered options (an ACI in
  // 8..255, a lineweight off the standard mm grid) must still be its OWN
  // option, or the select's `value` matches no `<option>` and the browser
  // shows the first option selected instead — ByLayer becomes unreachable
  // from there, since picking the option that is already (visually) selected
  // fires no change. Every combo's option list gets the current value added
  // when it is not already one of the standard choices.
  const withCurrentOption = (options, current) => (options.includes(current) ? options : [...options, current])
  // W4g-7b-03c-h D2: a true-coloured entity's current value is its own
  // `rgb(r,g,b)` string (formatColor's own reading, no space) — no standard
  // option carries it, so withCurrentOption below adds it as the SELECTED
  // option and every standard option, the nearest index's own name
  // included, becomes a real change that posts setColor and clears the 420.
  const trueColor = selectedEntity && Array.isArray(selectedEntity.trueColor) && selectedEntity.trueColor.length === 3
    ? selectedEntity.trueColor
    : null
  const colorValue = !selectedEntity ? 'ByLayer'
    : trueColor ? `rgb(${trueColor[0]},${trueColor[1]},${trueColor[2]})`
      : selectedEntity.aci === 256 ? 'ByLayer'
        : selectedEntity.aci === 0 ? 'ByBlock'
          : ACI_NAMES[selectedEntity.aci] || `index ${selectedEntity.aci}`
  const linetypeValue = !selectedEntity ? 'ByLayer'
    : linetypeCatalogue.find((name) => name.toLowerCase() === String(selectedEntity.linetype ?? 'ByLayer').toLowerCase())
      || selectedEntity.linetype || 'ByLayer'
  const lineweightValue = !selectedEntity ? 'ByLayer' : formatLineweight(Number.isFinite(selectedEntity.lineweight) ? selectedEntity.lineweight : -1)
  // W4g-7b-03c-g F5: `disabled` already follows the whole ladder (noDocument
  // / crashed / busy / readOnlyKind / noSelection); the displayed sentence
  // must be the SAME rung, not a hardcoded noSelection that lies on every
  // other one. The honesty-ladder gate (check_honesty_ladder.mjs) can only
  // verify a plain string or a literal REASONS.key; a per-rung sentence is a
  // computed value, same class as the Draw/Modify/Clipboard/Annotation/
  // Properties/Block clusters' own `reason = action.when(engineCtx)` above,
  // and like them counts as one more unverifiable-but-budgeted expression.
  const propertyWidgets = [
    {
      id: 'prop-color', label: 'Color', value: colorValue,
      options: withCurrentOption(['ByLayer', 'ByBlock', ...Object.values(ACI_NAMES), 'index...'], colorValue),
      disabled: !!property, reason: property,
      onChange: (value) => (value === 'index...' ? toggleArmed('modify', 'setColor') : applyEdit('setColor', { aci: value })),
    },
    {
      id: 'prop-linetype', label: 'Linetype', value: linetypeValue, title: LINETYPE_TITLE,
      options: withCurrentOption(linetypeCatalogue, linetypeValue),
      disabled: !!property, reason: property,
      onChange: (value) => applyEdit('setLinetype', { linetype: value }),
    },
    {
      id: 'prop-lineweight', label: 'Lineweight', value: lineweightValue,
      options: withCurrentOption(['ByLayer', 'ByBlock', 'Default', ...LINEWEIGHT_VALUES.map(formatLineweight)], lineweightValue),
      disabled: !!property, reason: property,
      onChange: (value) => applyEdit('setLineweight', { lineweight: value }),
    },
  ]

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
    if (key === 'style') {
      return (
        <select
          key={`${key}:${label}`}
          className="cp-input"
          value={promptInputs[key]}
          onChange={(event) => setPromptInput(key, event.target.value)}
          aria-label={`ribbon ${label}`}
          disabled={fieldsOff}
        >
          <option value="">Standard (default)</option>
          {(session.entities.dimstyles || []).map((name) => <option key={name} value={name}>{name}</option>)}
        </select>
      )
    }
    if (mode === 'checkbox') {
      return (
        <label key={`${key}:${label}`} className="cp-field">
          <input
            type="checkbox"
            checked={promptInputs[key] === 'true'}
            onChange={(event) => setPromptInput(key, event.target.checked ? 'true' : 'false')}
            aria-label={`ribbon ${label}`}
            disabled={fieldsOff}
          />
          {label}
        </label>
      )
    }
    // A numeric field that does not read as a number while the command is
    // refused is the one to fix: outlined, and named by the note. A
    // 'decimal-default' field (INSERT's scale and rotation) is blamed only
    // once something was actually typed: empty is its default, not a mistake.
    const invalid = !!liveRefusal && (mode === 'decimal'
      ? (failedExpression.has(key) || !readsAsNumber(effective[key]))
      : mode === 'decimal-default' && String(promptInputs[key] ?? '').trim() !== '' && !readsAsNumber(effective[key]))
    return (
      <input
        key={`${key}:${label}`}
        className={`cp-input${wide ? ' wide' : ''}`}
        type="text"
        inputMode={mode === 'edge' || mode === 'decimal-default' ? (mode === 'decimal-default' ? 'decimal' : 'text') : mode}
        list={key === 'name' ? BLOCK_CATALOGUE_ID : undefined}
        value={promptInputs[key]}
        onChange={(event) => setPromptInput(key, event.target.value)}
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
      {prompt.verb === 'INSERT' && (
        <datalist id={BLOCK_CATALOGUE_ID}>
          {/* W4g-7b-02c-e: offer only names the store would admit (its own
              rule, admissibleBlockName), trimmed the way it would compare
              them — never a name a typed selection would then be refused for. */}
          {(session.entities.blocks || [])
            .filter((b) => b?.complete === true && b.baseUnknown !== true)
            .map((b) => admissibleBlockName(b?.name))
            .filter((name) => name !== null)
            .map((name) => <option key={name} value={name} />)}
        </datalist>
      )}
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
          {forGroup('draw').filter((action) => action.panel === 'draw').map((action) => {
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
          {forGroup('modify').filter((action) => action.panel === 'modify').map((action) => {
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
        </RibbonCluster>
      )}
      {show.has('annotation') && (
        <RibbonCluster id="annotation" label="Annotation" note={draw || null}>
          {forGroup('draw').filter((action) => action.panel === 'annotation').map((action) => {
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
          {ANNOTATION_OFF.map((tool) => <RibbonTool key={tool.id} tool={offTool(tool)} />)}
        </RibbonCluster>
      )}
      {show.has('block') && (
        <RibbonCluster id="block" label="Block" note={draw || null}>
          {forGroup('draw').filter((action) => action.panel === 'block').map((action) => {
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
          {BLOCK_OFF.map((tool) => <RibbonTool key={tool.id} tool={offTool(tool)} />)}
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
      {show.has('properties') && propertiesSlot && createPortal(
        <>
          {forGroup('modify').filter((action) => action.panel === 'properties' && action.op === 'matchprop').map((action) => {
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
          {/* W4g-7b-03c: the three property combos sit on the SAME tools row
              (the #1059 lesson: a portaled seat's controls must be on the
              band, not the separate .ribbon-widgets row under it). */}
          {propertyWidgets.map((widget) => <RibbonWidget key={widget.id} widget={widget} />)}
        </>,
        propertiesSlot,
      )}
      {show.has('script') && scriptSlot && createPortal(<ScriptPanel />, scriptSlot)}
      {promptRow && (promptSlot ? createPortal(promptRow, promptSlot) : promptRow)}
    </>
  )
}
