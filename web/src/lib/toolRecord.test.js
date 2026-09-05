// @vitest-environment node
//
// The tool record's readers. `isWriteTool` used to be inlined three times (the
// ribbon, the tools panel, the slash picker), which is three chances for the
// write dot, the write gate and the write lock to disagree about the same
// record. One predicate, one test.
import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  DEFAULT_TOOL_ICON,
  DEFAULT_TOOL_SIZE,
  PLACEMENT_SIZES,
  RIBBON_TAB_IDS,
  isWriteTool,
  resetMcpSourceWarnLedger,
  toolIcon,
  toolMcpSource,
  toolPlacementSize,
  toolPlacementTab,
} from './toolRecord.js'

describe('isWriteTool', () => {
  it('is true only for a record whose capabilities include drawing.write', () => {
    expect(isWriteTool({ capabilities: ['drawing.write'] })).toBe(true)
    expect(isWriteTool({ capabilities: ['drawing.read', 'drawing.write'] })).toBe(true)
    expect(isWriteTool({ capabilities: ['drawing.read'] })).toBe(false)
  })

  it('fails CLOSED on anything that is not a capabilities array', () => {
    for (const tool of [null, undefined, {}, { capabilities: null }, { capabilities: 'drawing.write' },
      { capabilities: {} }, 'drawing.write', 0]) {
      expect(isWriteTool(tool)).toBe(false)
    }
  })
})

describe('toolIcon', () => {
  it('prefers the record and falls back to the shared default', () => {
    expect(toolIcon({ icon: 'layers' })).toBe('layers')
    expect(toolIcon({})).toBe(DEFAULT_TOOL_ICON)
    expect(toolIcon({ icon: '' })).toBe(DEFAULT_TOOL_ICON)
    expect(toolIcon({ icon: 7 })).toBe(DEFAULT_TOOL_ICON)
    expect(toolIcon(null)).toBe(DEFAULT_TOOL_ICON)
  })
})

describe('toolPlacementTab', () => {
  it('accepts exactly the tabs the cockpit band declares', () => {
    for (const tab of RIBBON_TAB_IDS) {
      expect(toolPlacementTab({ placement: { tab } })).toBe(tab)
    }
  })

  it('reads an unknown or absent tab as "no placement", never as a lost tool', () => {
    // `model` IS declared in RIBBON_TABS but carries a reason there, so it is
    // never selectable and never a valid placement target.
    for (const placement of [{ tab: 'model' }, { tab: 'nope' }, { tab: 3 }, {}, null]) {
      expect(toolPlacementTab({ placement })).toBe('')
    }
    expect(toolPlacementTab({})).toBe('')
    expect(toolPlacementTab(null)).toBe('')
  })
})

describe('toolPlacementSize', () => {
  it('accepts the ribbon sizes and defaults everything else', () => {
    for (const size of PLACEMENT_SIZES) {
      expect(toolPlacementSize({ placement: { size } })).toBe(size)
    }
    for (const placement of [{ size: 'huge' }, { size: 2 }, {}, null]) {
      expect(toolPlacementSize({ placement })).toBe(DEFAULT_TOOL_SIZE)
    }
    expect(toolPlacementSize(null)).toBe(DEFAULT_TOOL_SIZE)
  })
})

// Standardization slice 8c. Mirrors server/tool_record_fields.py's
// validate_mcp_source: the same registry-id shape, the same bound on `tool`,
// the same drop-with-one-warning discipline (server/tests/test_tool_record_fields.py
// runs the equivalent table server-side).
describe('toolMcpSource', () => {
  const VALID_SERVER_ID = 'abcdef0123456789abcdef01' // 24 lowercase hex chars

  afterEach(() => {
    resetMcpSourceWarnLedger()
    vi.restoreAllMocks()
  })

  it('is null when the record declares no mcp_source', () => {
    expect(toolMcpSource({})).toBeNull()
    expect(toolMcpSource(null)).toBeNull()
    expect(toolMcpSource({ mcp_source: null })).toBeNull()
  })

  it('reads a valid mcp_source through unchanged', () => {
    const tool = { name: 't', mcp_source: { server_id: VALID_SERVER_ID, tool: 'list-items' } }
    expect(toolMcpSource(tool)).toEqual({ server_id: VALID_SERVER_ID, tool: 'list-items' })
  })

  it('drops a hostile server_id', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const tool = { name: 't', mcp_source: { server_id: 'not-hex-shaped', tool: 'list-items' } }
    expect(toolMcpSource(tool)).toBeNull()
    expect(warn).toHaveBeenCalledTimes(1)
  })

  it('drops a hostile tool name (empty, over bound, or not a string)', () => {
    vi.spyOn(console, 'warn').mockImplementation(() => {})
    for (const tool of ['', 'x'.repeat(65), 7, null]) {
      resetMcpSourceWarnLedger()
      expect(toolMcpSource({ name: 't', mcp_source: { server_id: VALID_SERVER_ID, tool } })).toBeNull()
    }
  })

  it('drops a non-object mcp_source', () => {
    vi.spyOn(console, 'warn').mockImplementation(() => {})
    for (const raw of ['a-string', 7, ['array'], true]) {
      resetMcpSourceWarnLedger()
      expect(toolMcpSource({ name: 't', mcp_source: raw })).toBeNull()
    }
  })

  it('drops an mcp_source carrying an unknown key', () => {
    vi.spyOn(console, 'warn').mockImplementation(() => {})
    const tool = { name: 't', mcp_source: { server_id: VALID_SERVER_ID, tool: 'list-items', extra: 1 } }
    expect(toolMcpSource(tool)).toBeNull()
  })

  it('warns at most once per tool per process, naming the tool', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const tool = { name: 'flaky-tool', mcp_source: { server_id: 'nope' } }
    for (let i = 0; i < 5; i++) toolMcpSource(tool)
    expect(warn).toHaveBeenCalledTimes(1)
    expect(warn.mock.calls[0][0]).toContain('flaky-tool')
  })
})
