// The tool RECORD as the web reads it: one predicate and one placement
// reader, so every surface answers "is this a write tool?" and "where does it
// want to sit?" the same way.
//
// Before this module three surfaces each inlined
// `(tool.capabilities || []).includes('drawing.write')` — the ribbon, the
// tools panel and the slash picker — which is three chances for the write dot,
// the write gate and the write lock to disagree. One predicate, three callers.
//
// Plain module, no React, no DOM: it is imported by pure builders and by
// components alike, and it is the web half of server/tool_record_fields.py.

export const WRITE_CAPABILITY = 'drawing.write'

// Mirrors web/src/site/CockpitTopBand.jsx RIBBON_TABS and, server-side,
// tool_record_fields.RIBBON_TAB_IDS. `model` is declared in RIBBON_TABS with a
// reason (not in this engine yet), so it is never a valid placement target.
export const RIBBON_TAB_IDS = Object.freeze(['draw', 'insert', 'annotate', 'view', 'manage'])
export const PLACEMENT_SIZES = Object.freeze(['large', 'small', 'row'])
// What a tool that declares nothing has always rendered as. Changing either
// constant changes every undeclared tool, which is why they live here.
export const DEFAULT_TOOL_ICON = 'toolbox'
export const DEFAULT_TOOL_SIZE = 'large'

const TABS = new Set(RIBBON_TAB_IDS)
const SIZES = new Set(PLACEMENT_SIZES)

/**
 * Does this tool mutate the drawing? The ONE definition.
 * Fails closed: anything that is not a record with a `drawing.write` string in
 * its capabilities array reads as a read tool, never as an unguarded write.
 */
export function isWriteTool(tool) {
  const caps = tool && tool.capabilities
  return Array.isArray(caps) && caps.includes(WRITE_CAPABILITY)
}

/**
 * The tool's CockpitIcon key, or the shared default.
 * The key is not validated against the sprite here — CockpitIcon already
 * degrades a miss to an honest monogram, and cockpitIcons.test.js is the gate
 * that keeps declared keys real.
 */
export function toolIcon(tool) {
  const icon = tool && tool.icon
  return typeof icon === 'string' && icon ? icon : DEFAULT_TOOL_ICON
}

/**
 * The ribbon tab this tool asks for, or '' when it asks for none.
 * An unknown tab reads as none, so a bad record leaves the tool exactly where
 * it renders today instead of vanishing from the ribbon.
 */
export function toolPlacementTab(tool) {
  const tab = tool && tool.placement && tool.placement.tab
  return typeof tab === 'string' && TABS.has(tab) ? tab : ''
}

/** The tool's ribbon size, or the shared default. Unknown size -> default. */
export function toolPlacementSize(tool) {
  const size = tool && tool.placement && tool.placement.size
  return typeof size === 'string' && SIZES.has(size) ? size : DEFAULT_TOOL_SIZE
}
