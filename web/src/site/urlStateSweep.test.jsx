// @vitest-environment jsdom
//
// S24 (A14) reload sweep: for EACH view key (drawer, tool, sel, cam), drive the
// state the way the studio does, unload the page (unmount, fresh module
// graph), load it again on the same URL, and prove the state comes back. The
// surfaces below seat their keys with the same hooks and the same restore
// shapes App.jsx and ToolCast.jsx use (mounting either needs a live transport
// and a dozen controllers; app-wiring.test.mjs pins that both call these
// hooks). Every step also re-checks that the boot flags are byte-identical.
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { useCallback, useRef, useState } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const BOOT = '?demo=off&drawing=d-7&code=Ab%2Fc+d&state=eyJ0%3D'

let url
async function loadPage() {
  cleanup()
  vi.resetModules()
  url = await import('../lib/urlState.js')
  return url
}

function bootIntact() {
  expect(window.location.search.startsWith(BOOT)).toBe(true)
  expect(window.location.pathname).toBe('/app')
  expect(window.location.hash).toBe('#sheet-2')
}

beforeEach(() => window.history.replaceState(null, '', `/app${BOOT}#sheet-2`))
afterEach(() => {
  cleanup()
  vi.useRealTimers()
  window.history.replaceState(null, '', '/')
})

// App's drawer seat: studio drawers plus the session Details drawer.
function DrawerSurface() {
  const { useViewParamSeat, pushOnOpen } = url
  const [studio, setStudio] = useState('none')
  const [details, setDetails] = useState(null)
  const restore = useCallback((name) => {
    if (name === 'details') { setDetails({ urlKey: 'details' }); return true }
    setDetails(null)
    if (name == null) { setStudio('none'); return true }
    setStudio(name)
    return true
  }, [])
  useViewParamSeat('drawer', {
    value: details?.urlKey === 'details' ? 'details' : studio === 'none' ? null : studio,
    onRestore: restore,
    mode: (previous, next) => (previous === 'details' ? 'replace' : pushOnOpen(previous, next)),
  })
  return (
    <div>
      <output data-testid="studio">{studio}</output>
      <output data-testid="details">{details ? 'open' : 'closed'}</output>
      <button type="button" onClick={() => setDetails({ urlKey: 'details' })}>Details</button>
      <button type="button" onClick={() => setDetails(null)}>Close details</button>
      <button type="button" onClick={() => setStudio('jobs')}>Jobs</button>
    </div>
  )
}

const TOOLS = [{ name: 'place_combiner' }, { name: 'string_layout' }]

// App's and ToolCast's tool seat: restored once the catalog has loaded.
function ToolSurface({ tools, enabled = true, ready = tools.length > 0 }) {
  const { useViewParamSeat, pushOnOpen } = url
  const [openTool, setOpenTool] = useState(null)
  useViewParamSeat('tool', {
    value: openTool?.name ?? null,
    enabled,
    ready,
    onRestore: (name) => {
      if (name == null) { setOpenTool(null); return true }
      const found = tools.find((tool) => tool?.name === name)
      if (!found) return false
      setOpenTool(found)
      return true
    },
    mode: pushOnOpen,
  })
  return (
    <div>
      <output data-testid="tool">{openTool?.name ?? 'none'}</output>
      {tools.map((tool) => (
        <button key={tool.name} type="button" onClick={() => setOpenTool(tool)}>{tool.name}</button>
      ))}
    </div>
  )
}

// The selection seat: the URL handle waits for the drawing to load.
function SelectionSurface({ drawing }) {
  const { useViewParamSeat } = url
  const [selected, setSelected] = useState(null)
  useViewParamSeat('sel', {
    value: selected,
    ready: drawing != null,
    onRestore: (handle) => {
      if (handle == null) { setSelected(null); return true }
      if (!drawing.handles.includes(handle)) return false
      setSelected(handle)
      return true
    },
    mode: 'replace',
  })
  return (
    <div>
      <output data-testid="sel">{selected ?? 'none'}</output>
      <button type="button" onClick={() => setSelected('2B')}>Pick 2B</button>
    </div>
  )
}

// ToolCast's camera preset seat (Focus 3D).
function FocusSurface() {
  const { useViewParamSeat, CAM_FOCUS } = url
  const [focus, setFocus] = useState(false)
  useViewParamSeat('cam', {
    value: focus ? CAM_FOCUS : null,
    onRestore: (preset) => { setFocus(preset === CAM_FOCUS); return true },
    mode: 'replace',
  })
  return (
    <div>
      <output data-testid="focus">{focus ? 'focus' : 'controls'}</output>
      <button type="button" onClick={() => setFocus(true)}>Focus 3D</button>
    </div>
  )
}

// App's camera seat over a viewer that publishes poses like Viewer.jsx.
function fakeViewer(initial) {
  let pose = initial
  const listeners = new Set()
  const viewer = {
    setViews: [],
    subscribeCamera(listener) {
      listeners.add(listener)
      listener({ pose })
      return () => listeners.delete(listener)
    },
    setView(next) {
      viewer.setViews.push(next)
      pose = next === 'home' ? initial : { target: [next.center.x, next.center.y, 0], zoom: next.zoom }
      for (const listener of listeners) listener({ pose })
      return true
    },
    getPose() { return pose },
    move(next) {
      pose = next
      for (const listener of listeners) listener({ pose })
    },
  }
  return viewer
}

function CameraSurface({ viewer, delayMs = 0 }) {
  const { useCameraViewParam } = url
  const viewerRef = useRef(viewer)
  useCameraViewParam(viewerRef, { ready: true, delayMs })
  return null
}

describe('a reload restores each view key', () => {
  it('drawer: Details opens with a push, survives a reload, and Back closes it', async () => {
    await loadPage()
    const start = window.history.length
    render(<DrawerSurface />)
    fireEvent.click(screen.getByText('Details'))
    expect(window.location.search).toBe(`${BOOT}&drawer=details`)
    expect(window.history.length).toBe(start + 1)
    bootIntact()

    await loadPage()
    render(<DrawerSurface />)
    await waitFor(() => expect(screen.getByTestId('details').textContent).toBe('open'))
    expect(window.location.search).toBe(`${BOOT}&drawer=details`)

    act(() => { window.history.back() })
    await waitFor(() => expect(screen.getByTestId('details').textContent).toBe('closed'))
    expect(window.location.search).toBe(BOOT)
    bootIntact()
  })

  it('drawer: a studio drawer restores on reload and a close replaces', async () => {
    await loadPage()
    render(<DrawerSurface />)
    fireEvent.click(screen.getByText('Jobs'))
    expect(window.location.search).toBe(`${BOOT}&drawer=jobs`)

    await loadPage()
    render(<DrawerSurface />)
    await waitFor(() => expect(screen.getByTestId('studio').textContent).toBe('jobs'))
    expect(window.location.search).toBe(`${BOOT}&drawer=jobs`)
    const before = window.history.length
    fireEvent.click(screen.getByText('Details'))
    expect(window.location.search).toBe(`${BOOT}&drawer=details`)
    fireEvent.click(screen.getByText('Close details'))
    expect(window.location.search).toBe(`${BOOT}&drawer=jobs`)
    expect(window.history.length).toBe(before + 1)
    bootIntact()
  })

  it('tool: the opened tool comes back once the catalog loads; a vanished tool is cleared', async () => {
    await loadPage()
    render(<ToolSurface tools={TOOLS} />)
    fireEvent.click(screen.getByText('string_layout'))
    expect(window.location.search).toBe(`${BOOT}&tool=string_layout`)

    await loadPage()
    const { rerender } = render(<ToolSurface tools={[]} />)
    expect(screen.getByTestId('tool').textContent).toBe('none')
    expect(window.location.search).toBe(`${BOOT}&tool=string_layout`)
    rerender(<ToolSurface tools={TOOLS} />)
    await waitFor(() => expect(screen.getByTestId('tool').textContent).toBe('string_layout'))

    await loadPage()
    render(<ToolSurface tools={[{ name: 'place_combiner' }]} />)
    await waitFor(() => expect(window.location.search).toBe(BOOT))
    expect(screen.getByTestId('tool').textContent).toBe('none')
    bootIntact()
  })

  it('sel: the own selection replaces, waits for the drawing, and comes back', async () => {
    const drawing = { handles: ['1A', '2B'] }
    await loadPage()
    const start = window.history.length
    render(<SelectionSurface drawing={drawing} />)
    fireEvent.click(screen.getByText('Pick 2B'))
    expect(window.location.search).toBe(`${BOOT}&sel=2B`)
    expect(window.history.length).toBe(start)

    await loadPage()
    const { rerender } = render(<SelectionSurface drawing={null} />)
    expect(screen.getByTestId('sel').textContent).toBe('none')
    expect(window.location.search).toBe(`${BOOT}&sel=2B`)
    rerender(<SelectionSurface drawing={drawing} />)
    await waitFor(() => expect(screen.getByTestId('sel').textContent).toBe('2B'))
    bootIntact()
  })

  it('tool: pending readiness never writes over a reload value', async () => {
    await loadPage()
    window.history.replaceState(null, '', `/app${BOOT}&tool=string_layout#sheet-2`)
    const { rerender } = render(<ToolSurface tools={TOOLS} ready={false} />)
    fireEvent.click(screen.getByText('place_combiner'))
    expect(window.location.search).toBe(`${BOOT}&tool=string_layout`)
    rerender(<ToolSurface tools={TOOLS} ready />)
    await waitFor(() => expect(screen.getByTestId('tool').textContent).toBe('string_layout'))
    expect(window.location.search).toBe(`${BOOT}&tool=string_layout`)
    bootIntact()
  })

  it('sel: a handle missing from the reloaded drawing is cleared with replace', async () => {
    await loadPage()
    window.history.replaceState(null, '', `/app${BOOT}&sel=gone#sheet-2`)
    const start = window.history.length
    render(<SelectionSurface drawing={{ handles: ['1A', '2B'] }} />)
    await waitFor(() => expect(window.location.search).toBe(BOOT))
    expect(screen.getByTestId('sel').textContent).toBe('none')
    expect(window.history.length).toBe(start)
    bootIntact()
  })

  it('tool: reactivating a mounted surface restores the URL without pushing stale state', async () => {
    await loadPage()
    window.history.replaceState(null, '', `/app${BOOT}&tool=string_layout#sheet-2`)
    const start = window.history.length
    const { rerender } = render(<ToolSurface tools={TOOLS} enabled={false} />)
    fireEvent.click(screen.getByText('place_combiner'))
    expect(window.location.search).toBe(`${BOOT}&tool=string_layout`)
    rerender(<ToolSurface tools={TOOLS} />)
    await waitFor(() => expect(screen.getByTestId('tool').textContent).toBe('string_layout'))
    expect(window.history.length).toBe(start)
    expect(window.location.search).toBe(`${BOOT}&tool=string_layout`)
    bootIntact()
  })

  it('cam: the Focus 3D preset comes back', async () => {
    await loadPage()
    render(<FocusSurface />)
    fireEvent.click(screen.getByText('Focus 3D'))
    expect(window.location.search).toBe(`${BOOT}&cam=focus`)

    await loadPage()
    render(<FocusSurface />)
    await waitFor(() => expect(screen.getByTestId('focus').textContent).toBe('focus'))
    bootIntact()
  })

  it('cam: a settled camera pose is written with a replace and reapplied on reload', async () => {
    await loadPage()
    const start = window.history.length
    const first = fakeViewer({ target: [0, 0, 0], zoom: 1 })
    render(<CameraSurface viewer={first} />)
    act(() => { first.move({ target: [40.5, -12.25, 0], zoom: 2.5 }) })
    await waitFor(() => expect(window.location.search).toBe(`${BOOT}&cam=40.5,-12.25,2.5`))
    expect(window.history.length).toBe(start)

    await loadPage()
    const second = fakeViewer({ target: [0, 0, 0], zoom: 1 })
    render(<CameraSurface viewer={second} />)
    expect(second.setViews).toEqual([{ center: { x: 40.5, y: -12.25 }, zoom: 2.5 }])
    await waitFor(() => expect(window.location.search).toBe(`${BOOT}&cam=40.5,-12.25,2.5`))
    bootIntact()
  })

  it('cam: popstate restores a pose and cancels an old pending camera write', async () => {
    await loadPage()
    vi.useFakeTimers()
    window.history.replaceState(null, '', `/app${BOOT}&cam=10,20,2#sheet-2`)
    const viewer = fakeViewer({ target: [0, 0, 0], zoom: 1 })
    render(<CameraSurface viewer={viewer} delayMs={400} />)
    expect(viewer.setViews).toEqual([{ center: { x: 10, y: 20 }, zoom: 2 }])
    act(() => viewer.move({ target: [99, 99, 0], zoom: 9 }))
    act(() => {
      window.history.replaceState(null, '', `/app${BOOT}&cam=30,40,3#sheet-2`)
      window.dispatchEvent(new Event('popstate'))
    })
    expect(viewer.setViews.at(-1)).toEqual({ center: { x: 30, y: 40 }, zoom: 3 })
    act(() => vi.advanceTimersByTime(1000))
    expect(window.location.search).toBe(`${BOOT}&cam=30,40,3`)
    bootIntact()
  })

  it('cam: returning to an absent key fits home without adding the key back', async () => {
    await loadPage()
    vi.useFakeTimers()
    window.history.replaceState(null, '', `/app${BOOT}&cam=10,20,2#sheet-2`)
    const viewer = fakeViewer({ target: [0, 0, 0], zoom: 1 })
    render(<CameraSurface viewer={viewer} delayMs={400} />)
    act(() => {
      window.history.replaceState(null, '', `/app${BOOT}#sheet-2`)
      window.dispatchEvent(new Event('popstate'))
      // The real camera channel delivers the restored pose on a later frame.
      viewer.move({ target: [0, 0, 0], zoom: 1 })
      vi.advanceTimersByTime(1000)
    })
    expect(viewer.setViews.at(-1)).toBe('home')
    expect(window.location.search).toBe(BOOT)
    bootIntact()
  })

  it('cam: a saved pose waits until the viewer can apply it', async () => {
    await loadPage()
    vi.useFakeTimers()
    window.history.replaceState(null, '', `/app${BOOT}&cam=10,20,2#sheet-2`)
    const viewer = fakeViewer({ target: [0, 0, 0], zoom: 1 })
    const setView = viewer.setView
    let ready = false
    viewer.setView = (next) => ready ? setView(next) : false
    render(<CameraSurface viewer={viewer} delayMs={400} />)
    act(() => vi.advanceTimersByTime(1000))
    expect(window.location.search).toBe(`${BOOT}&cam=10,20,2`)
    expect(viewer.setViews).toEqual([])
    ready = true
    act(() => viewer.move({ target: [0, 0, 0], zoom: 1 }))
    expect(viewer.setViews).toEqual([{ center: { x: 10, y: 20 }, zoom: 2 }])
    act(() => vi.advanceTimersByTime(1000))
    expect(window.location.search).toBe(`${BOOT}&cam=10,20,2`)
    bootIntact()
  })
})
