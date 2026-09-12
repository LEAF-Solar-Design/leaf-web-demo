import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render } from '@testing-library/react'
import { readFileSync } from 'node:fs'
import { ProjectBoardGround } from './SurfaceGrounds.jsx'
import { CARD_SIZE, DEFAULT_POSITIONS, DEFAULT_VIEWPORT } from './WorldSpaceBoard.jsx'
import { boundsOfRects, fitCameraToBounds } from './worldSpaceGeometry.js'
import { CARD_NAMES, load, save } from './worldSpaceStore.js'

const memory = () => {
  const rows = new Map()
  return { load: vi.fn((id) => rows.get(id)), save: vi.fn((id, state) => rows.set(id, structuredClone(state))) }
}
const mount = (store, props = {}) => render(<ProjectBoardGround active worldSpace store={store} workspaceProject={{ project_id: 'project-a' }} {...props} />)
const board = (container) => container.querySelector('.ground-world')
const camera = (container) => JSON.parse(board(container).dataset.camera)
const card = (container, name = 'drawing') => container.querySelector(`[data-card="${name}"]`)

beforeEach(() => {
  // jsdom versions without PointerEvent still need pointer coordinates and ids.
  class Pointer extends MouseEvent {
    constructor(type, options) { super(type, options); this.pointerId = options?.pointerId ?? 1 }
  }
  vi.stubGlobal('PointerEvent', Pointer)
})
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

describe('Browser board world-space mount', () => {
  it('keeps the OFF path flat and shares exact tile markup with ON', () => {
    const props = {
      workspace: { drawing_versions: [{ version_id: 'v1', seq: 1, drawing_id: 'd1' }], jobs: [{ job_id: 'j1', kind: 'count' }], built_tools: [{ tool_id: 't1', name: 'Tool' }] },
      catalog: { families: [{ family_id: 'f1', label: 'Family', capabilities: [] }] },
      drawing: { name: 'Drawing', polylines: 2, layers: 1 },
    }
    const flat = mount(memory(), { ...props, worldSpace: false })
    expect(flat.container.querySelector('.ground-tiles')).not.toBeNull()
    expect(flat.container.querySelector('[data-card]')).toBeNull()
    const sections = [...flat.container.querySelectorAll('[data-tile]')].map((node) => node.outerHTML)
    flat.unmount()
    const spatial = mount(memory(), props)
    expect([...spatial.container.querySelectorAll('[data-card]')].map((node) => node.dataset.card)).toEqual(CARD_NAMES)
    expect([...spatial.container.querySelectorAll('[data-tile]')].map((node) => node.outerHTML)).toEqual(sections)
  })

  it('commits a card drag at pointerup and restores the world position', () => {
    const store = memory()
    const first = mount(store)
    const tile = card(first.container)
    const initial = tile.style.transform
    const zoom = camera(first.container).zoom
    fireEvent.pointerDown(tile, { clientX: 100, clientY: 100, pointerId: 1, button: 0 })
    fireEvent.pointerMove(tile, { clientX: 180, clientY: 140, pointerId: 1 })
    expect(tile.style.transform).not.toBe(initial)
    expect(store.save).not.toHaveBeenCalled()
    fireEvent.pointerUp(tile, { pointerId: 1 })
    const saved = store.load('project-a')
    expect(saved.positions.drawing.x).toBeCloseTo(80 / zoom)
    expect(saved.positions.drawing.y).toBeCloseTo(40 / zoom)
    const transform = tile.style.transform
    first.unmount()
    const second = mount(store)
    expect(card(second.container).style.transform).toBe(transform)
  })

  it('persists wheel camera changes and clamps both zoom limits', () => {
    const store = memory()
    const first = mount(store)
    fireEvent.wheel(board(first.container), { deltaY: -400, clientX: 300, clientY: 200 })
    const saved = camera(first.container)
    first.unmount()
    const second = mount(store)
    expect(camera(second.container)).toEqual(saved)
    for (let i = 0; i < 8; i++) fireEvent.wheel(board(second.container), { deltaY: -1000 })
    expect(camera(second.container).zoom).toBe(3)
    for (let i = 0; i < 8; i++) fireEvent.wheel(board(second.container), { deltaY: 1000 })
    expect(camera(second.container).zoom).toBe(0.25)
  })

  it('fits a tabbable card on Enter and all cards on Escape', () => {
    const { container } = mount(memory())
    expect(card(container).tabIndex).toBe(0)
    fireEvent.keyDown(card(container), { key: 'Enter' })
    expect(board(container).dataset.focusCard).toBe('drawing')
    expect(camera(container)).toEqual(fitCameraToBounds({ ...DEFAULT_POSITIONS.drawing, ...CARD_SIZE }, DEFAULT_VIEWPORT, 48))
    fireEvent.keyDown(card(container), { key: 'Escape' })
    expect(board(container).hasAttribute('data-focus-card')).toBe(false)
    expect(camera(container)).toEqual(fitCameraToBounds(boundsOfRects(CARD_NAMES.map((name) => ({ ...DEFAULT_POSITIONS[name], ...CARD_SIZE }))), DEFAULT_VIEWPORT, 48))
    fireEvent.doubleClick(card(container, 'jobs'))
    expect(board(container).dataset.focusCard).toBe('jobs')
  })

  it('pans the background, handles keyboard navigation, and cancels an unfinished drag', () => {
    const { container } = mount(memory())
    const desk = board(container)
    const initial = camera(container)
    fireEvent.pointerDown(desk, { clientX: 10, clientY: 20, pointerId: 1 })
    fireEvent.pointerMove(desk, { clientX: 60, clientY: 50, pointerId: 1 })
    expect(camera(container).x).toBe(initial.x + 50)
    fireEvent.pointerCancel(desk, { pointerId: 1 })
    expect(camera(container)).toEqual(initial)
    fireEvent.keyDown(desk, { key: 'ArrowLeft' })
    expect(camera(container).x).toBe(initial.x + 40)
    fireEvent.keyDown(desk, { key: '+' })
    expect(camera(container).zoom).toBeGreaterThan(initial.zoom)
    fireEvent.keyDown(desk, { key: '0' })
    expect(camera(container)).toEqual(initial)
  })

  it('shows six minimap markers and recenters on the clicked world point', () => {
    const { container } = mount(memory())
    const mini = container.querySelector('[data-minimap]')
    expect(mini.querySelectorAll('[data-minimap-card]')).toHaveLength(6)
    expect(mini.querySelector('[data-minimap-viewport]')).not.toBeNull()
    const initial = camera(container)
    fireEvent.click(mini, { clientX: 20, clientY: 20 })
    const overview = fitCameraToBounds(boundsOfRects(CARD_NAMES.map((name) => ({ ...DEFAULT_POSITIONS[name], ...CARD_SIZE }))), { width: 180, height: 114 }, 8)
    expect(camera(container).x).toBeCloseTo(600 - (20 - overview.x) / overview.zoom * initial.zoom)
    expect(camera(container)).not.toEqual(initial)
  })

  it('reports reduced motion and zeroes spatial transition durations', () => {
    vi.stubGlobal('matchMedia', vi.fn(() => ({ matches: true, addEventListener: vi.fn(), removeEventListener: vi.fn() })))
    const { container } = mount(memory())
    expect(board(container).dataset.reduced).toBe('true')
    const css = readFileSync(`${process.cwd()}/src/site/landing.css`, 'utf8')
    expect(css).toMatch(/@media \(prefers-reduced-motion: reduce\)\s*\{\s*\.ground-world[^}]*transition-duration:\s*0ms;/)
  })

  it('discards malformed injected state whole and isolates project scopes', () => {
    const store = memory()
    store.save('project-a', { positions: { ...DEFAULT_POSITIONS, jobs: { x: NaN, y: 0 } }, camera: { x: 99, y: 99, zoom: 1 } })
    const first = mount(store)
    expect(camera(first.container).x).not.toBe(99)
    fireEvent.wheel(board(first.container), { deltaY: -400 })
    const moved = camera(first.container)
    first.rerender(<ProjectBoardGround active worldSpace store={store} workspaceProject={{ project_id: 'project-b' }} />)
    expect(camera(first.container)).not.toEqual(moved)
  })
})

describe('per-device spatial store', () => {
  it.each(['not JSON', JSON.stringify({ positions: DEFAULT_POSITIONS, camera: { x: 0, y: 0, zoom: 0 } }), JSON.stringify({ positions: { ...DEFAULT_POSITIONS, jobs: { x: null, y: 0 } }, camera: { x: 0, y: 0, zoom: 1 } })])('discards invalid storage: %s', (raw) => {
    vi.stubGlobal('localStorage', { getItem: () => raw })
    expect(load('bad')).toBeNull()
  })
  it('never throws with storage absent or denied', () => {
    vi.stubGlobal('localStorage', undefined)
    expect(load('missing')).toBeNull()
    expect(() => save('missing', { positions: DEFAULT_POSITIONS, camera: { x: 0, y: 0, zoom: 1 } })).not.toThrow()
    vi.stubGlobal('localStorage', { getItem() { throw new Error('denied') }, setItem() { throw new Error('full') } })
    expect(load('denied')).toBeNull()
    expect(() => save('full', { positions: DEFAULT_POSITIONS, camera: { x: 0, y: 0, zoom: 1 } })).not.toThrow()
  })
})
