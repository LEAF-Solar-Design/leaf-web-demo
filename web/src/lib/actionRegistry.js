// THE ACTION RECORD (standardization slice 10a). One record per action, read
// by every surface that offers it: the drafting ribbon (ribbonClusters.js),
// the drafting engine's Draw/Modify op tables (EngineRibbonClusters.jsx), the
// composer's "/" command picker (composer.js + PromptBox.jsx), and App.jsx's
// global key ladder. Before this file those were five independent code paths
// and an action's label, icon, reason, keyboard cap and run handler were
// written out once per path with nothing holding them together.
//
// PURE AND REACT-FREE by construction: no JSX, no hooks, no DOM writes, no
// import that reaches React. `run` never performs the effect itself — it names
// the handler on the caller's context and passes the operands. That is what
// lets a ribbon button, a picker row and a keystroke reach the SAME record
// without the record knowing which one called.
//
// HONESTY CONTRACT (inherited verbatim from ribbonClusters.js's header, which
// is where this vocabulary was first written down): every action that can be
// taken away carries the sentence that says WHY. `when(ctx)` returns '' when
// the action is live and the exact reason string otherwise — the same strings
// the ribbon renders as `<label> (unavailable: <reason>)`. A reason that is
// not one of the frozen maps below is a contract break. validateRegistry holds
// the part of that a module load can hold: every `when` is a function, is
// total over the empty context, and names no sentence outside the maps for
// that context. A function's range cannot be enumerated at load, so the
// guarantee across shell states is the sixteen-context probe in
// actionRegistry.test.js ("never names a reason outside the registered
// vocabulary, in any context"). The honesty-ladder gate
// (scripts/check_honesty_ladder.mjs) reads the maps in this file the same way
// it reads every other `*REASONS` map.
//
// HONEST TRIGGERS, the point of the slice. `triggers` records what an action
// can ACTUALLY be reached by today, never what it should be reachable by:
//   mouse:    'click'   a real click handler exists.        null when none.
//   keyboard: 'kbd'     a key in the global ladder fires it (see `kbd`).
//             'enter'   Enter on the focused row runs it (the "/" picker).
//             null      no keyboard path of its own. A plain <button> still
//                       answers Enter/Space because the platform maps them to
//                       click; that is the mouse trigger's DOM affordance, not
//                       a keyboard trigger this registry declares.
//   touch:    'tap'     a plain click target (a <button>, or the "/" picker's
//                       div[role=option] row): a tap IS a click, no bespoke
//                       handler.
//             'pointer' real pointer* handlers (only CanvasPointPicker.jsx).
//             null      no touch path.
// `kbd` is null for every action that has no shortcut today. Slice 10b assigns
// shortcuts with the design session; this file records the absence honestly
// rather than inventing caps nobody bound.
//
// WHAT IS DELIBERATELY NOT HERE, so nobody reads the absence as an oversight:
//   - Catalog tools. They are per-tenant DATA (GET /api/catalog), not a fixed
//     record set; ribbonClusters.js's familyCluster still projects them, using
//     this file's REASONS for its gate.
//   - The reference panels (Annotation, Block, Properties, Groups, Clipboard)
//     and the engine's DRAW_OFF / MODIFY_OFF rows. They are present, disabled
//     and say "not in the browser engine yet"; they name no operation, so
//     there is no action to register.
//   - The engine mini-form's Run / Cancel. Their availability is decided by
//     live payload validation against the operands being typed (a refusal
//     sentence built by the store, not a fixed reason), so `when(ctx)` cannot
//     answer for them without moving that validation here. Recorded as a gap.
//   - catalogRouting.js's typed tool-name router. It resolves catalog TOOLS, a
//     different record; conflating the two would give one id two meanings.

// The worker-death errorKind, spelled once. engineSession.js owns the value;
// this module cannot import it (that file is a React hook module and this one
// is React-free), so engineSessionErrors.js holds the constant both read.
import { SESSION_ERROR } from '../cadedit/engineSessionErrors.js'

// The ribbon's reason vocabulary. Lived in ribbonClusters.js until this slice;
// it moved here because `when` is the registry's half of the honesty contract
// and a cycle (registry -> ribbon -> registry) would leave it in the temporal
// dead zone. ribbonClusters.js re-exports it, so every existing importer and
// every pinned test reads the same frozen object it always did.
export const REASONS = Object.freeze({
  writeLocked: 'another session holds the edit lock',
  writeUnentitled: 'your plan does not include editing tools',
  buildUnentitled: 'your plan does not include authoring tools',
  buildUnavailable: 'the authoring stage is off on this deployment',
  noDrawing: 'no drawing loaded',
  noVersions: 'no versioned drawing',
  versionBusy: 'a version change is in flight',
  running: 'a run is in flight',
  previewing: 'viewing a version, read-only',
  mutationsBlocked: 'edits are blocked on this drawing',
  nothingToUndo: 'nothing to undo',
  nothingToRedo: 'nothing to redo',
  notInEngine: 'not in the browser engine yet',
  // W4g-2 (one head): a write tool would move the server head under a
  // browser copy that holds edits nobody saved; the drafter decides which
  // survives before anything runs.
  unsavedEngineEdits: 'the browser engine holds unsaved edits: save or discard them first',
  // The publish settled in the author card but the catalog has not issued the
  // tool a digest yet, so there is nothing runnable to arm. Same sentence the
  // one honest resolver (site/publishedCatalogTool.js) fails with.
  publishing: 'publishing: not in the runnable catalog yet',
  // Standardization slice 8c: a record carrying mcp_source (a tool projected
  // from a connected MCP server) has no run path on any surface yet — the
  // projection itself is stubbed to emit nothing until a later slice.
  mcpToolNotWired: 'a tool from a connected service cannot run here yet',
})

// W4g-1b (engine reach): the console's own drawing opens in the engine at
// mount, so "no document" is a state, not "import something first".
export const DRAW_REASONS = Object.freeze({
  noDocument: 'no drawing in the browser engine yet',
  crashed: 'engine stopped: open a drawing again',
  busy: 'engine busy: wait for the current edit',
})

export const MODIFY_REASONS = Object.freeze({
  noDocument: 'no drawing in the browser engine yet',
  crashed: 'engine stopped: open a drawing again',
  busy: 'engine busy: wait for the current edit',
  noSelection: 'select an entity in the drawing',
  readOnlyKind: 'read-only entity kind',
})

// W4g-5c: the clipboard's ladder. CUT and COPY answer to the Modify ladder
// (they act on a selection); PASTE does not need a selection at all, it needs
// a record on the clipboard, so it has its own last rung.
export const CLIPBOARD_REASONS = Object.freeze({
  empty: 'nothing on the clipboard yet',
})

// W4g-1b: while the engine holds no document, the reach state (the provider's
// record of opening the console's own drawing) names what is happening, or
// why it could not happen, instead of the bare "no drawing" sentence. Only
// the opening and failed states speak; open or idle leaves the ladder's own.
function reachSentence(reach) {
  if (!reach || typeof reach.sentence !== 'string' || !reach.sentence) return ''
  return reach.state === 'opening' || reach.state === 'failed' ? reach.sentence : ''
}

// The global key ladder's own vocabulary. A ladder rung with nothing to act on
// is not an error — it is an action that is not available right now, and it
// owes the same sentence a greyed button owes.
export const LADDER_REASONS = Object.freeze({
  nothingOpen: 'nothing open to close right now',
  nothingToRetry: 'no failed step to retry right now',
  retryOwnedByResult: 'the result panel owns this retry',
})

/** Every sentence this registry is allowed to hand a renderer. */
export const KNOWN_REASON_VALUES = Object.freeze(new Set([
  ...Object.values(REASONS),
  ...Object.values(DRAW_REASONS),
  ...Object.values(MODIFY_REASONS),
  ...Object.values(CLIPBOARD_REASONS),
  ...Object.values(LADDER_REASONS),
]))

// --- reason ladders --------------------------------------------------------
//
// Pure over the engine session record, so a ladder is checkable on its own.
// The order is the order a drafter resolves the blockers in.

/** Why the Draw group is unavailable right now, or '' when it is live. Creation needs no selection. */
export function drawReason(session, reach = null) {
  if (!session) return reachSentence(reach) || DRAW_REASONS.noDocument
  if (session.errorKind === SESSION_ERROR.CRASHED) return DRAW_REASONS.crashed
  if (!session.engineParsed) return reachSentence(reach) || DRAW_REASONS.noDocument
  if (session.busy) return DRAW_REASONS.busy
  return ''
}

/** Why the Modify group is unavailable right now, or '' when it is live. */
export function modifyReason(session, reach = null) {
  if (!session) return reachSentence(reach) || MODIFY_REASONS.noDocument
  if (session.errorKind === SESSION_ERROR.CRASHED) return MODIFY_REASONS.crashed
  if (!session.engineParsed) return reachSentence(reach) || MODIFY_REASONS.noDocument
  if (session.busy) return MODIFY_REASONS.busy
  if (!session.selected) return MODIFY_REASONS.noSelection
  if (session.selected.editable === false) return MODIFY_REASONS.readOnlyKind
  return ''
}

/**
 * PASTE's ladder: everything the document itself must satisfy (through the
 * draw ladder, since a paste CREATES and needs no selection), then the
 * clipboard's own emptiness.
 */
export function clipboardReason(session, reach = null) {
  const document = drawReason(session, reach)
  if (document) return document
  if (!session?.clipboard) return CLIPBOARD_REASONS.empty
  return ''
}

/**
 * The version cluster's shared ladder: the blockers undo, redo and history all
 * answer to before their own. Lifted out of versionCluster unchanged so the
 * three records can each state it.
 */
export function versionSharedReason(ctx = {}) {
  if (!ctx.hasVersions) return REASONS.noVersions
  if (ctx.versionBusy) return REASONS.versionBusy
  if (ctx.running) return REASONS.running
  if (ctx.previewing) return REASONS.previewing
  if (ctx.mutationsBlocked) return REASONS.mutationsBlocked
  return ''
}

// --- the accessible-name composer -----------------------------------------

/**
 * The one place the ribbon's accessible name is spelled. DraftingRibbon's
 * RibbonTool and CockpitTopBand's QuickButton both render exactly this string,
 * and draftingRibbon.test.jsx / engineSessionProvider.test.jsx pin it byte for
 * byte, so it is composed here once and asserted against those literals.
 */
export function accessibleName(label, reason = '') {
  // No label, no name: the attribute is OMITTED (undefined), as the template
  // this replaced omitted it, never stamped as aria-label="" for a screen
  // reader to announce as nothing. An empty-string label is a name and stays.
  if (label == null) return undefined
  const name = String(label)
  const why = String(reason ?? '')
  return why ? `${name} (unavailable: ${why})` : name
}

// --- the global key ladder -------------------------------------------------

// A focused interactive control keeps its keys: Space must ACTIVATE a button,
// not yank focus into the command bar. The selector is the ladder's, spelled
// once so the pinning test can read the same string App.jsx obeys.
export const INTERACTIVE_TARGET_SELECTOR = 'button, a, summary, [role="button"], [role="option"], [role="menuitem"]'

/**
 * Esc pops ONE rung at a time, topmost surface first. The order is the ladder
 * App.jsx has always walked; the first rung whose `open` answers true wins and
 * nothing below it fires. Each `run` names the handler the caller supplies —
 * this module performs no effect of its own.
 */
export const ESCAPE_RUNGS = Object.freeze([
  Object.freeze({ id: 'drawer', open: (ctx) => !!ctx.drawer, run: (ctx) => ctx.onCloseDrawer?.() }),
  Object.freeze({ id: 'history', open: (ctx) => !!ctx.historyOpen, run: (ctx) => ctx.onCloseHistory?.() }),
  Object.freeze({ id: 'route', open: (ctx) => !!ctx.route, run: (ctx) => ctx.onDismissRoute?.() }),
  Object.freeze({ id: 'errors', open: (ctx) => !!ctx.routeErr || !!ctx.runErr, run: (ctx) => ctx.onClearErrors?.() }),
  // Esc-on-running is the ONE interrupt gesture (the rail keeps the job;
  // nothing cancels server-side).
  Object.freeze({ id: 'running', open: (ctx) => !!ctx.running, run: (ctx) => ctx.onInterruptRun?.() }),
  Object.freeze({ id: 'selection', open: (ctx) => !!ctx.selectedHandle, run: (ctx) => ctx.onClearSelection?.() }),
  // Bottom rung: the WorkspaceSummary Esc cap — close the open project only
  // once every higher surface has already yielded.
  Object.freeze({ id: 'project', open: (ctx) => !!ctx.openProjectId, run: (ctx) => ctx.onCloseProject?.() }),
])

/**
 * R fires the highest-priority visible error's retry. `rTarget` is computed by
 * App (it reads a dozen pieces of shell state); this table says what each of
 * its values RUNS. `result` is absent on purpose: ResultPanel owns its own
 * listener, and duplicating it here double-fired the retry (two POST /api/run
 * from one keypress).
 */
export const RETRY_RUNGS = Object.freeze({
  route: (ctx) => ctx.onRetryRoute?.(),
  history: (ctx) => ctx.onRetryHistory?.(),
  tools: (ctx) => ctx.onRetryTools?.(),
  catalog: (ctx) => ctx.onRetryCatalog?.(),
  refresh: (ctx) => ctx.onRetryRefresh?.(),
})

/** The Esc rung that would fire for this context, or '' when none would. */
export function escapeRung(ctx = {}) {
  const rung = ESCAPE_RUNGS.find((r) => r.open(ctx))
  return rung ? rung.id : ''
}

/** The R rung that would fire for this context, or '' when none would. */
export function retryRung(ctx = {}) {
  const target = ctx.rTarget
  return typeof target === 'string' && Object.prototype.hasOwnProperty.call(RETRY_RUNGS, target) ? target : ''
}

const isTypingTag = (tag) => tag === 'input' || tag === 'textarea'

function isInteractiveTarget(target) {
  if (typeof Element === 'undefined' || !(target instanceof Element)) return false
  return !!target.closest(INTERACTIVE_TARGET_SELECTOR)
}

/**
 * The global key ladder as ONE pure decision, so the behaviour App.jsx used to
 * spell as a chain of if/else can be walked by a test without a browser.
 *
 * Returns null when the key is not the ladder's, else
 * `{ id, rung, route, preventDefault, instant }`:
 *   id             the registry action to run (`byId(id).run(ctx)`).
 *   rung           which rung of a multi-rung action fired ('' when single).
 *   route          'kbd'  the action's own cap fired it.
 *                  'type' type-to-fall-through reached `bar:focus` instead.
 *   preventDefault whether the caller must swallow the key.
 *   instant        whether the change lands frame-of-keypress (markInstant).
 *
 * Order and skip rules are the ladder's own, unchanged: Cmd/Ctrl+K wins even
 * inside a field; Esc pops one rung when one is open (never preventDefaulted,
 * so a focused control still sees the key) and returns null when nothing is
 * open, so the ladder leaves the key alone; R fires only outside a text field,
 * only unmodified, and only when a retry rung is live, otherwise the key FALLS
 * THROUGH to the bar; Shift+? (slice 10b) opens the shortcut sheet, same
 * outside-a-text-field guard as R; and any other bare printable keystroke
 * falls into the bar unless the target is editable or interactive, or an
 * overlay (drawer, history) owns the typing.
 */
export function ladderDecision(event, ctx = {}) {
  if (!event || typeof event.key !== 'string') return null
  const tag = String(event.target?.tagName || '').toLowerCase()
  const typing = isTypingTag(tag)

  if ((event.metaKey || event.ctrlKey) && (event.key === 'k' || event.key === 'K')) {
    return { id: 'bar:focus', rung: '', route: 'kbd', preventDefault: true, instant: true }
  }

  if (event.key === 'Escape') {
    const rung = escapeRung(ctx)
    // No open rung: nothing to close, so the ladder leaves the key alone
    // (null, no preventDefault). Esc is not printable, so it never reaches the
    // type-to-fall-through route below either.
    return rung ? { id: 'bar:escape', rung, route: 'kbd', preventDefault: false, instant: true } : null
  }

  if (!typing && (event.key === 'r' || event.key === 'R')
      && !event.metaKey && !event.ctrlKey && !event.altKey) {
    const rung = retryRung(ctx)
    if (rung) return { id: 'bar:retry', rung, route: 'kbd', preventDefault: true, instant: true }
  }

  // Shift+? opens the shortcut sheet (slice 10b). Outside text fields only,
  // same guard as R: a real "?" typed into a field must stay a character.
  if (!typing && event.key === '?' && !event.metaKey && !event.ctrlKey && !event.altKey) {
    return { id: 'bar:shortcuts', rung: '', route: 'kbd', preventDefault: true, instant: false }
  }

  // Type-to-fall-through (operator rule): a bare printable keystroke on the
  // surface always falls into the prompt bar. Focus happens BEFORE the default
  // action so the character itself lands in the input, so this route never
  // preventDefaults and never stamps instant.
  const editable = typing || tag === 'select' || !!event.target?.isContentEditable
  if (!editable && !isInteractiveTarget(event.target) && !ctx.drawer && !ctx.historyOpen
      && !event.metaKey && !event.ctrlKey && !event.altKey
      && event.key.length === 1 && event.key !== ' ') {
    return { id: 'bar:focus', rung: '', route: 'type', preventDefault: false, instant: false }
  }
  return null
}

/**
 * The window keydown listener the shell mounts, built on the pure decision.
 * NO ALLOCATION ON A NON-LADDER KEY: `shell` is the plain state the decision
 * reads, built ONCE per subscription by the caller (never per keystroke), and
 * `handlers(shell)` builds the context the record runs against and is called
 * ONLY after a decision came back. The pre-slice if/else chain allocated
 * nothing for a key that was not its own, and neither does this. `markInstant`
 * stamps the frame-of-keypress change (data-instant) a decision asks for.
 * Fails closed at construction, not on the first keystroke.
 */
export function ladderListener(shell, handlers, markInstant) {
  if (!shell || typeof shell !== 'object') throw new TypeError('ladderListener: shell must be an object')
  if (typeof handlers !== 'function') throw new TypeError('ladderListener: handlers must be a function')
  return (event) => {
    const decision = ladderDecision(event, shell)
    if (!decision) return
    if (decision.instant) markInstant?.()
    if (decision.preventDefault) event.preventDefault()
    byId(decision.id)?.run(handlers(shell))
  }
}

// --- the records -----------------------------------------------------------

const NO_TRIGGERS = { mouse: null, keyboard: null, touch: null }
// A plain <button>: clicked, tapped, no keyboard path of its own.
const BUTTON_TRIGGERS = Object.freeze({ mouse: 'click', keyboard: null, touch: 'tap' })
// A "/" picker row, a div[role=option] rather than a <button>: clicked, tapped,
// and Enter on the highlighted row picks it (PromptBox.jsx's menu keydown).
const PICKER_TRIGGERS = Object.freeze({ mouse: 'click', keyboard: 'enter', touch: 'tap' })
// A key-ladder rung: no pointer path at all, by design.
const KEY_TRIGGERS = Object.freeze({ mouse: null, keyboard: 'kbd', touch: null })

const always = () => ''
const text = (value) => () => value

/** One ribbon record. `title` is a function of ctx so a toggle can name its next state. */
const ribbon = (id, label, display, icon, title, when, run, extra = {}) => ({
  id,
  label,
  text: display,
  icon,
  size: 'large',
  kbd: null,
  title: typeof title === 'function' ? title : text(title),
  when,
  // Can this action be taken away at all? An ungated record renders NO
  // `disabled` and NO `reason` key, the way the ribbon's own toggles always
  // have: a control that is never withheld owes no sentence, and inventing an
  // empty one would put a `reason: ''` in the DOM contract for no reader.
  gated: when !== always,
  run,
  triggers: BUTTON_TRIGGERS,
  surface: 'ribbon',
  ...extra,
})

/** One drafting-engine op record. `op` and `group` are the engine's own names. */
// `panel` is WHERE a record sits on the ribbon; `group` is HOW it runs. They
// differ only for W4g-5d's TEXT, a draw create the reference seats in its
// Annotation panel, so every gate that admits `draw` admits it unchanged.
const engineOp = (group, op, label, display, icon, title, size, panel = group) => ({
  id: `${group}:${op}`,
  op,
  group,
  panel,
  label,
  text: display,
  icon,
  size,
  write: true,
  gated: true,
  kbd: null,
  title: text(title),
  // W4g-1b: the reach state rides the context so a button's reason says
  // what the panel note says while the console's drawing is opening.
  // Each engine group answers to its own ladder: draw needs a document,
  // modify needs a selection, and paste needs a clipboard rather than either.
  when: group === 'draw'
    ? (ctx) => drawReason(ctx.session, ctx.reach)
    : op === 'pasteClip'
      ? (ctx) => clipboardReason(ctx.session, ctx.reach)
      : (ctx) => modifyReason(ctx.session, ctx.reach),
  // Arming vs. running is the consumer's decision (a tool with operands opens
  // the command prompt; one without runs on click), so the record names the
  // one handler and passes the op.
  run: (ctx) => ctx.onActivate?.(group, op),
  triggers: BUTTON_TRIGGERS,
  surface: 'engine',
})

const ACTION_LIST = [
  // View: fit / zoom / the Properties pane toggle (ribbonClusters.viewCluster).
  ribbon('fit', 'fit', 'Fit', 'fit', 'Fit the drawing to the view',
    (ctx) => (ctx.hasDrawing ? '' : REASONS.noDrawing), (ctx) => ctx.onFit?.(), { cluster: 'view' }),
  ribbon('zoom-in', 'zoom-in', 'Zoom in', 'zoom-in', 'Zoom in',
    (ctx) => (ctx.hasDrawing ? '' : REASONS.noDrawing), (ctx) => ctx.onZoomIn?.(), { cluster: 'view' }),
  ribbon('zoom-out', 'zoom-out', 'Zoom out', 'zoom-out', 'Zoom out',
    (ctx) => (ctx.hasDrawing ? '' : REASONS.noDrawing), (ctx) => ctx.onZoomOut?.(), { cluster: 'view' }),
  // The pane toggle is never taken away where it exists: the caller only seats
  // it when it owns a pane, so there is no state in which it owes a sentence.
  ribbon('properties-pane', 'properties', 'Properties', 'sidebar',
    (ctx) => (ctx.paneOpen ? 'Close the properties pane' : 'Open the properties pane'),
    always, (ctx) => ctx.onTogglePane?.(), { cluster: 'view' }),

  // Version: undo / redo / history, under EXACTLY the toolbar's gates.
  ribbon('undo', 'undo', 'Undo', 'undo', 'Undo the last version',
    (ctx) => versionSharedReason(ctx) || (!ctx.canUndo ? REASONS.nothingToUndo : ''),
    (ctx) => ctx.onUndo?.(), { cluster: 'version' }),
  ribbon('redo', 'redo', 'Redo', 'redo', 'Redo the undone version',
    (ctx) => versionSharedReason(ctx) || (!ctx.canRedo ? REASONS.nothingToRedo : ''),
    (ctx) => ctx.onRedo?.(), { cluster: 'version' }),
  // History answers to a SHORTER ladder than undo/redo: reading the versions
  // is legal while a run is in flight, changing them is not.
  ribbon('history', 'history', 'History', 'history', 'Open the version history',
    (ctx) => (!ctx.hasVersions ? REASONS.noVersions : ctx.versionBusy ? REASONS.versionBusy : ''),
    (ctx) => ctx.onToggleHistory?.(), { cluster: 'version' }),

  // Rail: the one command the hidden tool rail still needs from the band.
  ribbon('rail-expand', 'expand', 'Tool rail', 'sidebar', 'Expand the tool rail',
    always, (ctx) => ctx.onExpand?.(), { cluster: 'rail' }),

  // Author: two distinct reasons, because they have two distinct fixes — a
  // plan that lacks `build`, and a deployment whose authoring stage is off.
  ribbon('author-tool', 'author-tool', 'Author tool', 'wand', 'Build a new tool from plain English',
    (ctx) => (!ctx.entitled ? REASONS.buildUnentitled : !ctx.available ? REASONS.buildUnavailable : ''),
    (ctx) => ctx.onOpen?.(), { cluster: 'author' }),

  // Draw: each button creates ONE primitive from the numeric operands. The
  // engine validates again and refuses with a typed reason.
  engineOp('draw', 'createLine', 'line', 'Line', 'line', 'Draw a line from x,y to x2,y2', 'large'),
  engineOp('draw', 'createPolyline', 'polyline', 'Polyline', 'polyline', 'Draw a polyline through the points listed (x,y pairs)', 'large'),
  engineOp('draw', 'createCircle', 'circle', 'Circle', 'circle', 'Draw a circle at x,y with radius r', 'large'),
  engineOp('draw', 'createArc', 'arc', 'Arc', 'arc', 'Draw an arc at x,y with radius r from start to end (degrees)', 'large'),
  // W4g-4 RECTANG: two opposite corners; the store lowers it to the closed
  // polyline the engine draws.
  engineOp('draw', 'createRectangle', 'rectangle', 'Rectangle', 'rectangle', 'Draw a rectangle from corner x,y to corner x2,y2', 'small'),
  // W4g-4b: the rest of the reference's small Draw column, engine-backed.
  engineOp('draw', 'createEllipse', 'ellipse', 'Ellipse', 'ellipse', 'Draw an ellipse from a centre, an axis endpoint and a minor-to-major ratio', 'small'),
  engineOp('draw', 'createPoint', 'point', 'Point', 'point', 'Place a point at x,y', 'small'),
  // W4g-5d: single-line TEXT. A create (group draw) seated in the reference's
  // Annotation panel.
  engineOp('draw', 'createText', 'text', 'Text', 'text', 'Place a line of text at x,y with a height and rotation', 'large', 'annotation'),

  // Modify: the entity operations the compiled engine performs.
  engineOp('modify', 'delete', 'delete', 'Delete', 'delete', 'Delete the selected entity', 'small'),
  engineOp('modify', 'move', 'move', 'Move', 'move', 'Move the selected entity by dx, dy', 'small'),
  engineOp('modify', 'moveVertex', 'move-vertex', 'Move vertex', 'move-vertex', 'Move one vertex of the selection by dx, dy', 'small'),
  engineOp('modify', 'addVertex', 'add-vertex', 'Add vertex', 'add-vertex', 'Insert a vertex after the given one, at dx, dy', 'small'),
  engineOp('modify', 'deleteVertex', 'delete-vertex', 'Delete vertex', 'delete-vertex', 'Delete one vertex of the selection', 'small'),
  engineOp('modify', 'setLayer', 'set-layer', 'Set layer', 'set-layer', 'Reassign the selection to the layer named', 'small'),
  // W4g-4: the reference's Modify verbs the crate carries (COPY, MIRROR,
  // ROTATE, SCALE, EXPLODE), each a real engine op with a prompt; EXPLODE
  // takes no operands and runs on click like Delete.
  engineOp('modify', 'copy', 'copy', 'Copy', 'copy', 'Copy the selected entity by dx, dy', 'small'),
  engineOp('modify', 'mirror', 'mirror', 'Mirror', 'mirror', 'Mirror the selection about a line through two points', 'small'),
  engineOp('modify', 'rotate', 'rotate', 'Rotate', 'rotate', 'Rotate the selection about a base point by an angle (degrees)', 'small'),
  engineOp('modify', 'scale', 'scale', 'Scale', 'scale', 'Scale the selection about a base point by a factor', 'small'),
  engineOp('modify', 'explode', 'explode', 'Explode', 'explode', 'Explode the selected polyline into its segments', 'small'),
  engineOp('modify', 'offset', 'offset', 'Offset', 'offset', 'Draw a parallel copy of the selection, the distance you give, on the side you click', 'small'),
  engineOp('modify', 'arrayRect', 'array', 'Array', 'array', 'Copy the selection into a grid of rows and columns', 'small'),
  engineOp('modify', 'arrayPolar', 'array-polar', 'Polar array', 'array-polar', 'Copy the selection around a centre point through an angle', 'small'),
  // W4g-6: the intersection verbs. The geometry is computed in the browser
  // (intersect.js) and the engine applies it as ONE batch, so each is one
  // round trip and one undo step however many entities it touches.
  engineOp('modify', 'trim', 'trim', 'Trim', 'trim', 'Cut the selection at a cutting edge and remove the part you click', 'small'),
  engineOp('modify', 'extend', 'extend', 'Extend', 'extend', 'Lengthen the selection until it meets a boundary edge', 'small'),
  engineOp('modify', 'fillet', 'fillet', 'Fillet', 'fillet', 'Round the corner between the selection and a second line, arc or circle with an arc', 'small'),
  engineOp('modify', 'chamfer', 'chamfer', 'Chamfer', 'chamfer', 'Bevel the corner between the selection and a second line', 'small'),
  // W4g-4b: the reference's MATCHPROP, seated in its Properties panel. It
  // copies the selection's LAYER to the object you pick; colour, linetype
  // and lineweight wait on the contract (the panel's ByLayer fields say so).
  engineOp('modify', 'matchprop', 'match', 'Match', 'match', "Copy the selection's layer to the object you click (colour, linetype and lineweight are not carried yet)", 'large', 'properties'),
  // W4g-5c: the reference's Clipboard panel, in its order and its sizes.
  engineOp('clipboard', 'pasteClip', 'paste', 'Paste', 'paste', 'Paste the clipboard entity at a base point', 'large'),
  engineOp('clipboard', 'cutClip', 'cut', 'Cut', 'cut', 'Put the selection on the clipboard and delete it', 'small'),
  engineOp('clipboard', 'copyClip', 'copy-clip', 'Copy', 'copy', 'Put the selection on the clipboard', 'small'),

  // The "/" picker's CLIENT commands. `clientAction` is the key composer.js's
  // filterRunnable gates on: a command whose handler is missing is dropped
  // from the menu rather than listed and then silently ignored.
  //
  // `help` is DECLARED BY THE SERVER (the registry endpoint sends the entry);
  // only its handler is the client's, so it seats no static picker row.
  {
    id: 'slash:help',
    gated: false,
    label: 'help',
    text: 'help',
    icon: '',
    kbd: null,
    clientAction: 'help',
    staticEntry: false,
    description: '',
    title: text('Reopen the command menu'),
    when: always,
    run: (ctx) => ctx.onHelp?.(),
    triggers: PICKER_TRIGGERS,
    surface: 'slash',
  },
  // `mcp` has no server entry, so the client contributes both the row and the
  // handler (PromptBox.jsx seats it through mergePickerEntries).
  {
    id: 'slash:mcp',
    gated: false,
    label: 'mcp',
    text: 'mcp',
    icon: '',
    kbd: null,
    clientAction: 'mcp',
    staticEntry: true,
    description: 'show mounted MCP servers',
    title: text('Show mounted MCP servers'),
    when: always,
    run: (ctx) => ctx.onOpenMcp?.(),
    triggers: PICKER_TRIGGERS,
    surface: 'slash',
  },

  // The global key ladder. These four are the ONLY actions in this registry
  // that carry a `kbd`, because they are the only four bound today.
  {
    id: 'bar:focus',
    gated: false,
    label: 'Command bar',
    text: 'Command bar',
    icon: '',
    kbd: 'Mod+K',
    title: text('Focus the command bar'),
    when: always,
    run: (ctx) => ctx.focusBar?.(),
    triggers: KEY_TRIGGERS,
    surface: 'bar',
  },
  {
    id: 'bar:escape',
    gated: true,
    label: 'Close the topmost surface',
    text: 'Close',
    icon: '',
    kbd: 'Escape',
    title: text('Close the topmost open surface'),
    when: (ctx) => (escapeRung(ctx) ? '' : LADDER_REASONS.nothingOpen),
    run: (ctx) => {
      const id = escapeRung(ctx)
      if (!id) return ''
      ESCAPE_RUNGS.find((r) => r.id === id).run(ctx)
      return id
    },
    triggers: KEY_TRIGGERS,
    surface: 'bar',
  },
  {
    id: 'bar:retry',
    gated: true,
    label: 'Retry the failed step',
    text: 'Retry',
    icon: '',
    kbd: 'R',
    title: text('Retry the highest-priority failed step'),
    when: (ctx) => {
      if (ctx.rTarget === 'result') return LADDER_REASONS.retryOwnedByResult
      return retryRung(ctx) ? '' : LADDER_REASONS.nothingToRetry
    },
    run: (ctx) => {
      const id = retryRung(ctx)
      if (!id) return ''
      RETRY_RUNGS[id](ctx)
      return id
    },
    triggers: KEY_TRIGGERS,
    surface: 'bar',
  },
  // Slice 10b: the shortcut sheet, generated from this file's own `kbd`
  // fields (keyboardTable() below), so a cap added here shows up there with
  // no second place to update. Unlike the other three ladder rungs this row
  // is also a real palette row (Enter runs it from the resolver, a click or
  // tap opens it too), so it is the one 'bar' action honest about all three
  // triggers rather than the ladder's keyboard-only shape.
  {
    id: 'bar:shortcuts',
    gated: false,
    label: 'Keyboard shortcuts',
    text: 'Shortcuts',
    icon: '',
    kbd: 'Shift+?',
    title: text('Open the keyboard shortcut sheet'),
    when: always,
    run: (ctx) => ctx.onOpenShortcuts?.(),
    triggers: Object.freeze({ mouse: 'click', keyboard: 'kbd', touch: 'tap' }),
    surface: 'bar',
  },
]

// --- validation ------------------------------------------------------------

export const SURFACES = Object.freeze(['ribbon', 'engine', 'slash', 'bar'])
const MOUSE_TRIGGERS = Object.freeze([null, 'click'])
const KEYBOARD_TRIGGERS = Object.freeze([null, 'kbd', 'enter'])
const TOUCH_TRIGGERS = Object.freeze([null, 'tap', 'pointer'])
// Bounded by construction: an id, label or cap that grows without limit is a
// record nobody wrote, and a renderer must never be handed one.
export const MAX_ID_CHARS = 64
export const MAX_LABEL_CHARS = 120

const bounded = (value, max) => typeof value === 'string' && value.length > 0 && value.length <= max

/**
 * Every invariant a record must hold, checked ONCE at module load and thrown on
 * rather than rendered. Fails closed: a malformed registry is a code bug that
 * would otherwise surface as a greyed control with no sentence, an undefined
 * label, or a key that runs nothing.
 *
 * Returns the list it was given (frozen by the caller) so it reads as a gate.
 */
export function validateRegistry(actions) {
  if (!Array.isArray(actions) || actions.length === 0) throw new Error('actionRegistry: no actions')
  const seen = new Set()
  for (const a of actions) {
    if (!a || typeof a !== 'object') throw new Error('actionRegistry: a record is not an object')
    if (!bounded(a.id, MAX_ID_CHARS)) throw new Error(`actionRegistry: bad id ${JSON.stringify(a.id)}`)
    if (seen.has(a.id)) throw new Error(`actionRegistry: duplicate id ${a.id}`)
    seen.add(a.id)
    if (!bounded(a.label, MAX_LABEL_CHARS)) throw new Error(`actionRegistry: ${a.id} has no bounded label`)
    if (typeof a.text !== 'string' || a.text.length > MAX_LABEL_CHARS) throw new Error(`actionRegistry: ${a.id} has no bounded text`)
    if (typeof a.icon !== 'string') throw new Error(`actionRegistry: ${a.id} has no icon key`)
    if (!SURFACES.includes(a.surface)) throw new Error(`actionRegistry: ${a.id} has surface ${a.surface}`)
    if (!(a.kbd === null || bounded(a.kbd, MAX_ID_CHARS))) throw new Error(`actionRegistry: ${a.id} has a bad kbd cap`)
    if (typeof a.when !== 'function') throw new Error(`actionRegistry: ${a.id} has no when()`)
    if (typeof a.run !== 'function') throw new Error(`actionRegistry: ${a.id} has no run()`)
    if (typeof a.title !== 'function') throw new Error(`actionRegistry: ${a.id} has no title()`)
    const t = a.triggers
    if (!t || typeof t !== 'object') throw new Error(`actionRegistry: ${a.id} declares no triggers`)
    if (!MOUSE_TRIGGERS.includes(t.mouse)) throw new Error(`actionRegistry: ${a.id} mouse trigger ${t.mouse}`)
    if (!KEYBOARD_TRIGGERS.includes(t.keyboard)) throw new Error(`actionRegistry: ${a.id} keyboard trigger ${t.keyboard}`)
    if (!TOUCH_TRIGGERS.includes(t.touch)) throw new Error(`actionRegistry: ${a.id} touch trigger ${t.touch}`)
    // A cap with no keyboard trigger is a shortcut nobody bound; a keyboard
    // trigger of 'kbd' with no cap is a key nobody can find. Both are lies.
    if ((a.kbd === null) !== (t.keyboard !== 'kbd')) throw new Error(`actionRegistry: ${a.id} kbd and keyboard trigger disagree`)
    // `when` and `title` must be TOTAL: a renderer calls them with whatever
    // context it holds, including none at all on first paint.
    if (typeof a.gated !== 'boolean') throw new Error(`actionRegistry: ${a.id} does not declare gated`)
    const why = a.when({})
    if (typeof why !== 'string') throw new Error(`actionRegistry: ${a.id} when() returned a non-string`)
    if (why && !KNOWN_REASON_VALUES.has(why)) throw new Error(`actionRegistry: ${a.id} when() returned an unregistered reason`)
    if (!a.gated && why) throw new Error(`actionRegistry: ${a.id} is ungated but when() named a reason`)
    if (!bounded(a.title({}), MAX_LABEL_CHARS)) throw new Error(`actionRegistry: ${a.id} title() is not a bounded string`)
  }
  return actions
}

/**
 * THE registry: built once, at module load, and frozen. Every record inside is
 * frozen too, so a consumer that mutates one gets a TypeError in strict mode
 * instead of quietly changing every other surface's copy of the action.
 */
export const ACTIONS = Object.freeze(validateRegistry(ACTION_LIST).map((a) => Object.freeze({
  ...a,
  triggers: Object.freeze({ ...NO_TRIGGERS, ...a.triggers }),
})))

const BY_ID = new Map(ACTIONS.map((a) => [a.id, a]))

/**
 * Records grouped by one field, in registry order, each list frozen. Built
 * once with the registry, so a lookup on the paint path is one Map read that
 * hands back the SAME array every call: nothing allocates, and a consumer
 * that mutates the list gets a TypeError instead of changing every other
 * caller's view of the cluster.
 */
function indexBy(field) {
  const lists = new Map()
  for (const a of ACTIONS) {
    const key = a[field]
    if (key == null) continue
    if (!lists.has(key)) lists.set(key, [])
    lists.get(key).push(a)
  }
  for (const [key, list] of lists) lists.set(key, Object.freeze(list))
  return lists
}
const BY_SURFACE = indexBy('surface')
// `cluster` (a ribbon cluster) and `group` (an engine group) are two fields
// and two indexes: a ribbon cluster named draw or modify must never merge
// with the engine group of the same name.
const BY_CLUSTER = indexBy('cluster')
const BY_GROUP = indexBy('group')
const NONE = Object.freeze([])

// --- selectors -------------------------------------------------------------

/** One record by id, or null. O(1): the index is built once with the registry. */
export function byId(id) {
  return BY_ID.get(id) || null
}

/** Every record on one surface, in registry order. O(1), no allocation. */
export function forSurface(surface) {
  return BY_SURFACE.get(surface) || NONE
}

/** Every record in one RIBBON cluster, in registry order. O(1), no allocation. */
export function forCluster(cluster) {
  return BY_CLUSTER.get(cluster) || NONE
}

/** Every record in one ENGINE group (draw, modify), in registry order. O(1), no allocation. */
export function forGroup(group) {
  return BY_GROUP.get(group) || NONE
}

/**
 * The shortcut sheet's source: every action that actually carries a cap today,
 * in ladder order. An action with `kbd: null` is absent because nothing is
 * bound to it, not because the sheet forgot it.
 */
export function keyboardTable() {
  return ACTIONS.filter((a) => a.kbd !== null).map((a) => ({
    id: a.id,
    label: a.label,
    kbd: a.kbd,
    surface: a.surface,
  }))
}

/**
 * The "/" picker rows this client contributes itself. Server-declared commands
 * (help) are absent: the registry owns their HANDLER, the server owns the row.
 */
export function slashStaticEntries() {
  return forSurface('slash')
    .filter((a) => a.staticEntry)
    .map((a) => ({
      kind: 'command',
      name: a.label,
      description: a.description,
      client_action: a.clientAction,
    }))
}

/**
 * The `commandActions` map composer.js's filterRunnable gates on, built from
 * the registry for the ids given. Each handler closes over `ctx`, so wiring a
 * command is one registry record plus the ctx handler it names.
 */
export function slashCommandHandlers(ids, ctx) {
  const out = {}
  for (const id of ids) {
    const action = byId(id)
    if (!action || action.surface !== 'slash' || !action.clientAction) continue
    out[action.clientAction] = () => action.run(ctx)
  }
  return out
}

/**
 * One record projected into the ribbon's tool shape (DraftingRibbon's
 * RibbonTool / CockpitTopBand's QuickButton read exactly these fields). The
 * reason comes from `when`, so the accessible name the renderer composes is
 * the registry's answer, byte for byte.
 */
export function ribbonTool(action, ctx = {}, extra = {}) {
  const tool = {
    id: action.id,
    label: action.label,
    text: action.text,
    icon: action.icon,
    size: action.size,
    title: action.title(ctx),
  }
  if (action.gated) {
    const reason = action.when(ctx)
    tool.disabled = !!reason
    tool.reason = reason
  }
  tool.onClick = () => action.run(ctx)
  return { ...tool, ...extra }
}
