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
// Shared defaults for every tool-record consumer.
export const DEFAULT_TOOL_ICON = 'toolbox'
export const DEFAULT_TOOL_SIZE = 'large'

const TABS = new Set(RIBBON_TAB_IDS)
const SIZES = new Set(PLACEMENT_SIZES)

// Standardization slice 8c: `mcp_source.server_id` mirrors the exact shape
// server/tenant_mcp_store.py mints (`secrets.token_hex(12)`, 24 lowercase hex
// chars) and server/tool_record_fields.py validates against the same shape.
// `tool` is bounded the same as that module's MAX_MCP_TOOL_LEN.
const MCP_SERVER_ID_RE = /^[0-9a-f]{24}$/
export const MAX_MCP_TOOL_LEN = 64

// Bounded like tool_record_fields.py's _WARNED ledger: a catalog full of bad
// rows must stop growing this, not leak one console.warn per bad tool forever.
const MCP_SOURCE_WARN_MAX = 512
const _mcpSourceWarned = new Set()

function warnMcpSourceDropped(name) {
  if (_mcpSourceWarned.has(name) || _mcpSourceWarned.size >= MCP_SOURCE_WARN_MAX) return
  _mcpSourceWarned.add(name)
  try {
    if (import.meta.env?.DEV) {
      console.warn(`[toolRecord] mcp_source dropped for tool "${name}": malformed`)
    }
  } catch { /* a warning never breaks the caller */ }
}

/** Test seam: forget which tools already warned about a dropped mcp_source. */
export function resetMcpSourceWarnLedger() {
  _mcpSourceWarned.clear()
}

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
 * The tool's declared CockpitIcon key, or the shared fallback.
 */
export function toolIcon(tool) {
  const icon = tool && tool.icon
  return typeof icon === 'string' && icon ? icon : DEFAULT_TOOL_ICON
}

/** Studio-only solar category glyphs; undeclared tools are text buttons. */
export function studioToolIcon(tool) {
  const icon = tool && tool.icon
  const solar = /solar|panel|zone|string|inverter|trench/i.test(tool?.name || '')
  if (solar) {
    const name = tool.name
    if (/panel/i.test(name)) return 'grid'
    if (/zone/i.test(name)) return 'rectangle'
    if (/string/i.test(name)) return 'polyline'
    if (/inverter/i.test(name)) return 'block-insert'
    if (/trench/i.test(name)) return 'line'
  }
  return typeof icon === 'string' && icon ? icon : ''
}

// Presentation metadata beside the tool schema readers. Never infer a value
// or convert units: the proposal supplies the numbers in these declared units.
export const humanKey = Object.freeze({
  panelsPerString: { label: 'Panels per string' },
  panels_in_scope: { label: 'Panels in scope' },
  panelsInScope: { label: 'Panels in scope' },
  module: { label: 'Module' },
  module_name: { label: 'Module' },
  design_min_temp: { label: 'Design min temp', unit: '°C' },
  designMinTemp: { label: 'Design min temp', unit: '°C' },
  design_min_temp_c: { label: 'Design min temp', unit: '°C' },
  min_temp_c: { label: 'Design min temp', unit: '°C' },
  max_system_v: { label: 'Max system V', unit: 'V' },
  maxSystemV: { label: 'Max system V', unit: 'V' },
  max_system_voltage: { label: 'Max system V', unit: 'V' },
  max_system_voltage_v: { label: 'Max system V', unit: 'V' },
  module_power_w: { label: 'Module power', unit: 'W' },
  voc: { label: 'Open circuit voltage', unit: 'V' },
  spacing: { label: 'Spacing' },
})

export function proposalField(key, schema = {}) {
  const mapped = humanKey[key]
  const words = key.replace(/([a-z0-9])([A-Z])/g, '$1 $2').replace(/[_-]+/g, ' ').toLowerCase()
  return { label: mapped?.label || words.charAt(0).toUpperCase() + words.slice(1),
    unit: mapped?.unit || schema.unit || schema.units || '' }
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

/**
 * The tool's `{ server_id, tool }` mcp_source, or null.
 * Mirrors server/tool_record_fields.py's validate_mcp_source: server_id must
 * look like a registry id (the exact shape server/tenant_mcp_store.py mints);
 * tool must be a non-empty bounded string; no other keys. An invalid PRESENT
 * value is dropped (never rendered) and warned once per tool — the same
 * drop-with-warning discipline as the server's fold-tier read, not the
 * silent-default discipline toolIcon/toolPlacementTab use, because a
 * malformed mcp_source is never a legitimate "no preference" the way a bad
 * icon or placement is.
 */
export function toolMcpSource(tool) {
  const raw = tool && tool.mcp_source
  if (raw == null) return null
  const name = (tool && tool.name) || '<unnamed>'
  if (typeof raw !== 'object' || Array.isArray(raw)) {
    warnMcpSourceDropped(name)
    return null
  }
  const keys = Object.keys(raw)
  if (keys.some((key) => key !== 'server_id' && key !== 'tool')) {
    warnMcpSourceDropped(name)
    return null
  }
  const serverId = raw.server_id
  if (typeof serverId !== 'string' || !MCP_SERVER_ID_RE.test(serverId)) {
    warnMcpSourceDropped(name)
    return null
  }
  const toolName = raw.tool
  if (typeof toolName !== 'string' || !toolName || toolName.length > MAX_MCP_TOOL_LEN) {
    warnMcpSourceDropped(name)
    return null
  }
  return { server_id: serverId, tool: toolName }
}
