import React from 'react'
import fs from 'node:fs'
import path from 'node:path'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import SolarStringComposer from './SolarStringComposer.jsx'
import ground from './__fixtures__/stringComposerGround.json'
import rooftop from './__fixtures__/stringComposerRooftop.json'
import { decodeGroundFrameSlots, GROUND_SLOT_CODEC } from './solarGroundSlots.js'
import { SOLAR_STRING_COMPOSER_REASONS, STRING_ADD_TOOL, STRING_MULTI_ADD_TOOL,
  composeStringRequest, stringComposerView } from './solarStringComposerModel.js'

vi.mock('./solarGroundSlots.js', async (importOriginal) => {
  const real = await importOriginal()
  return { ...real, decodeGroundFrameSlots: vi.fn(real.decodeGroundFrameSlots) }
})

afterEach(() => { cleanup(); vi.clearAllMocks() })
const envelope = (graph = ground, version = 7) => ({
  intake: { solar_design_graph: structuredClone(graph) }, version, head: version, latest: version,
})
const Q = rooftop.panels.slice(3).map((panel) => panel.id)
const button = (name) => screen.getByRole('button', { name })
const click = (name) => fireEvent.click(button(name))
const change = (label, value) => fireEvent.change(screen.getByLabelText(label), { target: { value } })
const queueRegion = () => screen.getByRole('region', { name: 'Selection queue' })
const queueLabels = () => within(queueRegion()).queryAllByRole('listitem').map((item) => item.querySelector('span').textContent)
const expected = (graph, toolName, queue, stringLength) => composeStringRequest({
  view: stringComposerView({ envelope: envelope(graph), drawingId: 'd1', drawingVersion: 7, projectId: null }),
  toolName, queue, stringLength,
}).params
const range = (frameIndex, fromSlot, toSlot) => ({ kind: 'range', frameIndex,
  frameId: ground.frames[frameIndex].id, fromSlot, toSlot })
const addRange = (index = 0, first = '1', last = '3') => {
  click(`Select row ${index + 1} Group ${index + 1}`)
  change('First slot', first); change('Last slot', last); click('Add range')
}
async function mount(graph = ground, overrides = {}) {
  const props = { row: { name: STRING_ADD_TOOL }, drawingId: 'd1', drawingVersion: 7,
    readIntake: vi.fn(async () => envelope(graph)), onSubmit: vi.fn(), ...overrides }
  const result = render(<SolarStringComposer {...props} />)
  await act(async () => {})
  return { ...result, props }
}
const deferred = () => {
  let resolve, reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
const binary = (bytes) => {
  let text = ''
  for (let index = 0; index < bytes.length; index += 4096) {
    text += String.fromCharCode(...bytes.subarray(index, index + 4096))
  }
  return btoa(text)
}
const block = (count, start = 1) => {
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
  return { codec: GROUND_SLOT_CODEC, count, panel_ids: binary(ids), centres: binary(centres), angle: 0,
    panel: { rev: 0, provenance: {}, validity: {}, extra: {} } }
}

// Any quoted module specifier naming the component, with or without a directory or the .jsx extension.
const composerSpecifier = /(['"`])(?:[^'"`\n]*\/)?SolarStringComposer(?:\.jsx)?\1/
const referencesComposer = (text) => text.includes('SolarStringComposer.jsx') || composerSpecifier.test(text)
const props7 = (overrides = {}) => ({ row: { name: STRING_ADD_TOOL }, drawingId: 'd1', drawingVersion: 7,
  onSubmit: vi.fn(), ...overrides })

describe('SolarStringComposer', () => {
  it('CMP0 mounted only in the step editor', () => {
    const src = fs.existsSync(path.resolve(process.cwd(), 'src/solar/SolarStringComposer.jsx'))
      ? path.resolve(process.cwd(), 'src') : path.resolve(process.cwd(), 'web/src')
    const hosts = []
    for (const relative of fs.readdirSync(src, { recursive: true })) {
      const normalized = relative.split(path.sep).join('/')
      const full = path.join(src, relative)
      if (normalized === 'solar/SolarStringComposer.jsx' || normalized.includes('__fixtures__/') || /\.test\./.test(normalized) || !fs.statSync(full).isFile()) continue
      const text = fs.readFileSync(full, 'utf8')
      if (referencesComposer(text)) hosts.push(normalized)
    }
    expect(hosts.sort()).toEqual(['solar/SolarStepEditor.jsx'])
  })

  it('CMP0b the unmounted check sees every import spelling', () => {
    for (const text of ["import Composer from './solar/SolarStringComposer'",
      'import Composer from "./SolarStringComposer"', "import X from '../solar/SolarStringComposer.jsx'",
      'const C = lazy(() => import(`./solar/SolarStringComposer`))', "export { default } from '@/solar/SolarStringComposer'"]) {
      expect(referencesComposer(text), text).toBe(true)
    }
    for (const text of ["import { x } from './solarStringComposerModel.js'", "import T from './SolarStringComposer.test'",
      '// the SolarStringComposer panel is mounted by a later slice']) {
      expect(referencesComposer(text), text).toBe(false)
    }
  })

  it('CMP1 loads Ground rows once', async () => {
    const { props } = await mount()
    expect(props.readIntake).toHaveBeenCalledExactlyOnceWith('d1', 7)
    expect(button('Select row 1 Group 1')).toBeTruthy()
    expect(button('Select row 2 Group 2')).toBeTruthy()
    expect(screen.getByText('3 slots')).toBeTruthy()
    expect(screen.getByText('2 slots')).toBeTruthy()
  })

  it('CMP2 refuses project scope without a read', async () => {
    const { props } = await mount(ground, { projectId: 'p1' })
    expect(screen.getByText(SOLAR_STRING_COMPOSER_REASONS.scope)).toBeTruthy()
    expect(props.readIntake).not.toHaveBeenCalled()
  })

  it('CMP3 refuses an unsupported tool without a read', async () => {
    const { props } = await mount(ground, { row: { name: 'solar-string-delete' } })
    expect(screen.getByText(SOLAR_STRING_COMPOSER_REASONS.request)).toBeTruthy()
    expect(screen.getAllByRole('button').map((item) => item.textContent)).toEqual(['Cancel'])
    expect(props.readIntake).not.toHaveBeenCalled()
  })

  it('CMP4 shows a refused view and reloads', async () => {
    const readIntake = vi.fn(async () => envelope(ground, 8))
    await mount(ground, { readIntake })
    expect(screen.getByText(SOLAR_STRING_COMPOSER_REASONS.stale).getAttribute('role')).toBe('status')
    click('Reload drawing')
    await waitFor(() => expect(readIntake).toHaveBeenCalledTimes(2))
  })

  it('CMP5 shows unavailable after a rejected or synchronous read', async () => {
    for (const readIntake of [vi.fn().mockRejectedValue(new Error('read failed')),
      vi.fn(() => { throw new Error('read failed') })]) {
      await mount(ground, { readIntake })
      expect(screen.getByText(SOLAR_STRING_COMPOSER_REASONS.unavailable)).toBeTruthy()
      cleanup()
    }
  })

  it('CMP6 ignores a late first binding read', async () => {
    const old = deferred(), next = deferred()
    const readIntake = vi.fn().mockReturnValueOnce(old.promise).mockReturnValueOnce(next.promise)
    const { props, rerender } = await mount(ground, { readIntake })
    expect(screen.getByText(SOLAR_STRING_COMPOSER_REASONS.loading).getAttribute('role')).toBe('status')
    rerender(<SolarStringComposer {...props} drawingVersion={8} />)
    await act(async () => {})
    await act(async () => { next.resolve(envelope(rooftop, 8)) })
    await act(async () => { old.resolve(envelope()) })
    expect(screen.getByLabelText('Search panels')).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Select row 1 Group 1' })).toBeNull()
    expect(readIntake.mock.calls).toEqual([['d1', 7], ['d1', 8]])
  })

  it('CMP6 ignores an old rejection after reloading the current binding', async () => {
    const old = deferred(), next = deferred(), reloaded = deferred()
    const readIntake = vi.fn().mockReturnValueOnce(old.promise).mockReturnValueOnce(next.promise)
      .mockReturnValueOnce(reloaded.promise)
    const { props, rerender } = await mount(ground, { readIntake })
    rerender(<SolarStringComposer {...props} drawingVersion={8} />)
    await act(async () => {})
    await act(async () => { next.resolve(envelope(ground, 7)) })
    click('Reload drawing'); await act(async () => {})
    await act(async () => { reloaded.resolve(envelope(rooftop, 8)) })
    await act(async () => { old.reject(new Error('read failed')) })
    expect(screen.getByLabelText('Search panels')).toBeTruthy()
    expect(screen.queryByText(SOLAR_STRING_COMPOSER_REASONS.unavailable)).toBeNull()
    expect(readIntake.mock.calls).toEqual([['d1', 7], ['d1', 8], ['d1', 8]])
  })

  it('CMP7 composes multi over two ranges and checks the head', async () => {
    const { props } = await mount(ground, { row: { name: STRING_MULTI_ADD_TOOL } })
    addRange(); addRange(1, '1', '2'); change('String length', '3')
    expect(screen.getByText('5 panels')).toBeTruthy()
    expect(screen.getByText('String 1: 3 panels')).toBeTruthy()
    expect(screen.getByText('String 2: 2 panels')).toBeTruthy()
    click('Review & run')
    expect(screen.getByText(SOLAR_STRING_COMPOSER_REASONS.checking)).toBeTruthy()
    expect(screen.getByLabelText('First slot').disabled).toBe(true)
    await act(async () => {})
    expect(props.readIntake).toHaveBeenLastCalledWith('d1', 'head')
    const params = expected(ground, STRING_MULTI_ADD_TOOL, [range(0, 1, 3), range(1, 1, 2)], 3)
    expect(params.operation).toBe('add-strings')
    expect(params.expected_rev).toBe(2)
    expect(params.ordered_panel_refs).toHaveLength(5)
    expect(props.onSubmit).toHaveBeenCalledExactlyOnceWith(props.row, params)
  })

  it('CMP8 preserves descending single range order', async () => {
    const { props } = await mount()
    addRange(0, '3', '1'); click('Review & run')
    await act(async () => {})
    const params = expected(ground, STRING_ADD_TOOL, [range(0, 3, 1)])
    expect(params.operation).toBe('add-string')
    expect(props.onSubmit).toHaveBeenCalledExactlyOnceWith(props.row, params)
  })

  it('CMP9 validates typed drafts and leaves range bounds to preview', async () => {
    await mount(); click('Select row 1 Group 1'); change('Last slot', '3')
    for (const value of ['1.5', '0', 'a', '']) {
      change('First slot', value)
      expect(button('Add range').disabled).toBe(true)
      expect(screen.getByLabelText('First slot').getAttribute('aria-invalid')).toBe('true')
    }
    change('First slot', '1')
    for (const value of ['1.5', '0', 'a', '']) {
      change('Last slot', value)
      expect(button('Add range').disabled).toBe(true)
      expect(screen.getByLabelText('Last slot').getAttribute('aria-invalid')).toBe('true')
      expect(screen.getByLabelText('First slot').getAttribute('aria-invalid')).toBe('false')
    }
    change('First slot', '1'); change('Last slot', '4'); click('Add range')
    expect(queueLabels()).toEqual(['Group 1 slots 1 to 4'])
    expect(screen.getByText(SOLAR_STRING_COMPOSER_REASONS.range)).toBeTruthy()
    expect(button('Review & run').disabled).toBe(true)
  })

  it('CMP9b slot drafts accept nine digits and refuse ten', async () => {
    await mount(); click('Select row 1 Group 1')
    change('First slot', '1'); change('Last slot', '999999999')
    expect(screen.getByLabelText('Last slot').getAttribute('aria-invalid')).toBe('false')
    expect(button('Add range').disabled).toBe(false)
    change('Last slot', '1000000000')
    expect(screen.getByLabelText('Last slot').getAttribute('aria-invalid')).toBe('true')
    expect(button('Add range').disabled).toBe(true)
    change('Last slot', '3'); change('First slot', '1000000000')
    expect(screen.getByLabelText('First slot').getAttribute('aria-invalid')).toBe('true')
    expect(button('Add range').disabled).toBe(true)
    change('First slot', '999999999')
    expect(screen.getByLabelText('First slot').getAttribute('aria-invalid')).toBe('false')
    expect(button('Add range').disabled).toBe(false)
  })

  it('CMP10 pages and filters Roof panels without changing queue order', async () => {
    await mount(rooftop)
    const panels = screen.getByRole('region', { name: 'Panels' })
    expect(within(panels).getAllByRole('listitem')).toHaveLength(25)
    fireEvent.click(within(panels).getByRole('button', { name: 'Next page' }))
    expect(within(panels).getAllByRole('listitem')).toHaveLength(5)
    fireEvent.click(within(panels).getByRole('button', { name: 'Previous page' }))
    click(`Add panel ${Q[2]}`); change('Search panels', 'MULTI')
    expect(within(panels).getAllByRole('button', { name: /^Add panel / }).every((item) => item.textContent.includes('Multi group'))).toBe(true)
    fireEvent.click(within(panels).getByRole('button', { name: 'Next page' }))
    expect(within(panels).getAllByRole('listitem')).toHaveLength(2)
    fireEvent.click(within(panels).getByRole('button', { name: 'Previous page' }))
    click(`Add panel ${Q[0]}`)
    expect(queueLabels()).toEqual([Q[2], Q[0]])
  })

  it('CMP11 queue edits preserve the other entries order', async () => {
    await mount(rooftop)
    for (const id of Q.slice(0, 3)) click(`Add panel ${id}`)
    click('Move selection 3 up'); expect(queueLabels()).toEqual([Q[0], Q[2], Q[1]])
    click('Move selection 1 down'); expect(queueLabels()).toEqual([Q[2], Q[0], Q[1]])
    click('Remove selection 2'); expect(queueLabels()).toEqual([Q[2], Q[1]])
  })

  it('CMP12 pages a 26 entry queue', async () => {
    await mount(rooftop)
    const add = button(`Add panel ${Q[0]}`)
    act(() => { for (let i = 0; i < 26; i += 1) fireEvent.click(add) })
    expect(queueLabels()).toHaveLength(25)
    fireEvent.click(within(queueRegion()).getByRole('button', { name: 'Next page' }))
    expect(queueLabels()).toEqual([Q[0]])
    expect(button('Remove selection 26')).toBeTruthy()
  })

  it('CMP13 stale review keeps the queue and enables another check', async () => {
    const readIntake = vi.fn().mockResolvedValueOnce(envelope()).mockResolvedValueOnce(envelope(ground, 8))
    const { props } = await mount(ground, { readIntake })
    addRange(); click('Review & run'); await act(async () => {})
    expect(screen.getByRole('alert').textContent).toBe(SOLAR_STRING_COMPOSER_REASONS.stale)
    expect(props.onSubmit).not.toHaveBeenCalled()
    expect(queueLabels()).toEqual(['Group 1 slots 1 to 3'])
    expect(button('Review & run').disabled).toBe(false)
  })

  it('CMP14 rejected head read shows unavailable as an alert', async () => {
    const readIntake = vi.fn().mockResolvedValueOnce(envelope()).mockRejectedValueOnce(new Error('read failed'))
    const { props } = await mount(ground, { readIntake })
    addRange(); click('Review & run'); await act(async () => {})
    expect(screen.getByRole('alert').textContent).toBe(SOLAR_STRING_COMPOSER_REASONS.unavailable)
    expect(props.onSubmit).not.toHaveBeenCalled()
  })

  it('CMP14 ignores head reads after rebinding or unmounting', async () => {
    const head = deferred()
    const readIntake = vi.fn().mockResolvedValueOnce(envelope()).mockReturnValueOnce(head.promise)
      .mockResolvedValueOnce(envelope())
    const { props, rerender, unmount } = await mount(ground, { readIntake })
    addRange(); click('Review & run'); await act(async () => {})
    rerender(<SolarStringComposer {...props} drawingId="d2" />)
    await act(async () => {})
    await act(async () => { head.resolve(envelope()) })
    expect(props.onSubmit).not.toHaveBeenCalled()
    expect(queueLabels()).toEqual([])
    const nextHead = deferred()
    readIntake.mockReturnValueOnce(nextHead.promise)
    addRange(); click('Review & run'); await act(async () => {})
    unmount()
    await act(async () => { nextHead.resolve(envelope()) })
    expect(props.onSubmit).not.toHaveBeenCalled()
  })

  it('CMP15 same tick double click performs one check and submission', async () => {
    const head = deferred()
    const readIntake = vi.fn().mockResolvedValueOnce(envelope()).mockReturnValueOnce(head.promise)
    const { props } = await mount(ground, { readIntake })
    addRange()
    const run = button('Review & run')
    act(() => { fireEvent.click(run); fireEvent.click(run) })
    await act(async () => {})
    expect(readIntake).toHaveBeenCalledTimes(2)
    await act(async () => { head.resolve(envelope()) })
    expect(props.onSubmit).toHaveBeenCalledTimes(1)
  })

  it('CMP16 pending disables editing and failed Retry uses the checked path', async () => {
    const { props, rerender } = await mount()
    addRange(); rerender(<SolarStringComposer {...props} status="pending" />)
    expect(screen.getByText(SOLAR_STRING_COMPOSER_REASONS.pending).getAttribute('role')).toBe('status')
    for (const name of ['Add range', 'Remove selection 1', 'Move selection 1 up', 'Move selection 1 down', 'Review & run']) {
      expect(button(name).disabled).toBe(true)
    }
    for (const label of ['First slot', 'Last slot', 'Search rows']) {
      expect(screen.getByLabelText(label).disabled, label).toBe(true)
    }
    rerender(<SolarStringComposer {...props} status="failed" failureCode="STRING_TOO_LONG" />)
    expect(screen.getByRole('alert').textContent).toBe(SOLAR_STRING_COMPOSER_REASONS.tooLong)
    expect(queueLabels()).toEqual(['Group 1 slots 1 to 3'])
    expect(screen.getByLabelText('Last slot').value).toBe('3')
    click('Retry'); await act(async () => {})
    expect(props.readIntake).toHaveBeenLastCalledWith('d1', 'head')
    expect(props.onSubmit).toHaveBeenCalledExactlyOnceWith(props.row, expected(ground, STRING_ADD_TOOL, [range(0, 1, 3)]))
    rerender(<SolarStringComposer {...props} status="failed" failureCode="SOMETHING_ELSE" />)
    expect(screen.getByRole('alert').textContent).toBe(SOLAR_STRING_COMPOSER_REASONS.failed)
  })

  it('CMP16b pending disables moving a two entry queue', async () => {
    const { props, rerender } = await mount(rooftop)
    click(`Add panel ${Q[0]}`); click(`Add panel ${Q[1]}`)
    expect(button('Move selection 1 down').disabled).toBe(false)
    expect(button('Move selection 2 up').disabled).toBe(false)
    rerender(<SolarStringComposer {...props} status="pending" />)
    for (const name of ['Move selection 1 down', 'Move selection 2 up', 'Remove selection 2']) {
      expect(button(name).disabled, name).toBe(true)
    }
    fireEvent.click(button('Move selection 1 down'))
    expect(queueLabels()).toEqual([Q[0], Q[1]])
  })

  it('CMP16c pending disables String length on the multi tool', async () => {
    const { props, rerender } = await mount(ground, { row: { name: STRING_MULTI_ADD_TOOL } })
    addRange(); change('String length', '3')
    expect(screen.getByLabelText('String length').disabled).toBe(false)
    rerender(<SolarStringComposer {...props} status="pending" />)
    expect(screen.getByLabelText('String length').disabled).toBe(true)
    expect(screen.getByLabelText('String length').value).toBe('3')
  })

  it('CMP17 finished clears the queue retains length and reloads once', async () => {
    const { props, rerender } = await mount(ground, { row: { name: STRING_MULTI_ADD_TOOL } })
    addRange(); change('String length', '3')
    rerender(<SolarStringComposer {...props} status="pending" />)
    rerender(<SolarStringComposer {...props} status="finished" />)
    await act(async () => {})
    expect(queueLabels()).toEqual([])
    expect(screen.getByLabelText('String length').value).toBe('3')
    expect(screen.getByText(SOLAR_STRING_COMPOSER_REASONS.finished).getAttribute('role')).toBe('status')
    expect(props.readIntake.mock.calls).toEqual([['d1', 7], ['d1', 7]])
    rerender(<SolarStringComposer {...props} status="finished" />)
    await act(async () => {})
    expect(props.readIntake).toHaveBeenCalledTimes(2)
  })

  it('CMP18 defaults length to saved limit until typed', async () => {
    await mount(rooftop, { row: { name: STRING_MULTI_ADD_TOOL } })
    expect(screen.getByLabelText('String length').value).toBe('14')
    change('String length', '1.5')
    expect(screen.getByText(SOLAR_STRING_COMPOSER_REASONS.length)).toBeTruthy()
    expect(screen.getByLabelText('String length').getAttribute('aria-invalid')).toBe('true')
  })

  it('CMP19 Cancel calls close', async () => {
    const onClose = vi.fn()
    await mount(ground, { onClose }); click('Cancel')
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('CMP20 large Ground range renders a bounded list without preview ids', async () => {
    const graph = structuredClone(ground)
    graph.frames = [{ ...graph.frames[0], ground_slots: block(5000) }]
    graph.settings.panels_in_sequence = 4096
    const { container } = await mount(graph)
    addRange(0, '1', '4096')
    expect(screen.getByText('String 1: 4096 panels')).toBeTruthy()
    expect(container.querySelectorAll('li').length).toBeLessThan(100)
    expect(container.textContent).not.toContain('leaf:panel:')
    expect(button('Review & run').disabled).toBe(false)
  })

  it('CMP21 decodes once per accepted load and never in compose or freshness', async () => {
    const { props, rerender } = await mount()
    expect(decodeGroundFrameSlots).toHaveBeenCalledTimes(1)
    addRange(0, '1', '1'); addRange(0, '2', '2')
    click('Move selection 2 up'); click('Remove selection 1'); change('First slot', '3')
    click('Review & run'); await act(async () => {})
    expect(props.onSubmit).toHaveBeenCalledTimes(1)
    expect(decodeGroundFrameSlots).toHaveBeenCalledTimes(1)
    rerender(<SolarStringComposer {...props} status="finished" />)
    await act(async () => {})
    expect(decodeGroundFrameSlots).toHaveBeenCalledTimes(2)
  })

  it('CMP22 drawing change clears queue drafts search pages and selection', async () => {
    const { props, rerender } = await mount(ground, { row: { name: STRING_MULTI_ADD_TOOL } })
    addRange(); change('String length', '3'); change('Search rows', 'Group 1')
    rerender(<SolarStringComposer {...props} drawingId="d2" />)
    await act(async () => {})
    expect(props.readIntake).toHaveBeenLastCalledWith('d2', 7)
    expect(queueLabels()).toEqual([])
    expect(screen.getByLabelText('Search rows').value).toBe('')
    expect(screen.getByLabelText('String length').value).toBe('27')
    expect(screen.queryByLabelText('First slot')).toBeNull()
    click('Select row 1 Group 1')
    expect(screen.getByLabelText('First slot').value).toBe('')
    expect(screen.getByLabelText('Last slot').value).toBe('')
  })

  it('CMP23 never renders server text', async () => {
    const { container } = await mount(ground, { status: 'failed', failureCode: 'leak<b>secret</b>' })
    expect(container.textContent).not.toContain('secret')
    expect(screen.getByRole('alert').textContent).toBe(SOLAR_STRING_COMPOSER_REASONS.failed)
  })

  it('CMP24 bounds multi queue additions at 900 entries', async () => {
    await mount(rooftop, { row: { name: STRING_MULTI_ADD_TOOL } })
    const add = button(`Add panel ${Q[0]}`)
    // 901 clicks in one batch: every click reads the stale enabled button, so only the updater's own bound caps it.
    act(() => { for (let i = 0; i < 901; i += 1) fireEvent.click(add) })
    expect(within(queueRegion()).getByText('900 selections')).toBeTruthy()
    expect(screen.getAllByRole('button', { name: /^Add panel / }).every((item) => item.disabled)).toBe(true)
    expect(screen.getByText(SOLAR_STRING_COMPOSER_REASONS.duplicate)).toBeTruthy()
    fireEvent.click(add)
    expect(within(queueRegion()).getByText('900 selections')).toBeTruthy()
    expect(queueLabels()).toHaveLength(25)
  })

  it('CMP20b a 27 string preview pages 25 then 2', async () => {
    const graph = structuredClone(ground)
    graph.frames = [{ ...graph.frames[0], ground_slots: block(5000) }]
    graph.settings.panels_in_sequence = 4096
    await mount(graph, { row: { name: STRING_MULTI_ADD_TOOL } })
    change('String length', '1'); addRange(0, '1', '27')
    const strings = () => within(screen.getByRole('region', { name: 'Composed strings' }))
    expect(strings().getByText('27 strings')).toBeTruthy()
    const items = () => strings().getAllByRole('listitem').map((item) => item.textContent)
    expect(items()).toHaveLength(25)
    expect(items()[0]).toBe('String 1: 1 panels')
    expect(items()[24]).toBe('String 25: 1 panels')
    fireEvent.click(strings().getByRole('button', { name: 'Next page' }))
    expect(items()).toEqual(['String 26: 1 panels', 'String 27: 1 panels'])
  })

  it('CMP22b drawing change returns every pager to its first page', async () => {
    const { props, rerender } = await mount(rooftop, { row: { name: STRING_MULTI_ADD_TOOL } })
    const panels = () => within(screen.getByRole('region', { name: 'Panels' }))
    const strings = () => within(screen.getByRole('region', { name: 'Composed strings' }))
    const fill = () => {
      change('String length', '1')
      // Q holds the 27 unassigned panels; 22 sit on the first source page and 5 on the second.
      for (const id of Q.slice(0, 22)) click(`Add panel ${id}`)
      fireEvent.click(panels().getByRole('button', { name: 'Next page' }))
      for (const id of Q.slice(22, 26)) click(`Add panel ${id}`)
    }
    fill()
    fireEvent.click(within(queueRegion()).getByRole('button', { name: 'Next page' }))
    fireEvent.click(strings().getByRole('button', { name: 'Next page' }))
    expect(queueLabels()).toEqual([Q[25]])
    expect(strings().getAllByRole('listitem').map((item) => item.textContent)).toEqual(['String 26: 1 panels'])
    expect(screen.queryByRole('button', { name: `Add panel ${rooftop.panels[0].id}` })).toBeNull()
    rerender(<SolarStringComposer {...props} drawingId="d2" />)
    await act(async () => {})
    expect(props.readIntake).toHaveBeenLastCalledWith('d2', 7)
    expect(button(`Add panel ${rooftop.panels[0].id}`)).toBeTruthy()
    fill()
    expect(queueLabels()[0]).toBe(Q[0])
    expect(strings().getAllByRole('listitem')[0].textContent).toBe('String 1: 1 panels')
  })

  it('CMP25 a scope change before the first read runs starts no read', async () => {
    const before = vi.fn(async () => envelope())
    const after = vi.fn(async () => envelope())
    const { rerender } = render(<SolarStringComposer {...props7({ readIntake: before })} />)
    rerender(<SolarStringComposer {...props7({ readIntake: after, projectId: 'p1' })} />)
    await act(async () => {})
    expect(before).not.toHaveBeenCalled()
    expect(after).not.toHaveBeenCalled()
  })

  it('CMP25b an unsupported tool before the first read runs starts no read', async () => {
    const before = vi.fn(async () => envelope())
    const after = vi.fn(async () => envelope())
    const { rerender } = render(<SolarStringComposer {...props7({ readIntake: before })} />)
    rerender(<SolarStringComposer {...props7({ readIntake: after, row: { name: 'solar-string-delete' } })} />)
    await act(async () => {})
    expect(before).not.toHaveBeenCalled()
    expect(after).not.toHaveBeenCalled()
    expect(screen.getByRole('status').textContent).toBe(SOLAR_STRING_COMPOSER_REASONS.request)
  })

  it('CMP26 unmounting right after Review & run starts no head read', async () => {
    const { props, unmount } = await mount()
    addRange()
    props.readIntake.mockClear()
    click('Review & run'); unmount()
    await act(async () => {})
    expect(props.readIntake).not.toHaveBeenCalled()
    expect(props.onSubmit).not.toHaveBeenCalled()
  })

  it('CMP27 StrictMode reads the first binding once', async () => {
    const readIntake = vi.fn(async () => envelope())
    render(<React.StrictMode><SolarStringComposer {...props7({ readIntake })} /></React.StrictMode>)
    await act(async () => {})
    expect(readIntake).toHaveBeenCalledExactlyOnceWith('d1', 7)
    expect(button('Select row 1 Group 1')).toBeTruthy()
  })
})
