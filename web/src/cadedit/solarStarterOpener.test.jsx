import { StrictMode } from 'react'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { act, cleanup, render } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import SolarStarterOpener, { SOLAR_STARTER_DOCUMENT_ID, SOLAR_STARTER_EMPTY_INTAKE } from './SolarStarterOpener.jsx'

const fake = vi.hoisted(() => ({ context: null }))
vi.mock('./EngineSessionProvider.jsx', () => ({
  REACH_STATE: { IDLE: 'idle', OPENING: 'opening', OPEN: 'open', FAILED: 'failed' },
  useEngineSessionContext: () => fake.context,
}))
const bytes = new Uint8Array([48, 10])
const answer = () => ({ bytes, version: 1, head: 1, source: 'sample-static' })
let fetchDxf, onStarterState, openBytes, setReach
beforeEach(() => {
  fetchDxf = vi.fn(async () => answer())
  onStarterState = vi.fn()
  openBytes = vi.fn()
  setReach = vi.fn()
  fake.context = { setReach, session: {
    documentId: '', engineParsed: false, busy: false, dirty: false, savedVersion: null,
    actions: { openBytes, save: vi.fn(), checkout: vi.fn() },
  } }
})
afterEach(cleanup)
async function settle() { await act(async () => { await Promise.resolve(); await Promise.resolve(); await Promise.resolve() }) }
function mount(initial = {}, strict = false) {
  let props = { enabled: true, fetchDxf, retryKey: 0, onStarterState, ...initial }
  const tree = () => strict ? <StrictMode><SolarStarterOpener {...props} /></StrictMode> : <SolarStarterOpener {...props} />
  const view = render(tree())
  return { ...view, update(next = {}) { props = { ...props, ...next }; view.rerender(tree()) } }
}
function pending() {
  let resolve
  fetchDxf.mockImplementation(() => new Promise((done) => { resolve = done }))
  return (value = answer()) => resolve(value)
}

it('row1 disabled demo leaves reach and engine untouched', async () => {
  mount({ enabled: false }); await settle()
  expect(fetchDxf).toHaveBeenCalledTimes(0)
  expect(openBytes).toHaveBeenCalledTimes(0)
  expect(setReach).toHaveBeenCalledTimes(0)
})
it('row2 empty engine opens exactly one local uncommitted starter', async () => {
  mount()
  expect(setReach).toHaveBeenLastCalledWith({ state: 'opening', sentence: 'opening the rooftop starter...' })
  await settle()
  expect(fetchDxf).toHaveBeenCalledTimes(1)
  expect(openBytes.mock.calls).toEqual([[bytes, 'solar-starter.dxf']])
  expect(setReach).toHaveBeenLastCalledWith({ state: 'open', sentence: '', source: 'sample-static' })
  expect(fake.context.session.savedVersion).toBeNull()
  expect(fake.context.session.actions.save).toHaveBeenCalledTimes(0)
  expect(fake.context.session.actions.checkout).toHaveBeenCalledTimes(0)
})
it('row3 an existing head wins without changing reach', async () => {
  fake.context.session.documentId = 'demo-v3.dxf'
  mount(); await settle()
  expect(fetchDxf).toHaveBeenCalledTimes(0)
  expect(openBytes).toHaveBeenCalledTimes(0)
  expect(setReach).toHaveBeenCalledTimes(0)
})
it('row4 a hand import wins', async () => {
  fake.context.session.documentId = 'roof.dxf'
  mount(); await settle()
  expect(fetchDxf).toHaveBeenCalledTimes(0)
  expect(openBytes).toHaveBeenCalledTimes(0)
})
it('row5 the dirty starter survives a profile round trip', async () => {
  Object.assign(fake.context.session, { documentId: SOLAR_STARTER_DOCUMENT_ID, dirty: true })
  const view = mount(); await settle()
  view.update({ enabled: false }); view.update({ enabled: true }); await settle()
  expect(fetchDxf).toHaveBeenCalledTimes(0)
  expect(openBytes).toHaveBeenCalledTimes(0)
  expect(fake.context.session.documentId).toBe(SOLAR_STARTER_DOCUMENT_ID)
  expect(fake.context.session.dirty).toBe(true)
})
it('row6 waits until busy clears', async () => {
  fake.context.session.busy = true
  const view = mount(); await settle()
  expect(fetchDxf).toHaveBeenCalledTimes(0)
  fake.context.session.busy = false
  view.update(); await settle()
  expect(fetchDxf).toHaveBeenCalledTimes(1)
  expect(openBytes).toHaveBeenCalledTimes(1)
})
it('row7 discards a reply after leaving the profile', async () => {
  const resolve = pending()
  const view = mount(); await settle()
  view.update({ enabled: false })
  const count = setReach.mock.calls.length
  resolve(); await settle()
  expect(openBytes).toHaveBeenCalledTimes(0)
  expect(setReach).toHaveBeenCalledTimes(count)
})
it('row8 failure is a sentence with no render retry loop', async () => {
  fetchDxf.mockRejectedValue(new Error('GET /sample.dxf -> 503'))
  const view = mount(); await settle()
  expect(setReach).toHaveBeenLastCalledWith({ state: 'failed', sentence: expect.stringMatching(/rooftop starter.*503/) })
  view.update(); await settle()
  expect(fetchDxf).toHaveBeenCalledTimes(1)
  expect(openBytes).toHaveBeenCalledTimes(0)
})
it('row9 Retry re-arms exactly one attempt', async () => {
  fetchDxf.mockRejectedValueOnce(new Error('503'))
  const view = mount(); await settle()
  expect(fetchDxf).toHaveBeenCalledTimes(1)
  view.update({ retryKey: 1 }); await settle()
  view.update(); await settle()
  expect(fetchDxf).toHaveBeenCalledTimes(2)
  expect(openBytes).toHaveBeenCalledTimes(1)
})
it('row10 missing bytes reports no document', async () => {
  fetchDxf.mockResolvedValue({ bytes: null })
  mount(); await settle()
  expect(setReach).toHaveBeenLastCalledWith({ state: 'failed', sentence: expect.stringContaining('no document') })
  expect(openBytes).toHaveBeenCalledTimes(0)
})
it('row11 StrictMode issues one fetch and one open', async () => {
  mount({}, true); await settle()
  expect(fetchDxf).toHaveBeenCalledTimes(1)
  expect(openBytes).toHaveBeenCalledTimes(1)
})
it('row12 a document arriving during fetch discards the bytes and reports idle', async () => {
  const resolve = pending()
  const view = mount(); await settle()
  fake.context.session = { ...fake.context.session, documentId: 'roof.dxf' }
  view.update()
  resolve(); await settle()
  expect(openBytes).toHaveBeenCalledTimes(0)
  expect(setReach).toHaveBeenLastCalledWith({ state: 'idle', sentence: '' })
})
it('row13 viewer bootstrap uses only the empty presentation intake', () => {
  const source = readFileSync(resolve(process.cwd(), 'src/App.jsx'), 'utf8')
  expect(source).toContain("(intake || solarStarter === 'open')")
  expect(source).toContain('intake={intake ?? SOLAR_STARTER_EMPTY_INTAKE}')
  expect(Object.isFrozen(SOLAR_STARTER_EMPTY_INTAKE)).toBe(true)
  const sample = JSON.parse(readFileSync(resolve(process.cwd(), 'public/sample.intake.json'), 'utf8'))
  expect(Object.keys(SOLAR_STARTER_EMPTY_INTAKE).sort()).toEqual(Object.keys(sample).sort())
  expect(SOLAR_STARTER_EMPTY_INTAKE.polylines).toEqual([])
})
it('row14 every reach transition reports its state and unmount reports idle', async () => {
  fetchDxf.mockRejectedValueOnce(new Error('503'))
  const view = mount(); await settle()
  view.update({ retryKey: 1 }); await settle()
  expect(onStarterState.mock.calls).toEqual(setReach.mock.calls.map(([reach]) => [reach.state]))
  expect(onStarterState.mock.calls).toEqual([['opening'], ['failed'], ['opening'], ['open']])
  view.unmount()
  expect(onStarterState).toHaveBeenLastCalledWith('idle')
})
