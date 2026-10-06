import { describe, expect, it } from 'vitest'
import { SNAP_MODES, DEFAULT_SNAP_MODES, ALL_SNAP_MODES, snapModeBit, isSnapModeMask, snapModeEnabled } from './snapModes.js'

describe('snap modes', () => {
  it('OSM01 frozen menu descriptors', () => {
    expect(SNAP_MODES).toEqual([
      { kind: 'endpoint', bit: 1, label: 'Endpoint' },
      { kind: 'midpoint', bit: 2, label: 'Midpoint' },
      { kind: 'centre', bit: 4, label: 'Centre' },
      { kind: 'quadrant', bit: 16, label: 'Quadrant' },
      { kind: 'intersection', bit: 32, label: 'Intersection' },
      { kind: 'insertion', bit: 64, label: 'Insertion' },
      { kind: 'perpendicular', bit: 128, label: 'Perpendicular' },
      { kind: 'tangent', bit: 256, label: 'Tangent' },
      { kind: 'nearest', bit: 512, label: 'Nearest' },
    ])
    expect(Object.isFrozen(SNAP_MODES)).toBe(true)
    for (const mode of SNAP_MODES) expect(Object.isFrozen(mode)).toBe(true)
  })
  it('OSM02 default and all masks', () => {
    expect(DEFAULT_SNAP_MODES).toBe(23)
    expect(DEFAULT_SNAP_MODES).toBe(SNAP_MODES.slice(0, 4).reduce((mask, mode) => mask | mode.bit, 0))
    expect(ALL_SNAP_MODES).toBe(1015)
    expect(ALL_SNAP_MODES).toBe(SNAP_MODES.reduce((mask, mode) => mask | mode.bit, 0))
  })
  it('OSM03 kind lookup', () => {
    for (const mode of SNAP_MODES) expect(snapModeBit(mode.kind)).toBe(mode.bit)
    for (const kind of ['node', '', null, 'END']) expect(snapModeBit(kind)).toBe(0)
  })
  it('OSM04 mask validation', () => {
    for (const mask of [0, 1, 23, 1015]) expect(isSnapModeMask(mask)).toBe(true)
    for (const mask of [8, 1024, -1, 1.5, NaN, '23', null, 2 ** 53]) expect(isSnapModeMask(mask)).toBe(false)
  })
  it('OSM05 enabled lookup', () => {
    expect(snapModeEnabled(23, 'endpoint')).toBe(true)
    expect(snapModeEnabled(23, 'nearest')).toBe(false)
    expect(snapModeEnabled(1015, 'nearest')).toBe(true)
    expect(snapModeEnabled(0, 'endpoint')).toBe(false)
    expect(snapModeEnabled(8, 'endpoint')).toBe(false)
    expect(snapModeEnabled(23, 'node')).toBe(false)
  })
})

