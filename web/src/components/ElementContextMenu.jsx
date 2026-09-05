// THE ELEMENT CONTEXT MENU (standardization slice 9b, the ContextMenu half).
// One menu, mounted ONCE per scene through SurfaceFrame (site/SurfaceFrame.jsx
// `ContextMenu` slot), that opens over ANY element carrying `data-element-id`
// (slice 9a, web/src/lib/elementIdentity.js) — a ribbon tool, a board tile, a
// chat row, or the drawing canvas's current selection.
//
// WHY A GLOBAL DELEGATION HANDLER, NOT A PER-ELEMENT <ContextMenu.Trigger>:
// the drawing canvas's selection has no DOM node of its own (WebGL), and
// wrapping every ribbon tool / board tile / chat row in its own Radix Trigger
// would touch the DOM depth of every one of those render sites — the opposite
// of "additive". Instead this component listens once, at `document`, for the
// three real triggers (contextmenu, Shift+F10 / the Menu key, a touch long
// press), resolves `closestElementIdentity(event.target)` (or, for the
// keyboard path, the focused element), and — only once a real element-id is
// found — repositions an invisible 0x0 Radix Trigger at the pointer/focus
// coordinates and dispatches a real `contextmenu` event on it. Radix's own
// Trigger handler picks that dispatched event up exactly as it would a real
// right-click on content it wraps; this is the documented workaround for a
// "virtual" trigger Radix has no first-class prop for.
//
// ROWS come from the merged action registry (web/src/lib/actionRegistry.js,
// slice 10a), filtered by the element's KIND and run through the SAME
// resolver-row anatomy the Ctrl/Cmd+K palette already uses
// (web/src/lib/palette.js `actionPaletteRows`) — reused, not forked, so a
// disabled row's reason is composed exactly once no matter which surface
// renders it.
//
// WHAT KIND OF ROW EACH ELEMENT KIND GETS, and why some kinds get none yet:
// the registry has a real, already-gated action set for exactly three kinds
// today — the ribbon tool an id names (`tool`), the Modify/Clipboard-cut-copy
// group a canvas selection answers to (`entity`), and the Version cluster a
// board "Versions" tile echoes (`version`). `job`, `family`, `rung`, `turn`,
// `approval` and `item` have NO registry vocabulary yet (nothing in
// actionRegistry.js reasons about a build-queue job, a catalog family, an
// iOS ship-lane rung, or a chat turn/approval/feed item) — those kinds open
// the menu with ONLY the terminal row, honestly, rather than a fabricated
// one.
//
// THE TERMINAL ROW, "Ask Claude to…" (slice 9c): live for exactly the one
// element kind that has a real conversation scope today. converse.js's
// SCOPE_KINDS is ['project', 'drawing', 'entity'] — of this menu's element
// kinds (tool/version/job/family/rung/turn/approval/item/entity) only
// `entity` lines up, so only an entity selection carrying a real drawing id
// (`ctx.drawingId`, an honest gap at today's global mount — see the note by
// ScopedAskPanel below) opens the prompt; every other kind stays disabled
// with a real reason from CONTEXT_MENU_REASONS, never a fabricated one.
// Submitting posts through converse.js's `postMessage` — the ONE guarded
// transport (lib/secretGuardTransport.js) every free-text sender in this
// client shares — and mounts ChangeCapsule.jsx over the resulting annotation
// (useAnnotations.js, scoped to this same entity conversation).
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import * as RadixContextMenu from '@radix-ui/react-context-menu'

import { accessibleName, byId, forCluster, forGroup } from '../lib/actionRegistry.js'
import { closestElementIdentity } from '../lib/elementIdentity.js'
import { actionPaletteRows } from '../lib/palette.js'
import { ensureSession, postMessage } from '../converse.js'
import { isSecretRefused } from '../lib/secretGuardTransport.js'
import { useAnnotations } from '../useAnnotations.js'
import ChangeCapsule from './ChangeCapsule.jsx'

const LONG_PRESS_MS = 500
const LONG_PRESS_MOVE_PX_CEILING = 10

// The #977 lesson (see EngineRibbonClusters.jsx / App.jsx's own key-ladder
// note): a capture-phase key handler must never steal from a modal that owns
// it. This menu's own triggers stay OFF while a dialog/drawer holds the
// focused element or the pointed-at element sits inside one, and it never
// listens on the capture phase at all — see the header for the honesty-ladder
// map this reason string belongs to.
const MODAL_ANCESTOR_SELECTOR = '[role="dialog"], [aria-modal="true"], dialog, .drawer-layer'

// The reasons this slice registers with the honesty ladder
// (scripts/check_honesty_ladder.mjs discovers every `const *REASONS` map
// under web/src by name, so this is seen the same way REASONS/DRAW_REASONS/
// MODIFY_REASONS/CLIPBOARD_REASONS/LADDER_REASONS already are).
export const CONTEXT_MENU_REASONS = Object.freeze({
  askClaudeScoped: 'this element kind has no conversation scope yet, so the scoped prompt cannot open',
  askClaudeNoDrawing: 'no drawing is attached to this element yet, so the scoped prompt cannot open',
})

function isInsideModal(el) {
  return !!(el && typeof el.closest === 'function' && el.closest(MODAL_ANCESTOR_SELECTOR))
}

/**
 * Whether the scoped prompt can open for `identity`, and the honest reason
 * when it cannot. Only `entity` lines up with converse.js's SCOPE_KINDS
 * today (see the file header); every other kind is a real, permanent gap,
 * not a "not yet wired" one. `ctx.drawingId` absent is a SEPARATE, temporary
 * gap: today's global mount (App.jsx's contextMenuCtx) does not supply one,
 * the same honestly-named gap that file already documents for
 * `ctx.session`/`ctx.reach` on the Modify/Clipboard rows above.
 */
export function askClaudeReason(identity, ctx = {}) {
  if (!identity || identity.kind !== 'entity') return CONTEXT_MENU_REASONS.askClaudeScoped
  if (typeof ctx.drawingId !== 'string' || !ctx.drawingId) return CONTEXT_MENU_REASONS.askClaudeNoDrawing
  return ''
}

/**
 * The registry actions one element kind answers to. Pure and total: an
 * unrecognized or vocabulary-less kind returns `[]`, never throws — the
 * empty list IS the honest answer for `job` / `family` / `rung` / `turn` /
 * `approval` / `item` today.
 */
export function actionsForKind(kind, id) {
  if (kind === 'tool') {
    const action = byId(id)
    return action ? [action] : []
  }
  if (kind === 'entity') {
    // Modify answers to a selection; of the two Clipboard ops that also do,
    // Cut and Copy act on it (Paste does not, it needs a clipboard record
    // instead — see actionRegistry.js's own note on `clipboardReason`).
    return [...forGroup('modify'), ...forGroup('clipboard').filter((a) => a.op !== 'pasteClip')]
  }
  if (kind === 'version') {
    return forCluster('version')
  }
  return []
}

/**
 * One identity + live ctx -> the palette-shaped rows this menu renders.
 * `ctx` is whatever the mounting scene can supply (App.jsx / ToolCast.jsx);
 * an action whose `when` needs a field the caller never wired reads that
 * field as `undefined`, which is exactly the "not available" branch every
 * ladder function already has — an honest reason, never a fabricated one.
 */
export function rowsForIdentity(identity, ctx = {}) {
  if (!identity) return []
  const actions = actionsForKind(identity.kind, identity.id)
  const shaped = actions.map((action) => {
    const reason = action.gated ? action.when(ctx) : ''
    return {
      id: action.id,
      label: action.label,
      icon: action.icon,
      kbd: action.kbd,
      disabled: !!reason,
      reason,
      onSelect: () => action.run(ctx),
    }
  })
  return actionPaletteRows(shaped, '')
}

export default function ElementContextMenu({ ctx = {} }) {
  const [identity, setIdentity] = useState(null)
  // The open scoped-ask panel, if any: {identity, drawingId, x, y}. Kept
  // SEPARATE from `identity` (which the Radix menu clears on close) — the
  // panel and the capsule it grows into must survive the menu closing.
  const [ask, setAsk] = useState(null)
  const triggerRef = useRef(null)
  const pressTimerRef = useRef(null)
  const pressStartRef = useRef(null)
  const lastPosRef = useRef({ x: 0, y: 0 })

  const clearPressTimer = useCallback(() => {
    if (pressTimerRef.current) {
      clearTimeout(pressTimerRef.current)
      pressTimerRef.current = null
    }
    pressStartRef.current = null
  }, [])

  // The virtual-trigger dance: park the 0x0 trigger at (x, y) and fire a
  // REAL `contextmenu` event on it, bubbling, so Radix's own Trigger handler
  // (a plain DOM listener under the hood) opens the menu at that point
  // exactly as it would for a genuine right-click there.
  const openAt = useCallback((found, x, y) => {
    setIdentity(found)
    lastPosRef.current = { x, y }
    const el = triggerRef.current
    if (!el) return
    el.style.left = `${x}px`
    el.style.top = `${y}px`
    el.dispatchEvent(new MouseEvent('contextmenu', {
      bubbles: true, cancelable: true, clientX: x, clientY: y,
    }))
  }, [])

  useEffect(() => {
    const onContextMenu = (event) => {
      if (isInsideModal(event.target)) return
      const found = closestElementIdentity(event.target)
      if (!found) return
      event.preventDefault()
      openAt(found, event.clientX, event.clientY)
    }

    // Keyboard parity: Shift+F10 and the Menu key, on the FOCUSED element.
    const onKeyDown = (event) => {
      const isMenuKey = event.key === 'ContextMenu' || event.key === 'Apps'
      const isShiftF10 = event.key === 'F10' && event.shiftKey
      if (!isMenuKey && !isShiftF10) return
      const active = document.activeElement
      if (isInsideModal(active)) return
      const found = closestElementIdentity(active)
      if (!found) return
      event.preventDefault()
      const rect = typeof active.getBoundingClientRect === 'function'
        ? active.getBoundingClientRect()
        : { left: 0, bottom: 0 }
      openAt(found, rect.left, rect.bottom)
    }

    // Touch parity: a 500 ms long press, cancelled by enough movement, a
    // lifted finger, or a second touch point (a pinch is not a press).
    const onTouchStart = (event) => {
      if (event.touches?.length !== 1) return
      const touch = event.touches[0]
      if (isInsideModal(event.target)) return
      const found = closestElementIdentity(event.target)
      if (!found) return
      pressStartRef.current = { x: touch.clientX, y: touch.clientY }
      pressTimerRef.current = setTimeout(() => {
        pressTimerRef.current = null
        openAt(found, touch.clientX, touch.clientY)
      }, LONG_PRESS_MS)
    }
    const onTouchMove = (event) => {
      const start = pressStartRef.current
      const touch = event.touches?.[0]
      if (!start || !touch) return
      const dx = touch.clientX - start.x
      const dy = touch.clientY - start.y
      if (Math.hypot(dx, dy) > LONG_PRESS_MOVE_PX_CEILING) clearPressTimer()
    }

    document.addEventListener('contextmenu', onContextMenu)
    document.addEventListener('keydown', onKeyDown)
    document.addEventListener('touchstart', onTouchStart, { passive: true })
    document.addEventListener('touchmove', onTouchMove, { passive: true })
    document.addEventListener('touchend', clearPressTimer)
    document.addEventListener('touchcancel', clearPressTimer)
    return () => {
      document.removeEventListener('contextmenu', onContextMenu)
      document.removeEventListener('keydown', onKeyDown)
      document.removeEventListener('touchstart', onTouchStart)
      document.removeEventListener('touchmove', onTouchMove)
      document.removeEventListener('touchend', clearPressTimer)
      document.removeEventListener('touchcancel', clearPressTimer)
      clearPressTimer()
    }
  }, [openAt, clearPressTimer])

  const rows = useMemo(() => rowsForIdentity(identity, ctx), [identity, ctx])

  return (
    <RadixContextMenu.Root onOpenChange={(open) => { if (!open) setIdentity(null) }}>
      <RadixContextMenu.Trigger asChild>
        <span
          ref={triggerRef}
          aria-hidden="true"
          data-testid="element-context-menu-trigger"
          style={{ position: 'fixed', top: 0, left: 0, width: 0, height: 0, pointerEvents: 'none' }}
        />
      </RadixContextMenu.Trigger>
      {identity && (
        <RadixContextMenu.Portal>
          <RadixContextMenu.Content
            className="element-context-menu"
            data-testid="element-context-menu"
          >
            {rows.map((row) => (
              <RadixContextMenu.Item
                key={row.id}
                disabled={row.disabled}
                title={row.disabled ? row.reason : undefined}
                data-reason={row.disabled ? row.reason : undefined}
                onSelect={row.onSelect}
              >
                {accessibleName(row.label, row.disabled ? row.reason : '')}
              </RadixContextMenu.Item>
            ))}
            {rows.length > 0 && <RadixContextMenu.Separator />}
            {(() => {
              const reason = askClaudeReason(identity, ctx)
              return (
                <RadixContextMenu.Item
                  disabled={!!reason}
                  title={reason || undefined}
                  data-reason={reason || undefined}
                  data-testid="element-context-menu-ask-claude"
                  onSelect={reason ? undefined : () => {
                    const { x, y } = lastPosRef.current
                    setAsk({ identity, drawingId: ctx.drawingId, x, y })
                  }}
                >
                  {accessibleName('Ask Claude to…', reason)}
                </RadixContextMenu.Item>
              )
            })()}
          </RadixContextMenu.Content>
        </RadixContextMenu.Portal>
      )}
      {ask && (
        <ScopedAskPanel
          key={`${ask.identity.kind}:${ask.identity.id}`}
          identity={ask.identity}
          drawingId={ask.drawingId}
          x={ask.x}
          y={ask.y}
          resolveScopedIntakes={ctx.resolveScopedIntakes}
          onClose={() => setAsk(null)}
        />
      )}
    </RadixContextMenu.Root>
  )
}

/**
 * The scoped one-line prompt (opened by the terminal row) plus the change
 * capsule it grows into once a proposal lands. A single small floating
 * panel, positioned at the same coordinates the menu opened at.
 *
 * `resolveScopedIntakes` is the adapter ChangeCapsule.jsx documents:
 * `(identity) => {baseIntake, candidateIntake} | null`. Nothing here wires
 * one at the App.jsx mount today (the same honest, named gap the file header
 * describes) — omitting it means every accept for this element refuses with
 * ChangeCapsule's own real reason, never a fabricated pass.
 */
function ScopedAskPanel({ identity, drawingId, x, y, resolveScopedIntakes, onClose }) {
  const scope = useMemo(
    () => ({ kind: 'entity', handle: identity.id, drawingId }),
    [identity.id, drawingId],
  )
  const annotations = useAnnotations(scope, { enabled: true })
  const [text, setText] = useState('')
  const [sending, setSending] = useState(false)
  const [refusal, setRefusal] = useState(null)
  const [sendError, setSendError] = useState(null)

  const handleSubmit = useCallback(async (event) => {
    event.preventDefault()
    const trimmed = text.trim()
    if (!trimmed || sending) return
    setSending(true)
    setRefusal(null)
    setSendError(null)
    try {
      // The idempotent per-conversation cache converse.js already owns
      // (useAnnotations resolves the SAME session from this same scope);
      // calling it again here just returns the cached promise.
      const created = await ensureSession(scope)
      // THE ONE GUARDED TRANSPORT: postMessage runs the credential guard
      // (lib/secretGuardTransport.js) before this text ever reaches the
      // network — see converse.js's own header. Never call fetch here.
      await postMessage(created.session_id, { text: trimmed })
      setText('')
    } catch (err) {
      if (isSecretRefused(err)) setRefusal(err.refusal)
      else setSendError('The message did not send. Nothing changed.')
    } finally {
      setSending(false)
    }
  }, [scope, sending, text])

  return (
    <div
      className="ask-claude-panel"
      style={{ position: 'fixed', left: x, top: y, zIndex: 1000 }}
      data-testid="ask-claude-panel"
    >
      <form onSubmit={handleSubmit}>
        <input
          type="text"
          value={text}
          onChange={(event) => setText(event.target.value)}
          placeholder="Ask Claude to…"
          data-testid="ask-claude-input"
          disabled={sending}
        />
        <button type="submit" disabled={sending || !text.trim()} data-testid="ask-claude-send">
          {sending ? 'Sending…' : 'Send'}
        </button>
        <button type="button" onClick={onClose} data-testid="ask-claude-close">Close</button>
      </form>
      {refusal && (
        <div className="ask-claude-secret-notice" role="alert" data-testid="ask-claude-secret-notice">
          <span data-testid="ask-claude-secret-notice-reason">{refusal.reason}</span>
          <span className="dim" data-testid="ask-claude-secret-notice-mask">{refusal.masked}</span>
        </div>
      )}
      {sendError && <p role="alert" data-testid="ask-claude-send-error">{sendError}</p>}
      <ChangeCapsule
        identity={identity}
        annotation={annotations.annotation}
        busy={annotations.busy}
        error={annotations.error}
        confirmation={annotations.confirmation}
        onPreview={annotations.preview}
        onAccept={annotations.accept}
        onReject={annotations.reject}
        onRetry={annotations.retry}
        onUndo={annotations.undo}
        onEdit={() => setText('')}
        resolveScopedIntakes={resolveScopedIntakes}
      />
    </div>
  )
}
