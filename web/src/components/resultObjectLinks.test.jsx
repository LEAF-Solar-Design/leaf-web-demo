// @vitest-environment jsdom
import React from 'react'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import ResultPanel from './ResultPanel.jsx'
import DrawingOriginBridge from '../site/DrawingOriginBridge.jsx'

const context = vi.hoisted(() => ({ current: null }))
vi.mock('../site/DrawingObjectsContext.jsx', () => ({ useDrawingObjects: () => context.current }))
const key = 'engine:drawing-v1.dxf'
const record = { id: 'h:A1', name: 'Panel Alpha', path: 'Site / Frame 1' }
const envelope = (extra = {}) => ({ ok: true, origin: { drawingKey: key }, overlay: { highlight_handles: ['A1'] }, ...extra })
let frames, frameId
beforeEach(() => {
  context.current = { index: { drawingKey: key, byHandle: new Map([['A1', record]]) }, focus: vi.fn(), selectedHandles: ['B2'] }
  frames = new Map(); frameId = 0
  vi.stubGlobal('requestAnimationFrame', vi.fn((fn) => { frames.set(++frameId, fn); return frameId }))
  vi.stubGlobal('cancelAnimationFrame', vi.fn((id) => frames.delete(id)))
})
afterEach(() => { cleanup(); vi.unstubAllGlobals() })
const flushFrame = () => act(() => {
  const callbacks = [...frames.values()]; frames.clear(); callbacks.forEach((fn) => fn())
})
function open() { fireEvent.click(screen.getByText('Show highlighted objects')) }

describe('report object links', () => {
  it('uses named navigation jumps, leaving selection alone and recording Back', () => {
    const back = [], select = vi.fn()
    const navigation = { jump: vi.fn((id) => { back.push('previous camera'); return { ok: true } }), select }
    render(<ResultPanel result={envelope()} navigation={navigation} />)
    expect(screen.getByText('1 panel highlighted in the viewer')).toBeInTheDocument()
    open()
    fireEvent.click(screen.getByRole('button', { name: 'Panel Alpha Site / Frame 1' }))
    expect(navigation.jump).toHaveBeenCalledWith(record.id)
    expect(context.current.focus).not.toHaveBeenCalled()
    expect(select).not.toHaveBeenCalled()
    expect(context.current.selectedHandles).toEqual(['B2'])
    expect(back.pop()).toBe('previous camera')
    flushFrame()
    expect(screen.getByRole('status')).toHaveTextContent('Showing Panel Alpha')
  })

  it.each([
    ['no origin', 'This result does not record which drawing it ran on.'],
    ['different drawing', 'This result ran on a different drawing or version than the one open now.'],
    ['loading', "The drawing's object list is still loading."],
    ['missing', 'This object is not in the drawing that is open now.'],
  ])('%s leaves the handle as text with its reason', (kind, reason) => {
    const result = envelope(), jump = vi.fn()
    if (kind === 'no origin') delete result.origin
    if (kind === 'different drawing') context.current.index.drawingKey = 'console:other'
    if (kind === 'loading') context.current.index = null
    if (kind === 'missing') context.current.index.byHandle.clear()
    render(<ResultPanel result={result} navigation={{ jump }} />)
    open()
    expect(screen.getAllByText(reason)).toHaveLength(1)
    expect(screen.queryByRole('button')).toBeNull()
    fireEvent.click(screen.getByText('Handle A1'))
    expect(jump).not.toHaveBeenCalled()
    expect(context.current.focus).not.toHaveBeenCalled()
  })

  it('shows a shared unavailable reason only once for multiple handles', () => {
    render(<ResultPanel result={envelope({ origin: undefined, overlay: { highlight_handles: ['A1', 'B2'] } })} />)
    open()
    expect(screen.getAllByText('This result does not record which drawing it ran on.')).toHaveLength(1)
    expect(screen.getAllByRole('listitem')).toHaveLength(2)
    expect(screen.getByText('2 panels highlighted in the viewer')).toBeInTheDocument()
  })

  it('identifies a different drawing even for a result without an overlay', () => {
    context.current.index.drawingKey = 'console:other'
    render(<ResultPanel result={envelope({ overlay: undefined })} />)
    expect(screen.getByText('This result ran on a different drawing or version than the one open now.')).toBeInTheDocument()
    expect(screen.queryByText('Show highlighted objects')).toBeNull()
  })

  it('repeats success and refusal announcements and cancels pending frames on unmount', () => {
    const jump = vi.fn(() => ({ ok: true }))
    const view = render(<ResultPanel result={envelope()} navigation={{ jump }} />)
    const live = screen.getByRole('status')
    open()
    const button = screen.getByRole('button', { name: /Panel Alpha/ })
    for (let i = 0; i < 2; i++) {
      fireEvent.click(button)
      expect(live.textContent).toBe('')
      flushFrame()
      expect(live.textContent).toBe('Showing Panel Alpha')
    }
    jump.mockReturnValue({ ok: false, reason: 'This object is hidden by its layers.' })
    for (let i = 0; i < 2; i++) {
      fireEvent.click(button)
      expect(live.textContent).toBe('')
      flushFrame()
      expect(live.textContent).toBe('This object is hidden by its layers.')
    }
    fireEvent.click(button)
    view.unmount()
    expect(frames.size).toBe(0)
  })

  it('reaches all 2,345 handles in order with at most 100 rows mounted', () => {
    const handles = Array.from({ length: 2345 }, (_, i) => (i + 1).toString(16).toUpperCase())
    context.current.index.byHandle = new Map(handles.map((handle, i) => [handle, { id: `h:${handle}`, name: `Panel ${i + 1}`, path: 'Site' }]))
    const jump = vi.fn(() => ({ ok: true }))
    const view = render(<ResultPanel result={envelope({ overlay: { highlight_handles: handles } })} navigation={{ jump }} />)
    open()
    for (let page = 0; page < 24; page++) {
      const rows = view.container.querySelectorAll('.result-object-links li')
      expect(rows.length).toBe(Math.min(100, 2345 - page * 100))
      expect(rows[0].textContent).toBe(`Panel ${page * 100 + 1}Site`)
      expect(rows[rows.length - 1].textContent).toBe(`Panel ${Math.min(2345, (page + 1) * 100)}Site`)
      if (page < 23) fireEvent.click(screen.getByRole('button', { name: 'Show 100 more' }))
    }
    fireEvent.click(screen.getByRole('button', { name: 'Panel 2345 Site' }))
    expect(jump).toHaveBeenCalledWith(`h:${handles[2344]}`)
    expect(screen.queryByRole('button', { name: 'Show 100 more' })).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Previous 100' }))
    expect(view.container.querySelectorAll('.result-object-links li')).toHaveLength(100)
    view.rerender(<ResultPanel result={envelope()} navigation={{ jump }} />)
    expect(screen.getByRole('button', { name: /Panel 161 Site/ })).toBeInTheDocument()
  }, 15000)

  it('reports the current provider key through the bridge, including loading', () => {
    const onChange = vi.fn()
    const view = render(<DrawingOriginBridge onChange={onChange} />)
    expect(onChange).toHaveBeenLastCalledWith(key)
    context.current.index = null
    view.rerender(<DrawingOriginBridge onChange={onChange} />)
    expect(onChange).toHaveBeenLastCalledWith(null)
  })
})
