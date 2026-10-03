import { describe, expect, it, vi } from 'vitest'
import { decodeGroundSlots, decodeGroundFrameSlots, GROUND_SLOT_CODEC } from './solarGroundSlots.js'

const template = () => ({ rev: 0, provenance: {}, validity: {}, extra: {} })
const id = (n) => `leaf:panel:00000000-0000-4000-8000-${n.toString(16).padStart(12, '0')}`
const binary = (bytes) => {
  let text = ''
  // Chunk conversion avoids argument-count limits for the real boundary payloads.
  for (let index = 0; index < bytes.length; index += 4096) {
    text += String.fromCharCode(...bytes.subarray(index, index + 4096))
  }
  return btoa(text)
}
const block = (count = 1, start = 1) => {
  const ids = new Uint8Array(count * 16)
  const centres = new Uint8Array(count * 16)
  const view = new DataView(centres.buffer)
  for (let index = 0; index < count; index += 1) {
    const n = start + index
    ids[index * 16 + 6] = 0x40
    ids[index * 16 + 8] = 0x80
    ids[index * 16 + 13] = (n >>> 16) & 255
    ids[index * 16 + 14] = (n >>> 8) & 255
    ids[index * 16 + 15] = n & 255
    view.setFloat64(index * 16, n, true)
    view.setFloat64(index * 16 + 8, -n, true)
  }
  return { codec: GROUND_SLOT_CODEC, count, panel_ids: binary(ids), centres: binary(centres),
    angle: 0, panel: template() }
}
const frame = (ground_slots, frameId = 'F') => ({ id: frameId, ground_slots })
const refuse = (value) => expect(decodeGroundSlots(value)).toBeNull()
const accepted = (value, count = value.count, start = 1) => {
  const decoded = decodeGroundSlots(value)
  expect(decoded).not.toBeNull()
  expect(decoded.panelIds).toEqual(Array.from({ length: count }, (_, index) => id(start + index)))
  expect(decoded.slots).toEqual(Array.from({ length: count }, (_, index) => ({ panelId: id(start + index),
    slotNumber: index + 1, centre: { x: start + index, y: -(start + index) } })))
  expect(decoded.angle).toBe(value.angle)
  expect(Object.keys(decoded)).toEqual(['angle', 'slots', 'panelIds'])
}
const rowVariants = (mutate) => {
  for (const field of ['panel_ids', 'centres']) {
    const value = block()
    value[field] = mutate(value[field])
    refuse(value)
  }
}
const mutateId = (offset, byte) => {
  const value = block()
  const bytes = atob(value.panel_ids)
  value.panel_ids = btoa(bytes.slice(0, offset) + String.fromCharCode(byte) + bytes.slice(offset + 1))
  return value
}
const centreX = (x) => {
  const value = block()
  const bytes = new Uint8Array(16)
  const view = new DataView(bytes.buffer)
  view.setFloat64(0, x, true)
  view.setFloat64(8, -1, true)
  value.centres = binary(bytes)
  return value
}
const centreY = (y) => {
  const value = block()
  const bytes = new Uint8Array(16)
  const view = new DataView(bytes.buffer)
  view.setFloat64(0, 1, true)
  view.setFloat64(8, y, true)
  value.centres = binary(bytes)
  return value
}
const nested = (wrappers) => {
  let value = 0
  for (let index = 0; index < wrappers; index += 1) value = [value]
  const result = block()
  result.panel.extra = { a: value }
  return result
}
const tenFrames = () => Array.from({ length: 10 }, (_, index) => frame(block(10000, 1 + index * 10000), `F${index}`))

describe('shared compact Ground slot decoder', () => {
  it('GS1 decodes the exact server block and one frame', () => {
    const value = block()
    expect(value).toEqual({ codec: GROUND_SLOT_CODEC, count: 1,
      panel_ids: 'AAAAAAAAQACAAAAAAAAAAQ==', centres: 'AAAAAAAA8D8AAAAAAADwvw==', angle: 0, panel: template() })
    accepted(value)
    expect(decodeGroundFrameSlots([frame(value)])).toEqual({ frames: [{ frameIndex: 0, frameId: 'F',
      angle: 0, slots: [{ panelId: id(1), slotNumber: 1, centre: { x: 1, y: -1 } }] }], panelIds: [id(1)] })
  })
  it('GS2 preserves frame and slot order with local slot numbers', () => {
    const decoded = decodeGroundFrameSlots([frame(block()), frame(block(2, 2), 'F2')])
    expect(decoded.panelIds).toEqual([id(1), id(2), id(3)])
    expect(decoded.frames.map((item) => item.slots.map((slot) => slot.slotNumber))).toEqual([[1], [1, 2]])
    expect(decoded.frames.flatMap((item) => item.slots.map((slot) => slot.centre)))
      .toEqual([{ x: 1, y: -1 }, { x: 2, y: -2 }, { x: 3, y: -3 }])
    // Three rows have canonical base64 without any padding.
    accepted(block(3))
  })
  it('GS3 requires codec', () => { const value = block(); delete value.codec; refuse(value) })
  it('GS4 requires count', () => { const value = block(); delete value.count; refuse(value) })
  it('GS5 requires panel_ids', () => { const value = block(); delete value.panel_ids; refuse(value) })
  it('GS6 requires centres', () => { const value = block(); delete value.centres; refuse(value) })
  it('GS7 requires angle', () => { const value = block(); delete value.angle; refuse(value) })
  it('GS8 requires panel', () => { const value = block(); delete value.panel; refuse(value) })
  it('GS9 refuses extra block keys', () => refuse({ ...block(), unexpected: null }))
  it('GS10 requires the v1 codec', () => refuse({ ...block(), codec: 'leaf.solar-ground-slots.v2' }))
  it('GS11 requires a plain block', () => { for (const value of [null, [], 'x', 7, new Date()]) refuse(value) })
  it('GS12 requires a positive integer count', () => {
    for (const count of [0, true, 1.5]) refuse({ ...block(), count })
  })
  it('GS13 requires row lengths matching count', () => refuse({ ...block(), count: 2 }))
  it('GS14 requires text in both columns', () => rowVariants(() => null))
  it('GS15 refuses missing padding', () => rowVariants((text) => text.replace(/=+$/, '')))
  it('GS16 refuses extra padding', () => rowVariants((text) => text + '='))
  it('GS17 refuses the URL-safe alphabet', () => rowVariants((text) => '_' + text.slice(1)))
  it('GS18 refuses non-ASCII base64', () => rowVariants((text) => 'é' + text.slice(1)))
  it('GS19 refuses whitespace without normalizing', () => rowVariants((text) => ' ' + text.slice(1)))
  it('GS20 refuses an extra decoded byte at the same text length', () => rowVariants((text) => btoa(atob(text) + '\0')))
  it('GS21 refuses noncanonical padding bits', () => rowVariants((text) =>
    text.slice(0, -3) + String.fromCharCode(text.charCodeAt(text.length - 3) + 1) + '=='))
  it('GS22 requires UUID version four', () => refuse(mutateId(6, 0x10)))
  it('GS23 requires the UUID RFC variant', () => refuse(mutateId(8, 0xc0)))
  it('GS24 refuses duplicate IDs inside a block', () => {
    const value = block(2)
    value.panel_ids = btoa(atob(block().panel_ids).repeat(2))
    refuse(value)
  })
  it('GS25 refuses IDs repeated across frames', () => {
    expect(decodeGroundFrameSlots([frame(block(), 'F1'), frame(block(), 'F2')])).toBeNull()
  })
  it('GS26 refuses a NaN centre', () => refuse(centreX(NaN)))
  it('GS27 refuses infinite centres', () => { for (const x of [Infinity, -Infinity]) refuse(centreX(x)) })
  it('GS28 refuses nonfinite angles', () => {
    for (const angle of [NaN, Infinity, -Infinity]) refuse({ ...block(), angle })
  })
  it('GS29 refuses boolean angles', () => refuse({ ...block(), angle: true }))
  it('GS30 preserves large finite angles', () => accepted({ ...block(), angle: 1e300 }))
  it('GS31 requires every template key', () => {
    for (const key of ['rev', 'provenance', 'validity', 'extra']) {
      const value = block(); delete value.panel[key]; refuse(value)
    }
  })
  it('GS32 refuses extra template keys', () => {
    const value = block(); value.panel.unexpected = null; refuse(value)
  })
  it('GS33 bounds the integer template revision', () => {
    for (const rev of [-1, 1000001, true, 0.5]) {
      const value = block(); value.panel.rev = rev; refuse(value)
    }
    const value = block(); value.panel.rev = 1000000; accepted(value)
  })
  it('GS34 requires template dictionaries', () => {
    for (const key of ['provenance', 'validity', 'extra']) {
      const value = block(); value.panel[key] = []; refuse(value)
    }
  })
  it('GS35 refuses nonfinite template numbers', () => {
    for (const n of [Infinity, -Infinity, NaN, undefined, 1n, () => 1, new Date()]) {
      const value = block(); value.panel.extra = { n }; refuse(value)
    }
    const value = block(); value.panel.extra = { a: [null, true, false, 'text', 0] }; accepted(value)
  })
  it('GS36 refuses lone surrogates in strings and keys', () => {
    for (const extra of [{ s: '\ud800' }, { '\ud800': null }, { s: '\udc00' }]) {
      const value = block(); value.panel.extra = extra; refuse(value)
    }
    const value = block(); value.panel.extra = { s: 'é😀' }; accepted(value)
  })
  it('GS37 bounds dictionary cardinality', () => {
    const value = block()
    value.panel.extra = Object.fromEntries(Array.from({ length: 100001 }, (_, index) => [String(index), null]))
    refuse(value)
  })
  it('GS38 bounds list cardinality', () => {
    const value = block(); value.panel.extra = { a: Array(100001).fill(null) }; refuse(value)
  })
  it('GS39 accepts template depth thirty-two', () => accepted(nested(30)))
  it('GS40 refuses template depth thirty-three', () => refuse(nested(31)))
  it('GS41 bounds all visited template nodes', () => {
    const value = block()
    value.panel.extra = { a: Array.from({ length: 5 }, () => Array(99999).fill(null)) }
    refuse(value)
  })
  it('GS42 accepts the exact template byte budget', () => {
    const value = block(); value.panel.extra = { s: 'x'.repeat(16777165) }; accepted(value)
  })
  it('GS43 refuses the next template byte', () => {
    const value = block(); value.panel.extra = { s: 'x'.repeat(16777166) }; refuse(value)
    // Accounting is UTF-8 bytes, rather than UTF-16 code units.
    value.panel.extra = { s: 'é'.repeat(8388583) }; refuse(value)
  })
  it('GS44 accepts every ID and centre at the inclusive frame bound', () => accepted(block(10000)))
  it('GS45 refuses real rows above the frame bound', () => {
    const value = block(10001)
    expect(value.panel_ids).toHaveLength(213356)
    expect(value.centres).toHaveLength(213356)
    refuse(value)
  })
  it('GS46 accepts every ID at the inclusive graph bound', () => {
    const frames = tenFrames()
    expect(frames.reduce((sum, item) => sum + item.ground_slots.panel_ids.length + item.ground_slots.centres.length, 0))
      .toBe(4266720)
    const decoded = decodeGroundFrameSlots(frames)
    expect(decoded.panelIds).toEqual(Array.from({ length: 100000 }, (_, index) => id(index + 1)))
    expect(decoded.frames).toHaveLength(10)
    for (let index = 0; index < 10; index += 1) {
      expect(decoded.frames[index].slots).toEqual(Array.from({ length: 10000 }, (_, slot) => ({
        panelId: id(index * 10000 + slot + 1), slotNumber: slot + 1,
        centre: { x: index * 10000 + slot + 1, y: -(index * 10000 + slot + 1) },
      })))
    }
  })
  it('GS47 refuses real rows above the graph bound', () => {
    expect(decodeGroundFrameSlots([...tenFrames(), frame(block(1, 100001), 'F11')])).toBeNull()
  })
  it('GS48 accepts the parsed uint64 representation', () => {
    const value = block(); value.panel.extra = { n: JSON.parse('18446744073709551615') }; accepted(value)
  })
  it('GS49 accepts large finite template floats', () => {
    const value = block(); value.panel.extra = { n: 1e30 }; accepted(value)
  })
  it('GS50 selects only plain frames with own blocks and fails closed', () => {
    for (const frames of [null, {}, 'x', 7]) expect(decodeGroundFrameSlots(frames)).toBeNull()
    const inherited = Object.create({ ground_slots: block() })
    const frames = [null, [], 'x', new Date(), inherited, {}, frame(block())]
    expect(decodeGroundFrameSlots(frames).panelIds).toEqual([id(1)])
    expect(decodeGroundFrameSlots([...frames, { ground_slots: null }])).toBeNull()
    expect(decodeGroundFrameSlots([frame(block()), { ground_slots: undefined }])).toBeNull()
  })
  it('GS51 preserves original indices and optional string frame IDs', () => {
    const decoded = decodeGroundFrameSlots([{}, frame(block(), 'F1'), null,
      { ground_slots: block(1, 2) }, frame(block(1, 3), 7)])
    expect(decoded.frames.map(({ frameIndex, frameId }) => ({ frameIndex, frameId })))
      .toEqual([{ frameIndex: 1, frameId: 'F1' }, { frameIndex: 3, frameId: null }, { frameIndex: 4, frameId: null }])
  })
  it('GS52 preflights the aggregate before decoding any row', () => {
    const frames = [...tenFrames(), frame(block(1, 100001))]
    const spy = vi.spyOn(globalThis, 'atob')
    try {
      expect(decodeGroundFrameSlots(frames)).toBeNull()
      expect(spy).not.toHaveBeenCalled()
      const valid = frame(block())
      expect(decodeGroundFrameSlots([valid, frame({ ...block(1, 2), angle: NaN })])).toBeNull()
      expect(spy).not.toHaveBeenCalled()
    } finally { spy.mockRestore() }
  })
  it('GS53 returns exactly empty collections without selected blocks', () => {
    for (const frames of [[], [{}, null, { id: 'F' }]]) {
      expect(decodeGroundFrameSlots(frames)).toEqual({ frames: [], panelIds: [] })
    }
  })
  it('GS54 returns fresh results without mutation or partial refusals', () => {
    const value = block(2)
    const frames = [frame(value)]
    const before = structuredClone(frames)
    const first = decodeGroundFrameSlots(frames)
    const second = decodeGroundFrameSlots(frames)
    expect(first).toEqual(second)
    expect(first).not.toBe(second)
    expect(first.frames[0]).not.toBe(second.frames[0])
    expect(first.frames[0].slots[0].centre).not.toBe(second.frames[0].slots[0].centre)
    first.frames[0].slots[0].centre.x = 99
    first.panelIds.reverse()
    expect(frames).toEqual(before)
    accepted(value, 2)
    const direct = decodeGroundSlots(value)
    expect(direct.slots).not.toBe(decodeGroundSlots(value).slots)
    expect(decodeGroundFrameSlots([...frames, frame(block())])).toBeNull()
    expect(frames).toEqual(before)
  })
  it('GS55 refuses a zero count even when both row texts match it', () => {
    refuse({ ...block(), count: 0, panel_ids: '', centres: '' })
  })
  it('GS56 refuses a nonfinite y centre', () => {
    for (const y of [NaN, Infinity, -Infinity]) refuse(centreY(y))
    accepted(centreY(-1))
  })
  it('GS57 charges four bytes for an astral character', () => {
    const value = block(); value.panel.extra = { s: '\u{1F600}'.repeat(4194291) + 'x' }; accepted(value)
    value.panel.extra = { s: '\u{1F600}'.repeat(4194291) + 'xx' }; refuse(value)
  })
  it('GS58 charges five bytes for null and each boolean', () => {
    for (const n of [null, true, false]) {
      const value = block(); value.panel.extra = { n, s: 'x'.repeat(16777159) }; accepted(value)
      value.panel.extra = { n, s: 'x'.repeat(16777160) }; refuse(value)
    }
  })
  it('GS59 accepts exactly one hundred thousand entries and elements', () => {
    const value = block()
    value.panel.extra = Object.fromEntries(Array.from({ length: 100000 }, (_, index) => [String(index), null]))
    accepted(value)
    value.panel.extra = { a: Array(100000).fill(null) }
    accepted(value)
  })
  it('GS60 accepts exactly five hundred thousand template nodes', () => {
    const value = block()
    value.panel.extra = { a: [...Array.from({ length: 4 }, () => Array(99999).fill(null)), Array(99988).fill(null)] }
    accepted(value)
  })
  it('GS61 checks every template and row text before decoding any row', () => {
    const surrogate = block(1, 2); surrogate.panel.extra = { s: '\ud800' }
    const empty = block(1, 2); empty.centres = ''
    const cases = [[frame(block()), frame(surrogate, 'F2')], [frame(block()), frame(empty, 'F2')]]
    const spy = vi.spyOn(globalThis, 'atob')
    try {
      for (const frames of cases) expect(decodeGroundFrameSlots(frames)).toBeNull()
      expect(spy).not.toHaveBeenCalled()
    } finally { spy.mockRestore() }
  })
})
