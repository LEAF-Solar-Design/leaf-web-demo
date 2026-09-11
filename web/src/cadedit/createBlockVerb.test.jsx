import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { BLOCK_OPS, CREATE_OPS, CREATING_EDITS, buildCreatePayload } from './engineSession.js'
import { acceptsCommand } from './CommandLineArmer.jsx'
import { PROMPTS } from './promptKeys.js'
import { resolvePromptInputs } from './promptInputs.js'
import { parseDrawingCommand } from '../lib/commandWords.js'
import { parseScript } from './script.js'
import { byId, DEFERRED_REASONS } from '../lib/actionRegistry.js'
import ScriptPanel from './ScriptPanel.jsx'

const scriptContext = vi.hoisted(() => ({ value: null }))
vi.mock('./EngineSessionProvider.jsx', async (original) => ({
  ...await original(), useEngineSessionContext: () => scriptContext.value,
}))
afterEach(cleanup)

const line = { id: '16', type: 'LINE', layer: '0', vertices: [[12, 23, 0], [17, 23, 0]], normal: [0, 0, 1] }
const circle = { id: '17', type: 'CIRCLE', layer: '0', vertices: [[11, 24, 0]], radius: 2, normal: [0, 0, 1] }
const inputs = { name: 'B', x: '10', y: '20', members: '17' }
const context = (entities = [line, circle], committedEntities = [line, circle]) => ({ selectedId: '16', entities, committedEntities })
const build = (ctx = context(), values = inputs, blocks = []) => buildCreatePayload('createBlock', values, blocks, [], ctx)

describe('Create Block from cockpit entities', () => {
  it('keeps block replacement outside the closed Draw and creating-edit censuses', () => {
    expect(BLOCK_OPS).toEqual({ block: 'createBlock' })
    expect(Object.isFrozen(BLOCK_OPS)).toBe(true)
    expect(CREATE_OPS).not.toContain(BLOCK_OPS.block)
    expect(CREATING_EDITS).not.toContain(BLOCK_OPS.block)
  })
  it('carries the selection first, preserves the base and fixes the INSERT layer', () => {
    expect(build()).toEqual({ payload: { name: 'B', x: 10, y: 20, members: ['16', '17'], layer: '0' } })
    expect(build(context(), { ...inputs, members: '' }).payload.members).toEqual(['16'])
  })
  it('refuses 61 distinct members', () => {
    const entities = Array.from({ length: 61 }, (_, i) => ({ ...line, id: String(16 + i) }))
    expect(build(context(entities, entities), { ...inputs, members: entities.slice(1).map((e) => e.id).join(' ') }).refusal).toContain('1 to 60')
  })
  it.each([
    [{ type: 'INSERT' }, 'only LINE'],
    [{ type: 'LWPOLYLINE', bulges: [1, 0] }, 'straight'],
    [{ type: 'LWPOLYLINE', constantWidth: 2 }, 'widths must be zero'],
    [{ type: 'LWPOLYLINE', startWidths: [2, 0] }, 'widths must be zero'],
    [{ type: 'LWPOLYLINE', endWidths: [0, 2] }, 'widths must be zero'],
    [{ type: 'CIRCLE', radius: 2, normal: [0, 1, 0] }, 'normal +Z'],
    [{ aci: 0 }, 'ByBlock'],
    [{ linetype: 'ByBlock' }, 'ByBlock'],
    [{ lineweight: -2 }, 'ByBlock'],
    [{ modelSpace: false }, 'model space'],
  ])('refuses an ineligible member %j', (change, rule) => {
    expect(build(context([{ ...line, ...change }, circle])).refusal).toContain(rule)
  })
  it('refuses a group member', () => {
    const entities = [line, circle]
    entities.groups = [{ name: 'RACK', memberIds: ['16', '17'] }]
    expect(build(context(entities)).refusal).toContain('ungroup')
  })
  it('admits an unsaved member beside a committed member', () => {
    expect(build(context([line, circle], [circle]))).toEqual({
      payload: { name: 'B', x: 10, y: 20, members: ['16', '17'], layer: '0' },
    })
  })
  it('admits moved and relayered committed members', () => {
    expect(build(context([{ ...line, layer: 'SITE', vertices: [[2, 3, 0], [7, 3, 0]], linetype: 'bylayer' }, circle])).payload.members)
      .toEqual(['16', '17'])
  })
  it('refuses changed committed colour, linetype and lineweight with the property sentence', () => {
    for (const change of [{ aci: 3 }, { trueColor: [1, 2, 3] }, { linetype: 'DASHED' }, { lineweight: 25 }]) {
      expect(build(context([{ ...line, ...change }, circle])).refusal)
        .toBe('Create block refused: block members keep their colour, linetype and lineweight; change them after the block exists.')
    }
  })
  it('admits eligible hand-import members with null committedEntities', () => {
    expect(build(context([line, circle], null)).payload.members).toEqual(['16', '17'])
  })
  it('refuses a collision and reserved name punctuation, deferring a truncated catalogue to the crate', () => {
    const blocks = [{ name: 'RACK' }]
    expect(build(context(), { ...inputs, name: 'rack' }, blocks).refusal).toContain('already exists')
    expect(build(context(), { ...inputs, name: 'bad<name' }).refusal).toContain('reserved punctuation')
    const ctx = context()
    ctx.entities.blocksTruncated = true
    expect(build(ctx, { ...inputs, name: 'rack' }, blocks).payload.name).toBe('rack')
  })
  it('arms B and BLOCK through the real Draw record and leaves no deferred reason', () => {
    for (const word of ['B', 'BLOCK']) {
      const command = parseDrawingCommand(word)
      expect(command).toMatchObject({ group: 'draw', op: 'createBlock', verb: 'BLOCK' })
      expect(acceptsCommand(command)).toBe(true)
    }
    expect(byId('draw:createBlock')).toMatchObject({ panel: 'block', icon: 'block-create', label: 'create block' })
    expect(DEFERRED_REASONS).not.toHaveProperty('blockCreate')
  })
  it('takes script handles in hex with the first becoming the selection', () => {
    const parsed = parseScript('BLOCK B 10,20 10 11', parseDrawingCommand, PROMPTS)
    expect(parsed.lines[0].inputs).toMatchObject({ name: 'B', x: '10', y: '20', selectedId: '16', members: '17', membersDone: 'true' })
    const resolved = resolvePromptInputs(PROMPTS.createBlock, parsed.lines[0].inputs)
    expect(resolved.waitingStep).toBeNull()
    expect(build(context(), resolved.effective).payload.members).toEqual(['16', '17'])
    expect(parseScript('BLOCK B 10,20 nope', parseDrawingCommand, PROMPTS).refusal).toContain('hexadecimal')
    const unknown = parseScript('BLOCK B 10,20 FFFF', parseDrawingCommand, PROMPTS)
    expect(unknown.refusal).toBeUndefined()
    expect(build(context(), unknown.lines[0].inputs).refusal).toContain('only LINE')
  })
  it('stops a refused script line before any later command runs', () => {
    const create = vi.fn()
    scriptContext.value = { session: { ...context(), engineParsed: true, actions: { create } }, inputs: {}, reach: null }
    render(<ScriptPanel />)
    fireEvent.change(screen.getByLabelText('ribbon script'), { target: { value: 'BLOCK B 10,20 10 FFFF\nLINE 0,0 1,1' } })
    fireEvent.click(screen.getByTestId('cockpit-script-run'))
    expect(screen.getByTestId('cockpit-script-status').textContent).toContain('Script stopped at line 1: Create block refused: only LINE')
    expect(create).not.toHaveBeenCalled()
  })
})
