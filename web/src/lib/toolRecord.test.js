// @vitest-environment node
//
// The tool record's readers. `isWriteTool` used to be inlined three times (the
// ribbon, the tools panel, the slash picker), which is three chances for the
// write dot, the write gate and the write lock to disagree about the same
// record. One predicate, one test.
import { describe, expect, it } from 'vitest'

import {
  DEFAULT_TOOL_ICON,
  DEFAULT_TOOL_SIZE,
  PLACEMENT_SIZES,
  RIBBON_TAB_IDS,
  isWriteTool,
  toolIcon,
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
