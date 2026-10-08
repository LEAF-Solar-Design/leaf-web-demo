// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import PromptBox from './PromptBox.jsx'

const drawing = vi.hoisted(() => ({ current: null }))
vi.mock('../site/DrawingObjectsContext.jsx', () => ({ useDrawingObjects: () => drawing.current }))
const records = Array.from({ length: 10 }, (_, i) => ({ id: `h:${i}`, kind: 'panel', name: `Panel ${i}`, handles: [String(i)] }))
const index = { drawingKey: 'demo', records, byHandle: new Map(records.map((r) => [r.handles[0], r])) }
const noop = () => {}
function mount(props = {}) {
  return render(<PromptBox value="count panels" onChange={noop} onDispatch={noop} mcpDiscoveryEnabled={false} {...props} />)
}
function pickScope(container, name) {
  fireEvent.click(container.querySelector('.bar-scope'))
  const menu = screen.getByRole('listbox', { name: 'Scope' })
  fireEvent.click(within(menu).getByRole('option', { name: new RegExp(`^${name}\\b`) }))
}
beforeEach(() => {
  drawing.current = { index, selectedHandles: ['0', '1'], focus: vi.fn() }
  vi.stubGlobal('fetch', vi.fn(() => Promise.resolve({ ok: false, status: 404, json: () => Promise.resolve({}) })))
})
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

describe('visible selection context', () => {
  it('shows quiet filled selection chips left of scope and removes a focused chip with Backspace', () => {
    const { container } = mount()
    const chips = [...container.querySelectorAll('.bar-context')]
    expect(chips.map((chip) => chip.textContent)).toEqual(['Panel 0', 'Panel 1'])
    expect(chips[0].classList.contains('chip-neutral')).toBe(true)
    expect(chips[0].style.background).toBeTruthy()
    expect(chips[1].compareDocumentPosition(container.querySelector('.bar-scope')) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    chips[0].focus()
    fireEvent.keyDown(chips[0], { key: 'Backspace' })
    expect(screen.queryByRole('button', { name: 'Remove context: Panel 0' })).toBeNull()
    expect(screen.getByRole('button', { name: 'Remove context: Panel 1' })).toBeTruthy()
    expect(document.activeElement).toBe(screen.getByLabelText('Command bar'))
  })

  it('lets Escape bubble unchanged from a chip without removing context', () => {
    mount()
    const chip = screen.getByRole('button', { name: 'Remove context: Panel 0' })
    const listener = vi.fn()
    window.addEventListener('keydown', listener)
    try {
      chip.focus()
      const event = new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true })
      chip.dispatchEvent(event)
      expect(listener).toHaveBeenCalledTimes(1)
      expect(event.defaultPrevented).toBe(false)
      expect(screen.getByRole('button', { name: 'Remove context: Panel 0' })).toBe(chip)
    } finally { window.removeEventListener('keydown', listener) }
  })

  it('dispatches only shown chips, excluding removed, unknown and overflow selection', () => {
    drawing.current.selectedHandles = [...records.map((r) => r.handles[0]), 'unknown', '0']
    const onDispatch = vi.fn()
    const { container } = mount({ onDispatch })
    expect(container.querySelectorAll('.bar-context')).toHaveLength(8)
    fireEvent.click(screen.getByRole('button', { name: 'Remove context: Panel 0' }))
    fireEvent.keyDown(screen.getByLabelText('Command bar'), { key: 'Enter' })
    expect(onDispatch.mock.calls[0][0]).toBeUndefined()
    expect(onDispatch.mock.calls[0][1]).toMatchObject({ images: [], allowSecretOnce: false })
    expect(onDispatch.mock.calls[0][1].context).toEqual(records.slice(1, 8).map((r) => ({ id: r.id, kind: r.kind, label: r.name, handles: r.handles })))
    expect(onDispatch.mock.calls[0][1].context.map((c) => c.label)).toEqual([...container.querySelectorAll('.bar-context')].map((chip) => chip.textContent))
  })

  it('carries only visible context on a credential retry without retaining its authorisation', () => {
    const onDispatch = vi.fn()
    mount({ onDispatch, secretRefusal: { reason: 'Credential refused', masked: '••••', overridable: true } })
    fireEvent.click(screen.getByRole('button', { name: 'Remove context: Panel 0' }))
    fireEvent.click(screen.getByTestId('secret-send-anyway'))
    expect(onDispatch).toHaveBeenCalledTimes(1)
    expect(onDispatch.mock.calls[0][1]).toEqual({
      images: [], allowSecretOnce: true,
      context: [{ id: records[1].id, kind: records[1].kind, label: records[1].name, handles: records[1].handles }],
    })
    fireEvent.keyDown(screen.getByLabelText('Command bar'), { key: 'Enter' })
    expect(onDispatch).toHaveBeenCalledTimes(2)
    expect(onDispatch.mock.calls[1][1].allowSecretOnce).toBe(false)
    expect(onDispatch.mock.calls[1][1].context).toEqual(onDispatch.mock.calls[0][1].context)
  })

  it('updates chips on selection and drawing changes without carrying old removals', () => {
    const props = { value: 'count panels', onChange: noop, onDispatch: noop, mcpDiscoveryEnabled: false }
    const view = render(<PromptBox {...props} />)
    fireEvent.click(screen.getByRole('button', { name: 'Remove context: Panel 0' }))
    drawing.current = { ...drawing.current, selectedHandles: ['0'] }
    view.rerender(<PromptBox {...props} />)
    expect(screen.getByRole('button', { name: 'Remove context: Panel 0' })).toBeTruthy()
    drawing.current = { ...drawing.current, selectedHandles: ['0', '1'] }
    view.rerender(<PromptBox {...props} />)
    expect(screen.getByRole('button', { name: 'Remove context: Panel 0' })).toBeTruthy()
    drawing.current = { index: { drawingKey: 'other', records: [], byHandle: new Map() }, selectedHandles: ['0'] }
    view.rerender(<PromptBox {...props} />)
    expect(screen.queryByRole('button', { name: 'Remove context: Panel 0' })).toBeNull()
  })
})

describe('build drawing egress', () => {
  it('shows the egress line only after choosing build with drawing content', () => {
    const onOpenAuthor = vi.fn()
    const { container } = mount({ onOpenAuthor })
    expect(screen.queryByTestId('build-egress')).toBeNull()
    pickScope(container, 'build')
    expect(onOpenAuthor).toHaveBeenCalledTimes(1)
    expect(screen.getByTestId('build-egress').textContent).toBe('Sends the drawing to Claude')
    expect(screen.queryByRole('listbox', { name: 'Search results' })).toBeNull()
    pickScope(container, 'act')
    expect(screen.queryByTestId('build-egress')).toBeNull()
    pickScope(container, 'find')
    expect(screen.queryByTestId('build-egress')).toBeNull()
  })

  it('does not claim drawing egress when build has no drawing', () => {
    drawing.current = null
    const { container } = mount()
    pickScope(container, 'build')
    expect(screen.queryByTestId('build-egress')).toBeNull()
  })
})
