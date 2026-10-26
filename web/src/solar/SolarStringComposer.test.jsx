import React from 'react'
import fs from 'node:fs'
import path from 'node:path'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import SolarStringComposer from './SolarStringComposer.jsx'
import SolarStepEditor from './SolarStepEditor.jsx'
import { createSolarProjectClient } from './solarProjectClient.js'
import ground from './__fixtures__/stringComposerGround.json'
import rooftop from './__fixtures__/stringComposerRooftop.json'
import { decodeGroundFrameSlots, GROUND_SLOT_CODEC } from './solarGroundSlots.js'
import { SOLAR_STRING_COMPOSER_REASONS, PROJECT_STRING_COMPOSER_REASONS, STRING_ADD_TOOL, STRING_MULTI_ADD_TOOL,
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

  it('CMP2 incomplete project props refuse without a read', async () => {
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
    expect(readIntake.mock.calls).toEqual([['d1', 7], ['d1', 8], ['d1', 'head']])
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

  it('CMP13 stale review keeps the queue visible and requires reloading before another check', async () => {
    const readIntake = vi.fn().mockResolvedValueOnce(envelope()).mockResolvedValueOnce(envelope(ground, 8))
    const { props } = await mount(ground, { readIntake })
    addRange(); click('Review & run'); await act(async () => {})
    expect(screen.getByRole('alert').textContent).toBe(SOLAR_STRING_COMPOSER_REASONS.stale)
    expect(props.onSubmit).not.toHaveBeenCalled()
    expect(queueLabels()).toEqual(['Group 1 slots 1 to 3'])
    expect(button('Review & run').disabled).toBe(true)
    expect(button('Reload drawing').disabled).toBe(false)
  })

  it('CMP14 rejected head read shows unavailable as an alert', async () => {
    const readIntake = vi.fn().mockResolvedValueOnce(envelope()).mockRejectedValueOnce(new Error('read failed'))
    const { props } = await mount(ground, { readIntake })
    addRange(); click('Review & run'); await act(async () => {})
    expect(screen.getByRole('alert').textContent).toBe(SOLAR_STRING_COMPOSER_REASONS.unavailable)
    expect(props.onSubmit).not.toHaveBeenCalled()
  })

  it('blocks a reloaded head until the step editor submits with the matching drawing version', async () => {
    const readIntake = vi.fn().mockResolvedValueOnce(envelope()).mockResolvedValue(envelope(ground, 8))
    const submit = vi.fn()
    const props = { row: { name: STRING_ADD_TOOL }, drawingId: 'd1', readIntake, onClose: vi.fn() }
    const host = (version) => <SolarStepEditor {...props} drawingVersion={version}
      onSubmit={(row, params) => submit(version, row, params)} />
    const { rerender } = render(host(7))
    await screen.findByRole('button', { name: 'Select row 1 Group 1' })
    addRange(); click('Review & run')
    await screen.findByRole('alert')
    click('Reload drawing')
    await screen.findByRole('button', { name: 'Select row 1 Group 1' })
    expect(readIntake).toHaveBeenLastCalledWith('d1', 'head')
    expect(queueLabels()).toEqual([])
    addRange()
    expect(button('Review & run').disabled).toBe(true)
    click('Review & run')
    await act(async () => {})
    expect(submit).not.toHaveBeenCalled()
    expect(screen.getByText('The drawing changed. Reopen this tool from the current drawing to continue.')).toBeTruthy()

    rerender(host(8))
    await waitFor(() => expect(readIntake).toHaveBeenLastCalledWith('d1', 8))
    await screen.findByRole('button', { name: 'Select row 1 Group 1' })
    expect(queueLabels()).toEqual([])
    addRange()
    expect(button('Review & run').disabled).toBe(false)
    click('Review & run')
    await waitFor(() => expect(submit).toHaveBeenCalledExactlyOnceWith(8, props.row,
      expected(ground, STRING_ADD_TOOL, [range(0, 1, 3)])))
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
    // Its own budget: 571 ms on an idle host; the 5 s default timed out twice under CI shard load.
  }, 30_000)

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

const canonicalBinding = Object.freeze({ kind: 'project',
  organization_id: '00000000-0000-4000-8000-000000000001',
  project_id: '00000000-0000-4000-8000-000000000002',
  drawing_id: '00000000-0000-4000-8000-000000000003',
  input_version_id: '00000000-0000-4000-8000-000000000004' })
const nextCanonicalBinding = Object.freeze({ ...canonicalBinding,
  input_version_id: '00000000-0000-4000-8000-000000000005' })
const canonicalDigest = 'sha256:' + 'a'.repeat(64)
const canonicalContext = (binding = canonicalBinding, graph = ground) => ({
  context: { ...binding, head_version_id: binding.input_version_id, is_head: true, intake_sha256: 'b'.repeat(64) },
  intake: { solar_design_graph: structuredClone(graph), solar_design_graph_sha256: 'c'.repeat(64) },
})
const acceptedCatalog = (intent) => ({ ok: true, status: 200, data: { families: [{ capabilities: [
  { name: intent.tool, catalog_digest: intent.catalogDigest, availability: {
    admission_checked: true, admission: { status_code: 200, error: null }, entitled: true,
    engine_ready: true, implemented: true, input_ready: true, runnable: true, refusal_reasons: [] } },
] }] } })
const submittedOutcome = () => ({ ok: true, status: 202, data: {
  status: 'submitted', job_id: '00000000-0000-4000-8000-000000000009' } })
const projectHarness = (initial = canonicalBinding) => {
  let binding = initial, authority = 'session', generation = 0, lease = 0
  const listeners = new Set()
  const builder = createSolarProjectClient({ fetchImpl: vi.fn() })
  builder.setScope(binding, authority)
  const notify = () => { for (const listener of listeners) listener() }
  const unsubscribe = vi.fn()
  const client = {
    getSnapshot: () => ({ binding, authorityKey: authority, leaseRevision: lease }),
    subscribe: vi.fn((listener) => { listeners.add(listener); return () => { listeners.delete(listener); unsubscribe() } }),
    captureCatalogScope: () => {
      const g = generation, l = lease
      return () => g === generation && l === lease
    },
    prepareRun: vi.fn((request) => builder.prepareRun(request)),
    readContext: vi.fn(async () => ({ ok: true, status: 200, data: canonicalContext(binding) })),
    catalog: vi.fn(async ({ intents }) => acceptedCatalog(intents[0])),
    setScope: vi.fn(), dispose: vi.fn(), acquireCheckout: vi.fn(), releaseCheckout: vi.fn(),
  }
  return { client, unsubscribe,
    move(next, key = authority) { binding = next; authority = key; generation += 1; builder.setScope(next, key); notify() },
    rotate() { lease += 1; notify() },
    props(overrides = {}) {
      let keys = 0
      return { row: { name: STRING_ADD_TOOL }, solarBinding: initial, authorityKey: 'session',
        projectClient: client, catalogDigest: canonicalDigest, newProjectIntentKey: vi.fn(() => 'strings-' + (++keys)),
        onProjectSubmit: vi.fn(async () => submittedOutcome()), readIntake: vi.fn(), onSubmit: vi.fn(), ...overrides }
    },
  }
}
async function mountProject(overrides = {}, harness = projectHarness()) {
  const props = harness.props(overrides)
  const result = render(<SolarStringComposer {...props} />)
  await act(async () => {})
  return { ...result, props, harness }
}
const flushProject = async () => { await act(async () => {}) }
const readyProject = async () => { await waitFor(() => expect(button('Review & run').disabled).toBe(false)) }
const prepareSingle = async () => { addRange(); await readyProject() }

describe('canonical SolarStringComposer', () => {
  it('R10-11 Multi exact preview and unchanged reread hand off the identical intent once', async () => {
    const { props, harness } = await mountProject({ row: { name: STRING_MULTI_ADD_TOOL } })
    addRange(); addRange(1, '1', '2'); change('String length', '3')
    await readyProject()
    const intent = harness.client.catalog.mock.calls.at(-1)[0].intents[0]
    expect(intent.params).toEqual({ drawing_id: canonicalBinding.drawing_id,
      operation: 'add-strings', expected_rev: 2, string_length: 3,
      ordered_panel_refs: composeStringRequest({ view: stringComposerView({ envelope: envelope(),
        drawingId: 'd1', drawingVersion: 7, projectId: null }), toolName: STRING_MULTI_ADD_TOOL,
      queue: [range(0, 1, 3), range(1, 1, 2)], stringLength: 3 }).params.ordered_panel_refs })
    click('Review & run'); await flushProject()
    expect(props.onProjectSubmit).toHaveBeenCalledTimes(1)
    expect(props.onProjectSubmit.mock.calls[0][0]).toBe(intent)
    const guard = props.onProjectSubmit.mock.calls[0][1]
    expect(guard.binding).toEqual(canonicalBinding)
    expect(guard.authorityKey).toBe('session')
    expect(guard.signal).toBeInstanceOf(AbortSignal)
    expect(typeof guard.isCurrent).toBe('function')
    expect(harness.client.readContext).toHaveBeenCalledTimes(2)
    expect(screen.getByText(PROJECT_STRING_COMPOSER_REASONS.submitted)).toBeTruthy()
  })
  it('R10-12 Single exact preview uses the single tool and guarded canonical handoff', async () => {
    const { props, harness } = await mountProject()
    await prepareSingle()
    const intent = harness.client.catalog.mock.calls[0][0].intents[0]
    expect(intent.tool).toBe(STRING_ADD_TOOL)
    expect(intent.params.operation).toBe('add-string')
    expect(intent.params).not.toHaveProperty('string_length')
    click('Review & run'); await flushProject()
    expect(props.onProjectSubmit.mock.calls[0][0]).toBe(intent)
    expect(props.readIntake).not.toHaveBeenCalled()
    expect(props.onSubmit).not.toHaveBeenCalled()
  })
  it('R10-19 Nested semantic and reasonless preview refusals retain fixed presentation', async () => {
    for (const [code, sentence] of [['STRING_TOO_LONG', PROJECT_STRING_COMPOSER_REASONS.tooLong],
      [null, PROJECT_STRING_COMPOSER_REASONS.preview_refused]]) {
      const harness = projectHarness()
      let evidence
      harness.client.catalog.mockImplementation(async ({ intents }) => {
        evidence = acceptedCatalog(intents[0])
        const row = evidence.data.families[0].capabilities[0]
        row.availability.admission = { status_code: 409, error: { message: 'raw secret text',
          ...(code ? { reason_code: code } : {}) } }
        row.availability.runnable = false
        return evidence
      })
      const { props, container } = await mountProject({}, harness)
      addRange(); await flushProject()
      expect(screen.getByRole('alert').textContent).toBe(sentence)
      expect(container.textContent).not.toContain('raw secret text')
      expect(evidence.data.families[0].capabilities[0].availability.admission.error.message).toBe('raw secret text')
      expect(button('Review & run').disabled).toBe(true)
      expect(props.onProjectSubmit).not.toHaveBeenCalled()
      cleanup()
    }
  })
  it('R10-20 Changed canonical version head revision or digest blocks handoff without retargeting', async () => {
    for (const [edit, sentence] of [
      [(d) => { d.context.input_version_id = nextCanonicalBinding.input_version_id }, 'context_unreadable'],
      [(d) => { d.context.head_version_id = nextCanonicalBinding.input_version_id; d.context.is_head = false }, 'not_current_head'],
      [(d) => { d.intake.solar_design_graph.rev += 1 }, 'scope_stale'],
      [(d) => { d.context.intake_sha256 = 'd'.repeat(64) }, 'scope_stale'],
      [(d) => { d.intake.solar_design_graph_sha256 = 'e'.repeat(64) }, 'scope_stale'],
    ]) {
      const { props, harness } = await mountProject()
      await prepareSingle()
      const intent = harness.client.catalog.mock.calls[0][0].intents[0]
      const data = canonicalContext(); edit(data)
      harness.client.readContext.mockResolvedValueOnce({ ok: true, status: 200, data })
      click('Review & run'); await flushProject()
      expect(screen.getByRole('alert').textContent).toBe(PROJECT_STRING_COMPOSER_REASONS[sentence])
      expect(props.onProjectSubmit).not.toHaveBeenCalled()
      expect(intent.params.expected_rev).toBe(2)
      expect(intent.binding.input_version_id).toBe(canonicalBinding.input_version_id)
      expect(queueLabels()).toEqual(['Group 1 slots 1 to 3'])
      expect(button('Review & run').disabled).toBe(true)
      cleanup()
    }
  })
  it('R10-21 Scope client and authority replacement discard initial success and failure', async () => {
    for (const changeKind of ['scope', 'client', 'authority']) {
      for (const failure of [false, true]) {
        const oldRead = deferred()
        const harness = projectHarness()
        harness.client.readContext.mockReturnValueOnce(oldRead.promise)
        const { props, rerender } = await mountProject({}, harness)
        const next = changeKind === 'client' ? projectHarness() : harness
        if (changeKind !== 'client') act(() => harness.move(changeKind === 'scope' ? nextCanonicalBinding : canonicalBinding,
          changeKind === 'authority' ? 'next-session' : 'session'))
        const nextProps = { ...props, projectClient: next.client,
          solarBinding: changeKind === 'scope' ? nextCanonicalBinding : canonicalBinding,
          authorityKey: changeKind === 'authority' ? 'next-session' : 'session' }
        rerender(<SolarStringComposer {...nextProps} />)
        await flushProject()
        expect(button('Select row 1 Group 1')).toBeTruthy()
        await act(async () => { if (failure) oldRead.reject(new Error('obsolete secret')); else oldRead.resolve({
          ok: true, status: 200, data: canonicalContext(canonicalBinding, rooftop) }) })
        expect(button('Select row 1 Group 1')).toBeTruthy()
        expect(screen.queryByText(PROJECT_STRING_COMPOSER_REASONS.context_unreadable)).toBeNull()
        expect(harness.unsubscribe).toHaveBeenCalledTimes(1)
        expect(harness.client.dispose).not.toHaveBeenCalled()
        cleanup()
      }
    }
  })
  it('R10-22 Replacement during preview reread or callback discards every obsolete settlement', async () => {
    for (const stage of ['preview', 'reread', 'callback']) {
      for (const changeKind of ['scope', 'client', 'authority']) {
        for (const failure of [false, true]) {
        const pending = deferred()
        const harness = projectHarness()
        const overrides = stage === 'callback' ? { onProjectSubmit: vi.fn(() => pending.promise) } : {}
        if (stage === 'preview') harness.client.catalog.mockReturnValueOnce(pending.promise)
        const { props, rerender } = await mountProject(overrides, harness)
        addRange()
        if (stage === 'preview') await flushProject()
        else {
          await readyProject()
          if (stage === 'reread') harness.client.readContext.mockReturnValueOnce(pending.promise)
          click('Review & run'); await flushProject()
        }
        const next = changeKind === 'client' ? projectHarness() : harness
        if (changeKind !== 'client') act(() => harness.move(changeKind === 'scope' ? nextCanonicalBinding : canonicalBinding,
          changeKind === 'authority' ? 'new-session' : 'session'))
        const nextProps = { ...props, projectClient: next.client,
          solarBinding: changeKind === 'scope' ? nextCanonicalBinding : canonicalBinding,
          authorityKey: changeKind === 'authority' ? 'new-session' : 'session' }
        rerender(<SolarStringComposer {...nextProps} />); await flushProject()
        const intent = harness.client.catalog.mock.calls[0][0].intents[0]
        await act(async () => {
          if (failure) pending.reject(new Error('obsolete settlement failure'))
          else pending.resolve(stage === 'preview' ? acceptedCatalog(intent)
            : stage === 'reread' ? { ok: true, status: 200, data: canonicalContext() } : submittedOutcome())
        })
        expect(queueLabels()).toEqual([])
        expect(button('Review & run').disabled).toBe(true)
        expect(screen.queryByText(PROJECT_STRING_COMPOSER_REASONS.submitted)).toBeNull()
        expect(props.onProjectSubmit).toHaveBeenCalledTimes(stage === 'callback' ? 1 : 0)
        if (stage === 'callback') expect(props.onProjectSubmit.mock.calls[0][1].isCurrent()).toBe(false)
        cleanup()
        }
      }
    }
  })
  it('R10-23 A to B to A movement invalidates an old preview predicate', async () => {
    const old = deferred(), current = deferred()
    const harness = projectHarness()
    harness.client.catalog.mockReturnValueOnce(old.promise).mockReturnValueOnce(current.promise)
    const { props } = await mountProject({}, harness)
    addRange(); await flushProject()
    const intent = harness.client.catalog.mock.calls[0][0].intents[0]
    act(() => { harness.move(nextCanonicalBinding); harness.move(canonicalBinding) })
    await flushProject()
    expect(harness.client.catalog).toHaveBeenCalledTimes(2)
    await act(async () => { old.resolve(acceptedCatalog(intent)) })
    expect(button('Review & run').disabled).toBe(true)
    await act(async () => { current.resolve(acceptedCatalog(intent)) })
    await readyProject()
    expect(props.newProjectIntentKey).toHaveBeenCalledTimes(1)
    expect(harness.client.catalog.mock.calls[1][0].intents[0]).toBe(intent)
  })
  it('R10-24 Checkout rotation restarts interrupted loading and refreshes the same intent key', async () => {
    const pending = deferred()
    const harness = projectHarness()
    harness.client.readContext.mockReturnValueOnce(pending.promise)
    const { props } = await mountProject({}, harness)
    const initialSignal = harness.client.readContext.mock.calls[0][0].signal
    act(() => harness.rotate()); await flushProject()
    expect(initialSignal.aborted).toBe(true)
    expect(harness.client.readContext).toHaveBeenCalledTimes(2)
    expect(button('Select row 1 Group 1')).toBeTruthy()
    await act(async () => { pending.reject(new Error('rotated read')) })
    await prepareSingle()
    const intent = harness.client.catalog.mock.calls[0][0].intents[0]
    const renewed = deferred()
    harness.client.catalog.mockReturnValueOnce(renewed.promise)
    act(() => harness.rotate())
    expect(button('Review & run').disabled).toBe(true)
    await flushProject()
    expect(harness.client.catalog.mock.calls.at(-1)[0].intents[0]).toBe(intent)
    expect(props.newProjectIntentKey).toHaveBeenCalledTimes(1)
    await act(async () => { renewed.resolve(acceptedCatalog(intent)) })
    await readyProject()
    for (const method of ['setScope', 'dispose', 'acquireCheckout', 'releaseCheckout']) expect(harness.client[method]).not.toHaveBeenCalled()
  })
  it('R10-25 Ordered inputs tool and digest disarm immediately while search and callbacks preserve keys', async () => {
    const { props, harness, rerender } = await mountProject({ row: { name: STRING_MULTI_ADD_TOOL } })
    addRange(); addRange(1, '1', '2'); change('String length', '3'); await readyProject()
    const firstIntent = harness.client.catalog.mock.calls.at(-1)[0].intents[0]
    change('Search rows', 'Group')
    let nextKeys = 0
    const nextFactory = vi.fn(() => 'new-factory-key-' + (++nextKeys))
    const nextProps = { ...props, newProjectIntentKey: nextFactory, onProjectSubmit: vi.fn(async () => submittedOutcome()) }
    rerender(<SolarStringComposer {...nextProps} />); await flushProject()
    expect(harness.client.catalog.mock.calls.at(-1)[0].intents[0]).toBe(firstIntent)
    expect(nextFactory).not.toHaveBeenCalled()
    const armedRun = button('Review & run')
    act(() => { fireEvent.click(button('Move selection 2 up')); fireEvent.click(armedRun) })
    expect(button('Review & run').disabled).toBe(true)
    expect(nextProps.onProjectSubmit).not.toHaveBeenCalled()
    await readyProject()
    const moved = harness.client.catalog.mock.calls.at(-1)[0].intents[0]
    expect(moved).not.toBe(firstIntent)
    expect(moved.idempotencyKey).not.toBe(firstIntent.idempotencyKey)
    expect(nextFactory).toHaveBeenCalledTimes(1)
    const singleProps = { ...nextProps, row: { name: STRING_ADD_TOOL } }
    rerender(<SolarStringComposer {...singleProps} />)
    expect(button('Review & run').disabled).toBe(true)
    await readyProject()
    expect(harness.client.catalog.mock.calls.at(-1)[0].intents[0].tool).toBe(STRING_ADD_TOOL)
    const digestProps = { ...singleProps, catalogDigest: 'sha256:' + 'd'.repeat(64) }
    rerender(<SolarStringComposer {...digestProps} />)
    expect(button('Review & run').disabled).toBe(true)
    await readyProject()
    expect(harness.client.catalog.mock.calls.at(-1)[0].intents[0].catalogDigest).toBe(digestProps.catalogDigest)
    expect(nextFactory).toHaveBeenCalledTimes(3)
    click('Refresh preview'); await readyProject()
    expect(nextFactory).toHaveBeenCalledTimes(3)
  })
  it('R10-26 Same tick activation and unrelated renders keep one lock through callback settlement', async () => {
    const pending = deferred()
    const { props, harness, rerender } = await mountProject({ onProjectSubmit: vi.fn(() => pending.promise) })
    await prepareSingle()
    const run = button('Review & run')
    act(() => { fireEvent.click(run); fireEvent.click(run) })
    await flushProject()
    expect(harness.client.readContext).toHaveBeenCalledTimes(2)
    expect(props.onProjectSubmit).toHaveBeenCalledTimes(1)
    rerender(<SolarStringComposer {...props} status="finished" failureCode="STRING_TOO_LONG" />)
    expect(button('Review & run').disabled).toBe(true)
    expect(screen.getByLabelText('First slot').disabled).toBe(true)
    fireEvent.click(run); await flushProject()
    expect(props.onProjectSubmit).toHaveBeenCalledTimes(1)
    await act(async () => { pending.resolve(submittedOutcome()) })
    expect(screen.getByText(PROJECT_STRING_COMPOSER_REASONS.submitted)).toBeTruthy()
    expect(button('Review & run').disabled).toBe(true)
  })
  it('R10-27 Real R7 transports send D V T and K in their canonical locations', async () => {
    const calls = []
    const fetchImpl = vi.fn(async (url, options) => {
      calls.push({ url, options })
      const data = url.includes('/context?') ? canonicalContext() : url.startsWith('/api/capabilities?')
        ? acceptedCatalog({ tool: STRING_ADD_TOOL, catalogDigest: canonicalDigest }).data : submittedOutcome().data
      return { ok: true, status: url === '/api/run' ? 202 : 200, json: async () => data }
    })
    const client = createSolarProjectClient({ fetchImpl, authHeaders: () => ({ Authorization: 'tok' }) })
    client.setScope(canonicalBinding, 'session')
    const harness = projectHarness()
    const props = harness.props({ projectClient: client, onProjectSubmit: vi.fn((intent, options) => client.submitRun(intent, options)) })
    const mounted = render(<SolarStringComposer {...props} />)
    await flushProject(); await prepareSingle()
    click('Review & run'); await flushProject()
    expect(screen.getByText(PROJECT_STRING_COMPOSER_REASONS.submitted)).toBeTruthy()
    const contextCalls = calls.filter(({ url }) => url.includes('/context?'))
    expect(contextCalls).toHaveLength(2)
    expect(contextCalls[0].url).toBe('/api/projects/' + canonicalBinding.project_id + '/drawing-versions/' +
      canonicalBinding.input_version_id + '/context?drawing_id=' + canonicalBinding.drawing_id)
    const catalog = calls.find(({ url }) => url.startsWith('/api/capabilities?'))
    expect(catalog.options.method).toBe('POST')
    expect(new URLSearchParams(catalog.url.split('?')[1]).has('project_runs')).toBe(false)
    const entries = JSON.parse(catalog.options.body).project_runs
    const intent = props.onProjectSubmit.mock.calls[0][0]
    expect(entries).toEqual({ [STRING_ADD_TOOL]: { params: intent.params,
      catalog_digest: canonicalDigest, idempotency_key: intent.idempotencyKey } })
    const run = calls.find(({ url }) => url === '/api/run')
    expect(JSON.parse(run.options.body)).toEqual({ tool: STRING_ADD_TOOL, params: intent.params,
      dwg: canonicalBinding.input_version_id, catalog_digest: canonicalDigest })
    expect(run.options.headers['Idempotency-Key']).toBe(intent.idempotencyKey)
    expect(run.options.headers['X-Org-Id']).toBe(canonicalBinding.organization_id)
    expect(run.options.headers['X-Project-Id']).toBe(canonicalBinding.project_id)
    expect(run.options.headers['X-Tenant-Id']).toBe(canonicalBinding.organization_id)
    expect(run.options.headers['Content-Type']).toBe('application/json')
    for (const call of calls) {
      expect(call.url).not.toContain('dwg_version')
      expect(call.options.body ?? '').not.toContain('expected_drawing_head')
      expect(call.options.body ?? '').not.toContain('solar_design_graph')
      expect(call.options.body ?? '').not.toContain('_creation')
    }
    mounted.unmount(); client.dispose()
  })
  it('R10-28 Valid submitted feedback retains the queue and prevents repeat handoff', async () => {
    const { props } = await mountProject({ status: 'finished', failureCode: 'STRING_TOO_LONG' })
    await prepareSingle()
    expect(screen.queryByText(SOLAR_STRING_COMPOSER_REASONS.finished)).toBeNull()
    click('Review & run'); await flushProject()
    expect(screen.getByText(PROJECT_STRING_COMPOSER_REASONS.submitted).getAttribute('role')).toBe('status')
    expect(queueLabels()).toEqual(['Group 1 slots 1 to 3'])
    expect(screen.getByLabelText('Last slot').value).toBe('3')
    click('Review & run'); await flushProject()
    expect(props.onProjectSubmit).toHaveBeenCalledTimes(1)
  })
  it('R10-29 Uncertain submission waits for explicit retry of the same prepared intent and key', async () => {
    const onProjectSubmit = vi.fn().mockRejectedValueOnce(new Error('transport secret'))
      .mockResolvedValueOnce(submittedOutcome())
    const { props, harness } = await mountProject({ onProjectSubmit })
    await prepareSingle()
    const intent = harness.client.catalog.mock.calls[0][0].intents[0]
    click('Review & run'); await flushProject()
    expect(screen.getByRole('alert').textContent).toBe(PROJECT_STRING_COMPOSER_REASONS.submission_uncertain)
    expect(queueLabels()).toEqual(['Group 1 slots 1 to 3'])
    await flushProject()
    expect(onProjectSubmit).toHaveBeenCalledTimes(1)
    expect(props.newProjectIntentKey).toHaveBeenCalledTimes(1)
    click('Refresh preview'); await readyProject()
    expect(screen.getByRole('alert').textContent).toBe(PROJECT_STRING_COMPOSER_REASONS.submission_uncertain)
    expect(props.newProjectIntentKey).toHaveBeenCalledTimes(1)
    click('Retry'); await flushProject()
    expect(onProjectSubmit).toHaveBeenCalledTimes(2)
    expect(onProjectSubmit.mock.calls[0][0]).toBe(intent)
    expect(onProjectSubmit.mock.calls[1][0]).toBe(intent)
    expect(screen.getByText(PROJECT_STRING_COMPOSER_REASONS.submitted)).toBeTruthy()
    cleanup()
    for (const result of [undefined, { ok: false, status: null, reason: 'SIP_R7_TRANSPORT_FAILED', envelope: null },
      { ok: true, status: 200, data: submittedOutcome().data },
      { ok: true, status: 202, data: { status: 'submitted', job_id: 7 } },
      { ok: false, status: 503, reason: 'HTTP_ERROR', envelope: null }]) {
      const callback = vi.fn().mockResolvedValueOnce(result).mockResolvedValueOnce(submittedOutcome())
      const next = await mountProject({ onProjectSubmit: callback })
      await prepareSingle(); click('Review & run'); await flushProject()
      expect(screen.getByRole('alert').textContent).toBe(PROJECT_STRING_COMPOSER_REASONS.submission_uncertain)
      expect(callback).toHaveBeenCalledTimes(1)
      const retained = callback.mock.calls[0][0]
      click('Retry'); await flushProject()
      expect(callback.mock.calls[1][0]).toBe(retained)
      expect(next.props.newProjectIntentKey).toHaveBeenCalledTimes(1)
      cleanup()
    }
  })
  it('R10-30 StrictMode unmount and pre microtask changes suppress obsolete reads and own only subscriptions', async () => {
    const harness = projectHarness()
    const props = harness.props()
    const strict = render(<React.StrictMode><SolarStringComposer {...props} /></React.StrictMode>)
    await flushProject()
    expect(harness.client.readContext).toHaveBeenCalledTimes(1)
    expect(harness.client.subscribe).toHaveBeenCalledTimes(2)
    expect(harness.unsubscribe).toHaveBeenCalledTimes(1)
    await prepareSingle()
    click('Review & run'); strict.unmount(); await flushProject()
    expect(harness.client.readContext).toHaveBeenCalledTimes(1)
    expect(props.onProjectSubmit).not.toHaveBeenCalled()
    expect(harness.unsubscribe).toHaveBeenCalledTimes(2)
    for (const method of ['setScope', 'dispose', 'acquireCheckout', 'releaseCheckout']) expect(harness.client[method]).not.toHaveBeenCalled()
    const before = projectHarness(), after = projectHarness(nextCanonicalBinding)
    const a = before.props(), b = after.props()
    const mounted = render(<SolarStringComposer {...a} />)
    mounted.rerender(<SolarStringComposer {...b} />)
    await flushProject()
    expect(before.client.readContext).not.toHaveBeenCalled()
    expect(after.client.readContext).toHaveBeenCalledTimes(1)
    cleanup()
    const abandoned = projectHarness()
    const immediate = render(<SolarStringComposer {...abandoned.props()} />)
    immediate.unmount(); await flushProject()
    expect(abandoned.client.readContext).not.toHaveBeenCalled()
    const callback = deferred()
    const duringCallback = await mountProject({ onProjectSubmit: vi.fn(() => callback.promise) })
    await prepareSingle(); click('Review & run'); await flushProject()
    const options = duringCallback.props.onProjectSubmit.mock.calls[0][1]
    duringCallback.unmount()
    expect(options.signal.aborted).toBe(true)
    await act(async () => { callback.reject(new Error('obsolete callback failure')) })
    expect(options.isCurrent()).toBe(false)
    expect(duringCallback.harness.unsubscribe).toHaveBeenCalledTimes(1)
  })
  it('R10-31 Own canonical props never fall back and incomplete legacy hints retain their refusal', async () => {
    for (const solarBinding of [undefined, null, { ...canonicalBinding, input_version_id: 7 }]) {
      const harness = projectHarness()
      const props = harness.props({ solarBinding, projectId: null, drawingId: 'd1', drawingVersion: 7 })
      render(<SolarStringComposer {...props} />); await flushProject()
      expect(screen.getByText(PROJECT_STRING_COMPOSER_REASONS.binding_invalid)).toBeTruthy()
      expect(harness.client.readContext).not.toHaveBeenCalled()
      expect(props.readIntake).not.toHaveBeenCalled()
      expect(props.onSubmit).not.toHaveBeenCalled()
      cleanup()
    }
    for (const overrides of [{ authorityKey: null }, { projectClient: null },
      { newProjectIntentKey: null }, { onProjectSubmit: null }]) {
      const harness = projectHarness()
      const props = harness.props(overrides)
      render(<SolarStringComposer {...props} />); await flushProject()
      expect(screen.getByText(PROJECT_STRING_COMPOSER_REASONS.submit_unavailable)).toBeTruthy()
      expect(harness.client.readContext).not.toHaveBeenCalled()
      expect(props.readIntake).not.toHaveBeenCalled()
      expect(props.onSubmit).not.toHaveBeenCalled()
      cleanup()
    }
    const mismatch = projectHarness()
    mismatch.move(nextCanonicalBinding)
    await mountProject({}, mismatch)
    expect(screen.getByText(PROJECT_STRING_COMPOSER_REASONS.binding_invalid)).toBeTruthy()
    expect(mismatch.client.readContext).not.toHaveBeenCalled()
    cleanup()
    const invalidKey = await mountProject({ newProjectIntentKey: vi.fn(() => '') })
    addRange(); await flushProject()
    expect(screen.getByText(PROJECT_STRING_COMPOSER_REASONS.idempotency_key_required)).toBeTruthy()
    expect(invalidKey.harness.client.catalog).not.toHaveBeenCalled()
    const corrected = { ...invalidKey.props, newProjectIntentKey: vi.fn(() => 'corrected-key') }
    invalidKey.rerender(<SolarStringComposer {...corrected} />)
    click('Refresh preview'); await readyProject()
    expect(corrected.newProjectIntentKey).toHaveBeenCalledTimes(1)
    cleanup()
    const { props } = await mount(ground, { projectId: 'p1' })
    expect(screen.getByText(SOLAR_STRING_COMPOSER_REASONS.scope)).toBeTruthy()
    expect(props.readIntake).not.toHaveBeenCalled()
    cleanup()
    const project = await mountProject({ projectId: 'p1', drawingId: 'd1', drawingVersion: 7 })
    await prepareSingle(); click('Review & run'); await flushProject()
    expect(project.props.readIntake).not.toHaveBeenCalled()
    expect(project.props.onSubmit).not.toHaveBeenCalled()
  })
  it('R10-42 A current initial-read rejection leaves loading with fixed feedback', async () => {
    const harness = projectHarness()
    harness.client.readContext.mockRejectedValueOnce(new Error('private read failure'))
    const { container } = await mountProject({}, harness)
    await flushProject()
    expect(screen.queryByText(PROJECT_STRING_COMPOSER_REASONS.loading)).toBeNull()
    expect(screen.getByText(PROJECT_STRING_COMPOSER_REASONS.context_unreadable)).toBeTruthy()
    expect(container.textContent).not.toContain('private read failure')
  })
  it('R10-43 Checkout rotation aborts a locked callback and refreshes the same intent', async () => {
    const pending = deferred()
    const { props, harness } = await mountProject({ onProjectSubmit: vi.fn(() => pending.promise) })
    await prepareSingle()
    const intent = harness.client.catalog.mock.calls.at(-1)[0].intents[0]
    click('Review & run'); await flushProject()
    expect(props.onProjectSubmit).toHaveBeenCalledTimes(1)
    expect(props.onProjectSubmit.mock.calls[0][0]).toBe(intent)
    const guard = props.onProjectSubmit.mock.calls[0][1]
    expect(guard.signal.aborted).toBe(false)
    expect(guard.isCurrent()).toBe(true)
    act(() => harness.rotate())
    expect(guard.signal.aborted).toBe(true)
    expect(guard.isCurrent()).toBe(false)
    await act(async () => { pending.resolve(submittedOutcome()) })
    await readyProject()
    expect(screen.queryByText(PROJECT_STRING_COMPOSER_REASONS.submitted)).toBeNull()
    expect(harness.client.catalog).toHaveBeenCalledTimes(2)
    expect(harness.client.catalog.mock.calls.at(-1)[0].intents[0]).toBe(intent)
    expect(props.newProjectIntentKey).toHaveBeenCalledTimes(1)
  })
  it('R10-44 Source pagination preserves the prepared intent and key', async () => {
    const harness = projectHarness()
    harness.client.readContext.mockResolvedValueOnce({ ok: true, status: 200,
      data: canonicalContext(canonicalBinding, rooftop) })
    const { props } = await mountProject({}, harness)
    click(`Add panel ${Q[0]}`); await readyProject()
    const intent = harness.client.catalog.mock.calls.at(-1)[0].intents[0]
    fireEvent.click(within(screen.getByRole('region', { name: 'Panels' })).getByRole('button', { name: 'Next page' }))
    await flushProject()
    expect(props.newProjectIntentKey).toHaveBeenCalledTimes(1)
    expect(harness.client.catalog.mock.calls.at(-1)[0].intents[0]).toBe(intent)
  })
  it('R10-45 A malformed string job UUID remains uncertain', async () => {
    const outcome = submittedOutcome()
    outcome.data.job_id = 'not-a-uuid'
    const onProjectSubmit = vi.fn(async () => outcome)
    await mountProject({ onProjectSubmit })
    await prepareSingle()
    click('Review & run'); await flushProject()
    expect(screen.queryByText(PROJECT_STRING_COMPOSER_REASONS.submitted)).toBeNull()
    expect(screen.getByRole('alert').textContent).toBe(PROJECT_STRING_COMPOSER_REASONS.submission_uncertain)
    expect(onProjectSubmit).toHaveBeenCalledTimes(1)
  })
  it('R10-46 A foreign-project client snapshot prevents the initial read', async () => {
    const harness = projectHarness()
    harness.move({ ...canonicalBinding, project_id: nextCanonicalBinding.input_version_id })
    const { props } = await mountProject({}, harness)
    expect(harness.client.readContext).not.toHaveBeenCalled()
    expect(props.readIntake).not.toHaveBeenCalled()
    expect(screen.getByText(PROJECT_STRING_COMPOSER_REASONS.binding_invalid)).toBeTruthy()
  })
  it('R10-47 Submitted feedback disappears after client-only scope movement', async () => {
    const calls = []
    const fetchImpl = vi.fn(async (url, options) => {
      calls.push({ url, options })
      const data = url.includes('/context?') ? canonicalContext() : url.startsWith('/api/capabilities?')
        ? acceptedCatalog({ tool: STRING_ADD_TOOL, catalogDigest: canonicalDigest }).data : submittedOutcome().data
      return { ok: true, status: url === '/api/run' ? 202 : 200, json: async () => data }
    })
    const client = createSolarProjectClient({ fetchImpl })
    client.setScope(canonicalBinding, 'session')
    const harness = projectHarness()
    const props = harness.props({ projectClient: client,
      onProjectSubmit: vi.fn((intent, options) => client.submitRun(intent, options)) })
    const mounted = render(<SolarStringComposer {...props} />)
    await flushProject(); await prepareSingle()
    click('Review & run'); await flushProject()
    expect(screen.getByText(PROJECT_STRING_COMPOSER_REASONS.submitted)).toBeTruthy()
    expect(calls.filter(({ url }) => url.includes('/context?'))).toHaveLength(2)
    expect(calls.filter(({ url }) => url === '/api/run')).toHaveLength(1)
    act(() => client.setScope(nextCanonicalBinding, 'session'))
    await flushProject()
    expect(screen.queryByText(PROJECT_STRING_COMPOSER_REASONS.submitted)).toBeNull()
    expect(screen.getByText(PROJECT_STRING_COMPOSER_REASONS.scope_stale)).toBeTruthy()
    expect(props.solarBinding).toBe(canonicalBinding)
    expect(props.projectClient).toBe(client)
    mounted.unmount(); client.dispose()
  })
  it('R10-33 Real R7 sanitizes dotted HTTP reasons and component never renders raw envelopes', async () => {
    let refusal
    const client = createSolarProjectClient({ fetchImpl: vi.fn(async (url) => url.includes('/context?')
      ? { ok: true, status: 200, json: async () => canonicalContext() }
      : { ok: false, status: 409, json: async () => ({ error: { reason_code: 'bad.reason', message: 'raw server secret' },
        checkout_capability: 'lco1.' + 'a'.repeat(64) }) }) })
    client.setScope(canonicalBinding, 'session')
    const supplied = { ...client, catalog: vi.fn(async (options) => { refusal = await client.catalog(options); return refusal }) }
    const { container } = await mountProject({ projectClient: supplied })
    addRange(); await flushProject()
    expect(refusal.reason).toBe('HTTP_ERROR')
    expect(refusal.status).toBe(409)
    expect(refusal.envelope.error.reason_code).toBe('bad.reason')
    expect(refusal.envelope).not.toHaveProperty('checkout_capability')
    expect(Object.isFrozen(refusal.envelope.error)).toBe(true)
    expect(screen.getByRole('alert').textContent).toBe(PROJECT_STRING_COMPOSER_REASONS.preview_refused)
    expect(container.textContent).not.toContain('raw server secret')
    expect(container.textContent).not.toContain('bad.reason')
    expect(container.textContent).not.toContain('lco1.')
    cleanup(); client.dispose()
  })
  it('R10-50 A refused submission shows the fixed sentence and never the server diagnostic', async () => {
    const onProjectSubmit = vi.fn(async () => ({ ok: false, status: 409, reason: 'HTTP_ERROR',
      envelope: { error: { message: 'private server diagnostic' } } }))
    const { container } = await mountProject({ onProjectSubmit })
    await prepareSingle(); click('Review & run'); await flushProject()
    expect(onProjectSubmit).toHaveBeenCalledTimes(1)
    expect(screen.getByRole('alert').textContent).toBe(PROJECT_STRING_COMPOSER_REASONS.preview_refused)
    expect(container.textContent).not.toContain('private server diagnostic')
    cleanup()
  })
})
