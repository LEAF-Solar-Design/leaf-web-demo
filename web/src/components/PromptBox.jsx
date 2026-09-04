// The SB3 command bar well (crib §1), docked at the bottom of the center
// column. Two rows — a 40 px input row (accent › caret, 13 px text, placeholder
// "Find, act, or build… ( / for tools)") and a light controls row inside the
// well: the 20 px "+" add chip and ONE "scope ▾" chip left (the chip opens the
// O2 resolver offering the find · act · build scopes, each row carrying its
// lane's live hit dot); project context, the summon keycap (⌘K, swapping to an
// emphasized Enter cap while focused) and a quiet Run chip right (SB3: no
// primary in the well). Enter dispatches; while a route decision is showing
// above the well, Enter belongs to the resolver (routeActive) and typing clears
// the stale route (App-side).
//
// Slash commands: a leading "/" lists the runnable tool catalog as an O2
// resolver menu above the well (same anatomy as the route resolver), filtered
// live by the text after the slash — ↑/↓ move, Tab completes the name into the
// input, Enter completes AND dispatches (the route decision strip still asks
// for confirmation — paid actions never auto-execute). Esc closes just the
// menu; a space after the tool name closes it too (args mode). The menu stands
// down entirely whenever ANOTHER resolver owns the well — a route decision
// (routeActive) or the scope menu — so only one listbox ever owns the keys.
//
// G2 ingest: the bar is the ONE drop catcher — mid-drag the well border goes
// dashed accent with "Drop manifest to ingest — runs sandboxed". No ingest
// path exists in src/api.js, so a drop surfaces the honest X1-style red strip
// (never a silent ignore).

import { useEffect, useId, useMemo, useRef, useState } from 'react'
import useExit from '../useExit.js'
import { authHeaders, config, noteUnauthorized } from '../api.js'
import { modChord } from '../lib/keys.js'
import { SECRET_REASONS, SECRET_REASONS_NO_MOUNT } from '../lib/secretPatterns.js'
import { isWriteTool } from '../lib/toolRecord.js'
import CockpitIcon from '../site/CockpitIcon.jsx'
import {
  appendPromptHistory,
  autoGrowHeight,
  createPromptHistoryState,
  filterRunnable,
  historyKeydown,
  mergePickerEntries,
  slashCommandHandlers,
  slashStaticEntries,
  pickerTrigger,
  rankEntries,
  replacePickerTrigger,
  setPromptHistorySession,
  setPromptHistoryValue,
  clipboardImagesToAttachments,
  IMAGE_MEDIA_TYPES,
} from '../composer.js'

// The client's own "/" rows, projected from the action registry (slice 10a):
// `mcp` is the one command with no server entry, so this client contributes
// both its row and its handler. Built once at module load, like the registry.
const STATIC_COMMANDS = slashStaticEntries()

// The credential-refusal copy now lives in the pure module beside the patterns
// (lib/secretPatterns.js), because the assistant reply box guards itself with
// the SAME sentences and must not import a React component to get them. Kept
// re-exported here: PromptBox.secretGuard.test.jsx and any consumer that
// already reads it from the bar keep working unchanged.
export { SECRET_REASONS, SECRET_REASONS_NO_MOUNT }

// The bar's scopes, mapped onto the app's lanes (find→run · act→solve ·
// build→author). Selecting find/act returns you to the composer (the router
// still decides the lane from what you type — no fake filter); build opens
// the real author flow, which also makes the "+" add chip an honest action.
const SCOPES = [
  { id: 'find', lane: 'run', desc: 'run an existing tool on the drawing' },
  { id: 'act', lane: 'solve', desc: 'cloud solve jobs — not wired in this demo' },
  { id: 'build', lane: 'build', desc: 'author a new capability' },
]

// Lane hit dots per the standard's dot grammar: run/build are actionable lanes
// (solid green on hit); solve is honestly not wired (amber square on hit);
// every lane rests as a hollow not-in-play dot.
function laneDotClass(lane, hit) {
  if (!hit) return 'dot hollow'
  if (lane === 'solve') return 'dot square'
  return 'dot'
}

export default function PromptBox({
  value, onChange, onDispatch, routing, hintLane, projectName, inputRef, routeActive,
  onOpenAuthor, tools = [], skills = [], sessionId = null,
  // action name -> handler, for registry entries of kind "command". An entry
  // whose action has no handler here is filtered out of the menu entirely
  // (composer.js filterRunnable), so the picker can never offer something this
  // client would silently ignore.
  commandActions = {},
  imageAttachmentsEnabled = false,
  // --- credential refusal (slice 8a round 3) -----------------------------
  // THIS COMPONENT HAS NO GUARD, and that is the fix. The guard is on the wire
  // (lib/secretGuardTransport.js, called by api.nlPrompt); the controller
  // catches its typed refusal and publishes it here. A local copy of the
  // decision is what made rounds 1 and 2 wrong: it made this composer look
  // like the authority while paths it never sees stayed open. Render the
  // verdict that was actually enforced, and nothing else.
  secretRefusal = null,
  // W4d Slice E: the reference's one-line docked "Command:" prompt. The DOM
  // is the same well; this only adds a class the studio's drafting-surface
  // CSS lays out as one row, and swaps the caret glyph for the prompt word.
  // False (the default, and rail OFF) renders byte-for-byte as before.
  commandLine = false,
  // Standardization slice 5a: the stage (/try, site/ToolCast.jsx) mounts THIS
  // component where its hand-rolled .tc-bar block stood, and 36 e2e rows key
  // off that block's hooks. Everything below is optional and defaults to
  // today's console render, byte for byte (promptBox.stage.test.jsx pins the
  // default element sequence against a capture taken before this slice).
  //
  // classNames: extra class names ADDED beside the box's own, never a rename:
  //   `bar` on the .bar well, `wrap` on the .bar-input row, `input` on the
  //   textarea, `run` on the Run chip. The box's classes stay (its CSS keys
  //   off them); the alias is a second hook on the same node.
  classNames = {},
  // projectSlot: what renders where the `.bar-proj` label stands (a node, or
  //   a render prop receiving projectName). The caller owns its classes, and
  //   `bar-proj` is the one that carries the row's margin-left:auto.
  projectSlot = null,
  // keycap: what renders where the ⌘K / Enter keycap swaps (a node, or a
  //   render prop receiving { focused }). The stage passes a static ⌘K
  //   because staging polish-pins asserts that exact text.
  keycap = null,
  // disabledReason: the caller-owned Run ladder. Left undefined, the box
  //   keeps its own rule (routing, or an empty prompt). A string disables Run
  //   and rides on the chip as its title, the honest reason; null enables it.
  disabledReason,
  // runLabel / routingLabel: the chip's copy at rest and while routing. The
  //   stage says "Send" in the public demo and "Routing" without the ellipsis,
  //   both asserted by e2e rows.
  runLabel = 'Run',
  routingLabel = 'Routing…',
  placeholder = 'Find, act, or build… ( / for tools)',
  // dropIngestEnabled: the G2 drop catcher below. The stage turns it OFF
  //   because a drop on its bar already means "open this DWG or DXF"
  //   (ToolCast's own handler on .tc-bar), and one gesture cannot carry two
  //   meanings. With it off the well registers no drag handlers at all, so
  //   the event bubbles to the stage's handler untouched.
  dropIngestEnabled = true,
  // mcpDiscoveryEnabled: gates the tenant-scoped MCP list fetch below.
  //   Default true keeps the console's byte-for-byte behavior (it never
  //   passes this prop). The stage passes signedIn && isEntitled('converse')
  //   because BLOCKER 1 (2026-09-04): mounting this component on the public,
  //   signed-out /try stage made every load call a tenant-scoped private
  //   endpoint the signed-out stage has no business calling, and a tenant
  //   without the converse entitlement could 401 a SIGNED-IN stage user off
  //   a background call the stage never made. A false value skips the fetch
  //   entirely, so a signed-out mount makes NO network call.
  mcpDiscoveryEnabled = true,
}) {
  // a11y (carried item 2, slice 5a round 2): disabledReason used to ride ONLY
  // on the Run chip's title, which a hover-only tooltip never reaches by
  // keyboard or screen reader. runReasonId names a visually-hidden node
  // (App.jsx's inline-style pattern: no .sr-only utility exists in the
  // sheet) that aria-describedby points the chip at whenever a reason
  // string is present, so the same sentence title= carries reaches both.
  const runReasonId = useId()
  const [focused, setFocused] = useState(false)
  const [scopeOpen, setScopeOpen] = useState(false)
  const [scopeIdx, setScopeIdx] = useState(0)
  const [dragging, setDragging] = useState(false)
  const [dropErr, setDropErr] = useState(null)
  const dragDepth = useRef(0) // dragenter/leave pair counter (children re-fire them)
  const rootRef = useRef(null)
  const scopeMenu = useExit(scopeOpen) // 180 ms M1 exit fade
  const [menuIdx, setMenuIdx] = useState(0)
  const [menuDismissed, setMenuDismissed] = useState(false)
  const [caret, setCaret] = useState(() => String(value ?? '').length)
  const [isComposing, setIsComposing] = useState(false)
  const [mcpOpen, setMcpOpen] = useState(false)
  const [mcpServers, setMcpServers] = useState([])
  const [attachments, setAttachments] = useState([])
  const [attachmentError, setAttachmentError] = useState(null)
  const attachmentUrlsRef = useRef(new Set())
  const historyRef = useRef(createPromptHistoryState(sessionId))

  // A PromptBox instance survives session switches, so retain histories in the
  // ref but reset only navigation state when its active session changes.
  historyRef.current = setPromptHistorySession(historyRef.current, sessionId)

  const releaseAttachment = (image) => {
    if (image?.thumbnailUrl) {
      URL.revokeObjectURL(image.thumbnailUrl)
      attachmentUrlsRef.current.delete(image.thumbnailUrl)
    }
  }
  const clearAttachments = () => setAttachments((current) => {
    for (const image of current) releaseAttachment(image)
    return []
  })
  useEffect(() => {
    clearAttachments()
  }, [sessionId]) // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => () => {
    for (const url of attachmentUrlsRef.current) URL.revokeObjectURL(url)
    attachmentUrlsRef.current.clear()
  }, [])

  // Both picker surfaces get the tenant-scoped, redacted list. Full resource
  // enumeration is a follow-up because it needs a harness proxy endpoint.
  // Gated on mcpDiscoveryEnabled (BLOCKER 1): a signed-out mount, or a caller
  // that passes false, makes NO network call — this is a discovery call for
  // the slash picker, never load-bearing, so a 401 off it is marked
  // non-fatal (fatal: false) and never wipes the stored token or fires the
  // unauthorized listeners, even for a signed-in user on a tenant without
  // the converse entitlement.
  useEffect(() => {
    if (!mcpDiscoveryEnabled) {
      setMcpServers([])
      return undefined
    }
    let live = true
    const loadMcp = async () => {
      try {
        const headers = { 'X-Tenant-Id': config.tenant, ...authHeaders() }
        const response = await fetch(`${config.apiBase}/api/converse/mcp`, { headers })
        noteUnauthorized(response, '/api/converse/mcp', headers.Authorization, { fatal: false })
        const body = response.ok ? await response.json().catch(() => null) : null
        if (live) setMcpServers(Array.isArray(body?.servers) ? body.servers : [])
      } catch {
        if (live) setMcpServers([])
      }
    }
    loadMcp()
    return () => { live = false }
  }, [mcpDiscoveryEnabled])

  // Slice 5a: an alias is appended to the box's own class, never swapped in
  // for it, so the console's selectors and the stage's both resolve.
  const withAlias = (base, alias) => (alias ? `${base} ${alias}` : base)
  // The Run chip's disabled state: the caller's ladder when one was passed
  // (a string is a reason, null is all-clear), else the box's own rule.
  const runDisabled = disabledReason === undefined
    ? (routing || !value.trim())
    : !!disabledReason

  const trigger = pickerTrigger(value, caret, isComposing)
  const afterSlash = trigger?.kind === 'slash' ? trigger.query : null
  const entrySource = useMemo(
    () => mergePickerEntries(tools, skills, mcpServers, STATIC_COMMANDS),
    [tools, skills, mcpServers],
  )
  const localCommandActions = useMemo(() => ({
    ...commandActions,
    ...slashCommandHandlers(['slash:mcp'], { onOpenMcp: () => setMcpOpen(true) }),
  }), [commandActions])

  // Name-prefix matches rank first (the Tab target reads left-to-right), then
  // name/description substring matches — case-insensitive, like Claude's picker.
  const matches = useMemo(() => {
    if (!trigger) return []
    const source = trigger.kind === 'resource'
      ? entrySource.filter((entry) => entry.kind === 'resource')
      : entrySource.filter((entry) => entry.kind !== 'resource')
    return rankEntries(filterRunnable(source, localCommandActions), trigger.query)
  }, [trigger, entrySource, localCommandActions])

  // While ANOTHER resolver is showing — a route decision, or the scope menu —
  // that resolver owns the surface AND the keys, so this menu stands down
  // entirely (typing clears the route App-side, which brings it straight back).
  // Gate on scopeMenu.shown, NOT scopeOpen: useExit holds the scope resolver
  // mounted for its 180 ms exit fade after scopeOpen flips false, so gating on
  // scopeOpen reopens this menu mid-fade and puts two listboxes on screen at
  // once. scopeMenu.shown covers both the open and the fading state.
  const menuOpen = !!trigger && !menuDismissed && !routeActive && !scopeMenu.shown

  // Any edit re-arms a dismissed menu and re-anchors the highlight. The
  // credential refusal is retired by the controller's own setPrompt, which the
  // same keystroke reaches, so a notice never outlives the text it was about.
  useEffect(() => { setMenuDismissed(false); setMenuIdx(0) }, [value])
  const idx = Math.min(menuIdx, Math.max(0, matches.length - 1))

  // Tab: complete the name into the input (trailing space closes the menu and
  // starts args mode). Enter: complete and hand off to dispatch in one act.
  const changePrompt = (nextValue, nextCaret = String(nextValue ?? '').length) => {
    historyRef.current = setPromptHistoryValue(historyRef.current, nextValue)
    setCaret(nextCaret)
    onChange(nextValue)
  }
  // allowSecretOnce is the "Send anyway" click's authorisation, carried as a
  // PARAMETER on this one dispatch. Nothing here remembers it, so a click the
  // host short-circuits authorises exactly nothing.
  const dispatchPrompt = (override, { allowSecretOnce = false } = {}) => {
    const sent = typeof override === 'string' ? override : value
    if (sent.trim() && !routing && !runDisabled) {
      historyRef.current = appendPromptHistory(historyRef.current, sent, sessionId)
    }
    const dispatched = onDispatch(override, { images: attachments, allowSecretOnce })
    return Promise.resolve(dispatched).then((result) => {
      if (result?.status === 202) clearAttachments()
      return result
    })
  }
  const onPaste = (e) => {
    if (!imageAttachmentsEnabled) {
      if ([...(e.clipboardData?.items || [])].some((item) => item?.kind === 'file' && IMAGE_MEDIA_TYPES.has(item.type))) {
        e.preventDefault()
        setAttachmentError('Image paste is available in the assistant reply box.')
      }
      return
    }
    const result = clipboardImagesToAttachments(e.clipboardData?.items, attachments)
    if (result.error) { e.preventDefault(); setAttachmentError(result.error); return }
    if (!result.attachments.length) return
    e.preventDefault()
    setAttachmentError(null)
    setAttachments((current) => [...current, ...result.attachments.map((image) => {
      const thumbnailUrl = URL.createObjectURL(image.file)
      attachmentUrlsRef.current.add(thumbnailUrl)
      return { ...image, id: `${Date.now()}-${Math.random()}`, thumbnailUrl }
    })])
  }
  const removeAttachment = (id) => setAttachments((current) => {
    const found = current.find((image) => image.id === id)
    releaseAttachment(found)
    return current.filter((image) => image.id !== id)
  })
  const complete = (t) => {
    if (!trigger) return
    const insertion = t.kind === 'resource' ? t.insertionText : `/${t.name} `
    const next = replacePickerTrigger(value, trigger, insertion)
    setMenuDismissed(true)
    changePrompt(next, trigger.start + insertion.length)
  }
  const pick = (t) => {
    // A command runs its own handler — it is not a tool, so routing it through
    // onDispatch would send "/stop" to the prompt router as if the tenant had
    // a tool by that name. filterRunnable guarantees the handler exists.
    if (t.kind === 'command') {
      changePrompt('')
      const run = localCommandActions[t.client_action]
      if (typeof run === 'function') run(t)
      return
    }
    if (trigger) {
      const insertion = t.kind === 'resource' ? t.insertionText : `/${t.name} `
      const next = replacePickerTrigger(value, trigger, insertion)
      setMenuDismissed(true)
      changePrompt(next, trigger.start + insertion.length)
      if (t.kind === 'resource') return
      dispatchPrompt(`/${t.name}`)
    }
  }

  const onKeyDown = (e) => {
    if (menuOpen && !e.isComposing) { // IME candidate navigation keeps its keys
      if (e.key === 'ArrowDown' && matches.length > 0) {
        e.preventDefault(); setMenuIdx(Math.min(idx + 1, matches.length - 1)); return
      }
      if (e.key === 'ArrowUp' && matches.length > 0) {
        e.preventDefault(); setMenuIdx(Math.max(idx - 1, 0)); return
      }
      if (e.key === 'Tab' && matches[idx]) {
        e.preventDefault(); complete(matches[idx]); return
      }
      if (e.key === 'Escape') {
        // closes ONLY the menu — the global Esc ladder must not also fire
        e.preventDefault(); e.stopPropagation(); setMenuDismissed(true); return
      }
      if (e.key === 'Enter' && matches[idx]) {
        e.preventDefault(); pick(matches[idx]); return
      }
    }
    // Ctrl+J is the terminal client's newline-in-any-terminal escape hatch;
    // honoured here so the same muscle memory works in the browser well.
    if (e.key === 'j' && e.ctrlKey && !e.isComposing) {
      e.preventDefault()
      insertNewline(e.target)
      return
    }
    if ((e.key === 'ArrowUp' || e.key === 'ArrowDown') && !e.isComposing) {
      const result = historyKeydown(historyRef.current, {
        key: e.key,
        value,
        selectionStart: e.target.selectionStart,
        sessionId,
      })
      historyRef.current = result.state
      if (result.handled) {
        e.preventDefault()
        changePrompt(result.value)
        requestAnimationFrame(() => {
          try { e.target.setSelectionRange(result.selectionStart, result.selectionStart) } catch { /* detached */ }
        })
        return
      }
    }
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { // isComposing: IME confirm-Enter must not dispatch
      // The resolver / decision strip owns Enter — but we must still swallow
      // it. An <input> had no default to suppress; a <textarea> inserts a
      // newline, so returning bare would type into the prompt during the
      // resolver's own handoff (including RoutePanel's ~350 ms cooldown, where
      // it also declines the key).
      if (routeActive || scopeOpen) { e.preventDefault(); return }
      e.preventDefault()
      dispatchPrompt()
    }
    // Shift+Enter deliberately falls through: the textarea inserts the newline
    // itself, so the caret lands where the user expects without us rebuilding
    // the value (which would strand the cursor at the end).
  }

  // Ctrl+J has no native newline behavior to fall through to, so splice one in
  // at the caret and restore the selection on the next frame.
  const insertNewline = (el) => {
    if (!el || typeof el.selectionStart !== 'number') { changePrompt(`${value}\n`); return }
    const start = el.selectionStart
    const end = el.selectionEnd
    changePrompt(`${value.slice(0, start)}\n${value.slice(end)}`)
    requestAnimationFrame(() => {
      try { el.setSelectionRange(start + 1, start + 1) } catch { /* detached */ }
    })
  }

  const openScope = (idx) => { setScopeIdx(idx); setScopeOpen(true) }
  const pickScope = (s) => {
    setScopeOpen(false)
    if (s.lane === 'build' && onOpenAuthor) { onOpenAuthor(); return }
    inputRef?.current?.focus()
  }

  // Scope resolver keys (capture, so the app's global Esc/Enter ladders stand
  // down while the menu is open) + outside-click close — the ProjectSwitcher
  // popover's pattern.
  useEffect(() => {
    if (!scopeOpen) return undefined
    const onDoc = (e) => { if (rootRef.current && !rootRef.current.contains(e.target)) setScopeOpen(false) }
    const onKey = (e) => {
      if (e.key === 'Escape') { e.stopPropagation(); setScopeOpen(false); return }
      if (e.key === 'ArrowDown') { e.preventDefault(); setScopeIdx((i) => Math.min(i + 1, SCOPES.length - 1)); return }
      if (e.key === 'ArrowUp') { e.preventDefault(); setScopeIdx((i) => Math.max(i - 1, 0)); return }
      if (e.key === 'Enter') { e.preventDefault(); e.stopPropagation(); pickScope(SCOPES[scopeIdx]) }
    }
    document.addEventListener('mousedown', onDoc)
    window.addEventListener('keydown', onKey, true)
    return () => {
      document.removeEventListener('mousedown', onDoc)
      window.removeEventListener('keydown', onKey, true)
    }
  }, [scopeOpen, scopeIdx]) // eslint-disable-line react-hooks/exhaustive-deps

  // --- G2 drop catcher -----------------------------------------------------
  const onDragEnter = (e) => {
    e.preventDefault()
    dragDepth.current += 1
    setDragging(true)
  }
  const onDragOver = (e) => { e.preventDefault() }
  const onDragLeave = () => {
    dragDepth.current = Math.max(0, dragDepth.current - 1)
    if (dragDepth.current === 0) setDragging(false)
  }
  const onDrop = (e) => {
    e.preventDefault()
    dragDepth.current = 0
    setDragging(false)
    // Honest failure: there is no ingest endpoint in api.js — say so plainly
    // (X1 anatomy: red dot + sentence naming what failed + honest note).
    const name = e.dataTransfer?.files?.[0]?.name
    setDropErr(`Ingest isn’t connected in this demo${name ? ` — ${name} wasn’t ingested` : ''}`)
  }

  return (
    <>
      {dropErr && (
        <div className="strip-failed enter">
          <span className="dot red" aria-hidden="true" />
          <span className="strip-sentence">
            {dropErr}
            <span className="dim"> · nothing was uploaded</span>
          </span>
          <button type="button" className="chip-act" onClick={() => setDropErr(null)}>Dismiss</button>
        </div>
      )}
      <div
        className={withAlias(`bar${dragging ? ' drag' : ''}${commandLine ? ' bar-command-line' : ''}`, classNames.bar)}
        data-tour="command-bar"
        ref={rootRef}
        onDragEnter={dropIngestEnabled ? onDragEnter : undefined}
        onDragOver={dropIngestEnabled ? onDragOver : undefined}
        onDragLeave={dropIngestEnabled ? onDragLeave : undefined}
        onDrop={dropIngestEnabled ? onDrop : undefined}
      >
        {dragging && (
          <div className="bar-drop-hint" aria-hidden="true">Drop manifest to ingest — runs sandboxed</div>
        )}
        {menuOpen && (
          <div className="resolver slash-menu" id="slash-menu-listbox" role="listbox" aria-label="Tool commands">
            <div className="resolver-header">
              {matches.length > 0
                ? <>{trigger.kind === 'resource' ? 'MCP servers · Tab inserts · Enter picks' : 'Tools · Tab completes · Enter picks — you still confirm before it runs'}</>
                : <>{trigger.kind === 'resource' ? 'No MCP servers mounted.' : <>No tool matches “/{afterSlash}” — keep typing, or Esc to close</>}</>}
            </div>
            {matches.map((t, i) => {
              const isWrite = isWriteTool(t)
              return (
                <div
                  key={t.name}
                  id={`slash-opt-${i}`}
                  className={`resolver-row ${i === idx ? 'active' : ''}`}
                  role="option"
                  aria-selected={i === idx}
                  onMouseEnter={() => setMenuIdx(i)}
                  onMouseDown={(e) => e.preventDefault()} // keep the input focused
                  onClick={() => pick(t)}
                >
                  <span className="lbar" aria-hidden="true" />
                  {/* The SAME glyph the ribbon shows for this tool (one record,
                      one icon) — decorative, aria-hidden inside CockpitIcon, so
                      the row's accessible name is still the `.label` text
                      below. Resource rows (@mounts) are servers, not tools, and
                      carry no icon. The read/write dot stays exactly where it
                      is: it is the money-relevant signal, not decoration. */}
                  {t.kind !== 'resource' && (
                    <CockpitIcon id={t.icon || ''} fallback={t.name} size="small" />
                  )}
                  <span className={isWrite ? 'dot square' : 'dot'} aria-hidden="true" />
                  <span className="label">
                    <span className="route-tool">{t.kind === 'resource' ? `@${t.name}:` : `/${t.name}`}</span>
                    {t.description && <span className="dim"> · {t.description}</span>}
                  </span>
                  {/* Kind first when the registry supplied one: a picker that
                      mixes commands, skills and tools has to say which is
                      which. Tools keep their read/write reading, which is the
                      money-relevant distinction. */}
                  <span className="count">
                    {/* "server", not "resource": these entries are MCP server
                        roots. Resource enumeration needs a harness proxy that
                        does not exist yet, so labelling them `resource` claimed
                        a capability this release does not ship (review round 2). */}
                    {t.kind === 'resource' ? 'server' : (t.kind && t.kind !== 'tool' ? t.kind : (isWrite ? 'write' : 'read'))}
                  </span>
                  {i === idx && <span className="key hot">Tab</span>}
                </div>
              )
            })}
          </div>
        )}
        {mcpOpen && (
          <div className="resolver mcp-panel" role="status" aria-label="Mounted MCP servers">
            <div className="resolver-header">Mounted MCP servers <button type="button" className="chip-neutral" onClick={() => setMcpOpen(false)}>Close</button></div>
            {mcpServers.length > 0 ? mcpServers.map((server) => (
              <div className="resolver-row" key={server.name}>
                <span className="lbar" aria-hidden="true" />
                <span className="dot hollow" aria-hidden="true" />
                <span className="label"><span className="route-tool">{server.name}</span><span className="dim"> · {server.host}</span></span>
              </div>
            )) : <div className="resolver-row"><span className="label">No MCP servers mounted.</span></div>}
            <div className="resolver-header">Tools are approval-gated.</div>
          </div>
        )}
        <div className={withAlias('bar-input', classNames.wrap)}>
          <span className="bar-caret" aria-hidden="true">{commandLine ? 'Command:' : '›'}</span>
          {/* A textarea, not an input: Shift+Enter (and Ctrl+J) must be able to
              put a real newline in the buffer — an <input> silently cannot hold
              one, so the existing `!e.shiftKey` dispatch guard was only ever
              half the feature. rows=1 plus the autogrow below keeps the 40 px
              single-line well until the text actually needs a second row. */}
          <textarea
            ref={inputRef}
            rows={1}
            // The well's styling hangs off `.bar-input input` (an ELEMENT
            // selector), so a <textarea> matches none of it — width, border
            // reset, background, colour, font and the 16 px mobile zoom guard
            // all vanish without this class. styles.css already carries the
            // `.bar-field` alternate for exactly this swap.
            className={withAlias('bar-field', classNames.input)}
            value={value}
            onChange={(e) => changePrompt(e.target.value, e.target.selectionStart)}
            onSelect={(e) => setCaret(e.target.selectionStart)}
            onCompositionStart={() => setIsComposing(true)}
            onCompositionEnd={() => setIsComposing(false)}
            onPaste={onPaste}
            onKeyDown={onKeyDown}
            onFocus={() => setFocused(true)}
            onBlur={() => setFocused(false)}
            placeholder={placeholder}
            spellCheck={false}
            aria-label="Command bar"
            data-testid="command-bar"
            role="combobox"
            aria-autocomplete="list"
            aria-expanded={menuOpen}
            aria-controls={menuOpen ? 'slash-menu-listbox' : undefined}
            aria-activedescendant={menuOpen && matches[idx] ? `slash-opt-${idx}` : undefined}
            style={{ height: autoGrowHeight(value), resize: 'none', overflowY: 'auto' }}
          />
        </div>
        {secretRefusal && (
          <div className="bar-secret-notice" role="alert" data-testid="secret-notice">
            <span className="dot red" aria-hidden="true" />
            <span className="strip-sentence" data-testid="secret-notice-reason">{secretRefusal.reason}</span>
            {/* At most a four-character shape prefix behind a fixed bullet run
                (maskForNotice). This is the ONLY place any character of the
                pasted credential is rendered, and it is never the entropy. */}
            <span className="dim" data-testid="secret-notice-mask">{secretRefusal.masked}</span>
            {secretRefusal.overridable && (
              <button
                type="button"
                className="chip-neutral"
                data-testid="secret-send-anyway"
                // Re-issues the SAME dispatch with the override as a call
                // parameter. Nothing is armed, so a click the host
                // short-circuits evaporates instead of latching open for the
                // next unrelated Enter. That is the round-3 fix, and it is why
                // this button needs no disabled set wider than the bar's own.
                disabled={routing}
                onClick={() => dispatchPrompt(undefined, { allowSecretOnce: true })}
              >
                Send anyway
              </button>
            )}
          </div>
        )}
        {(attachmentError || attachments.length > 0) && (
          <div className="converse-note" role={attachmentError ? 'alert' : undefined}>
            {attachmentError && <span className="dim">{attachmentError}</span>}
            {attachments.map((image) => (
              <span key={image.id} style={{ display: 'inline-flex', alignItems: 'center', gap: 4, marginLeft: 6 }}>
                <img src={image.thumbnailUrl} alt="Pending image attachment" width="28" height="28" style={{ objectFit: 'cover' }} />
                <button type="button" className="chip-neutral" onClick={() => removeAttachment(image.id)} aria-label="Remove image attachment">Remove</button>
              </span>
            ))}
          </div>
        )}
        <div className="bar-controls">
          <button
            type="button"
            className="bar-add"
            onClick={() => openScope(2)}
            aria-label="Add — build a new capability"
            title="Add — build a new capability"
          >
            +
          </button>
          <button
            type="button"
            className="bar-scope"
            onClick={() => (scopeOpen ? setScopeOpen(false) : openScope(0))}
            aria-expanded={scopeOpen}
            aria-haspopup="listbox"
          >
            scope ▾
          </button>
          {projectSlot != null
            ? (typeof projectSlot === 'function' ? projectSlot(projectName) : projectSlot)
            : <span className="bar-proj">{projectName}</span>}
          {keycap != null
            ? (typeof keycap === 'function' ? keycap({ focused }) : keycap)
            : (focused ? <span className="key hot">Enter</span> : <span className="key">{modChord('K')}</span>)}
          <button
            type="button"
            className={withAlias('chip-act', classNames.run)}
            onClick={() => dispatchPrompt()}
            disabled={runDisabled}
            title={disabledReason || undefined}
            aria-describedby={disabledReason ? runReasonId : undefined}
          >
            {routing ? routingLabel : runLabel}
          </button>
          {disabledReason && (
            <span
              id={runReasonId}
              style={{
                position: 'absolute', width: 1, height: 1, padding: 0, margin: -1,
                overflow: 'hidden', clip: 'rect(0 0 0 0)', whiteSpace: 'nowrap', border: 0,
              }}
            >
              {disabledReason}
            </span>
          )}
        </div>
        {scopeMenu.shown && (
          <div
            className={`resolver${scopeMenu.exiting ? ' exit' : ''}`}
            role="listbox"
            aria-label="Scope"
          >
            <div className="resolver-header">Scope — one prompt, three lanes</div>
            {SCOPES.map((s, i) => (
              <div
                key={s.id}
                className={`resolver-row ${i === scopeIdx ? 'active' : ''}`}
                role="option"
                aria-selected={i === scopeIdx}
                onMouseEnter={() => setScopeIdx(i)}
                onClick={() => pickScope(s)}
              >
                <span className="lbar" aria-hidden="true" />
                <span className={laneDotClass(s.lane, hintLane === s.lane)} aria-hidden="true" />
                <span className="label">
                  {s.id}
                  <span className="dim"> · {s.desc}</span>
                </span>
                <span className="count">{s.lane}</span>
                {i === scopeIdx && <span className="key hot">Enter</span>}
              </div>
            ))}
          </div>
        )}
      </div>
    </>
  )
}
