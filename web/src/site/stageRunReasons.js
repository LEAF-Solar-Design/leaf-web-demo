// The stage's Run ladder, spelled as sentences (standardization slice 5a).
//
// ToolCast.jsx used to disable its own `.tc-run` button on one inline
// predicate (`platformSession.status !== 'active' || !hasDrawing || busy ||
// jobRunning || routing || phase === 'loading'`) and said nothing about WHICH
// rung had taken the control away. The console's PromptBox, which the stage
// mounts now, takes the same ladder as ONE honest sentence (its
// `disabledReason` prop), so this module owns the rungs, in the order the old
// predicate evaluated them, and the honesty-ladder gate
// (web/scripts/check_honesty_ladder.mjs) holds every sentence to its floor:
// frozen map, plain quoted strings, no placeholder, twelve characters or more.
//
// Pure: no React, no DOM. The exact strings are pinned by
// stageRunReasons.test.js, and nothing else spells them.

import { byId } from '../lib/actionRegistry.js'

export const STAGE_RUN_REASONS = Object.freeze({
  session: 'Sign in to run a request on this drawing.',
  drawing: 'Upload a DWG or DXF before running a request.',
  busy: 'A request is already running. Wait for it to finish.',
  job: 'A job is still running. Detach from it or wait for it to finish.',
  routing: 'Routing the request. Wait for the route decision.',
  loading: 'The drawing is still loading.',
})

/**
 * The first rung that takes Run away, or null when every rung passes.
 *
 * The order is the OLD predicate's order, so the sentence names the same rung
 * the `disabled` attribute used to fire on. Every input is read as a boolean;
 * a missing field fails closed on the rung it belongs to, which is the honest
 * reading of "I do not know whether you are signed in": you are not.
 */
// The stage's Help ladder (standardization slice 13d): one static rung.
// ToolCast never mounts ShortcutSheet and wires no Shift+? rung (Slice 5a/10b
// stopped at the console), so the stage's "Keyboard shortcuts" palette row
// (byId('bar:shortcuts'), the SAME registry record the console's row reads —
// nothing forked) is declared here as disabled rather than either hidden
// (which would let a real registry cap go dark on this surface with nothing
// to check it against) or wired to a handler nobody built (which would run
// nothing and say the cap works when it does not).
export const STAGE_HELP_REASONS = Object.freeze({
  shortcuts: 'The keyboard shortcut sheet is not wired into the public stage yet.',
})

/**
 * The stage's ONE act-scope palette row for the console's shortcut sheet:
 * byId('bar:shortcuts')'s own id/label/icon/kbd (never re-typed), declared
 * disabled with STAGE_HELP_REASONS.shortcuts. A pure function, not a JSX
 * literal, so stageRunReasons.test.js can pin its shape without mounting
 * ToolCast.jsx.
 */
export function stageHelpPaletteRow() {
  const action = byId('bar:shortcuts')
  return {
    id: action.id,
    label: action.label,
    icon: action.icon,
    kbd: action.kbd,
    disabled: true,
    reason: STAGE_HELP_REASONS.shortcuts,
    onSelect: () => {},
  }
}

export function stageRunDisabledReason({
  sessionActive = false,
  hasDrawing = false,
  busy = false,
  jobRunning = false,
  routing = false,
  loading = false,
} = {}) {
  if (!sessionActive) return STAGE_RUN_REASONS.session
  if (!hasDrawing) return STAGE_RUN_REASONS.drawing
  if (busy) return STAGE_RUN_REASONS.busy
  if (jobRunning) return STAGE_RUN_REASONS.job
  if (routing) return STAGE_RUN_REASONS.routing
  if (loading) return STAGE_RUN_REASONS.loading
  return null
}
