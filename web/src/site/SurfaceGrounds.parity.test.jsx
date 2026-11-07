import * as React from 'react'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'

let SurfaceGrounds
let labLoad
let opsLoad
let browserProps
let reload

beforeEach(async () => {
  vi.resetModules()
  vi.doMock('react', () => React)
  vi.spyOn(console, 'error').mockImplementation(() => {})
  vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockReturnValue({
    top: 0, left: 0, right: 900, bottom: 700, width: 900, height: 700,
  })
  // jsdom's Location.reload is non-configurable. Intercept location reads
  // without replacing the DOM event methods used by the measured grounds.
  reload = vi.fn()
  const realWindow = window
  const location = { pathname: window.location.pathname, search: window.location.search, reload }
  vi.stubGlobal('window', new Proxy(realWindow, {
    get(target, key) {
      if (key === 'location') return location
      const value = Reflect.get(target, key, target)
      return typeof value === 'function' ? value.bind(target) : value
    },
  }))
  browserProps = vi.fn()
  vi.doMock('./BrowserAssetGround.jsx', () => ({
    BrowserAssetGround: (props) => { browserProps(props); return null },
  }))
  vi.doMock('./ProjectBoardGround.jsx', () => ({ ProjectBoardGround: () => null }))
  vi.doMock('../ios/DeviceGround.jsx', () => ({ DeviceGround: () => null }))
  labLoad = vi.fn()
  opsLoad = vi.fn()
  vi.doMock('../lab/LabGround.jsx', () => labLoad())
  vi.doMock('../integrations/OpsWorkspaceGround.jsx', () => opsLoad())
  SurfaceGrounds = (await import('./SurfaceGrounds.jsx')).default
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  for (const path of [
    'react', './BrowserAssetGround.jsx', './ProjectBoardGround.jsx',
    '../ios/DeviceGround.jsx', '../lab/LabGround.jsx', '../integrations/OpsWorkspaceGround.jsx',
    'next/dynamic',
  ]) vi.doUnmock(path)
})

function loadedGround(ground) {
  return function Loaded({ active }) {
    return <section data-ground={ground} hidden={!active}>Loaded {ground} workspace</section>
  }
}

function resetImportMock(surface) {
  // Re-register to evict Vitest's mocked module result. The mounted ground
  // retains its rejected lazy type until the real Retry handler replaces it.
  if (surface === 'lab') vi.doMock('../lab/LabGround.jsx', () => labLoad())
  else vi.doMock('../integrations/OpsWorkspaceGround.jsx', () => opsLoad())
}

it.each([
  ['lab', 'lab', 'Lab'], ['ops', 'ops', 'Ops'],
])('retries a rejected %s import inline and mounts the workspace', async (surface, ground, label) => {
  const load = surface === 'lab' ? labLoad : opsLoad
  const module = surface === 'lab'
    ? { default: loadedGround(ground) }
    : { OpsWorkspaceGround: loadedGround(ground) }
  load.mockRejectedValueOnce(new Error('Chunk unavailable')).mockResolvedValueOnce(module)
  render(<SurfaceGrounds surface={surface} studioShell />)
  const alert = await screen.findByRole('alert')
  expect(alert).toHaveClass('ground-failed')
  expect(alert).toHaveTextContent(`The ${label} workspace did not load. Your project and session are unchanged.`)
  expect(alert.closest('[data-measured]')).toHaveAttribute('data-measured', 'true')
  resetImportMock(surface)
  fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
  expect(await screen.findByText(`Loaded ${ground} workspace`)).toBeVisible()
  expect(screen.queryByRole('alert')).toBeNull()
  expect(load).toHaveBeenCalledTimes(2)
  expect(reload).not.toHaveBeenCalled()
})

it.each([
  ['lab', 'lab'], ['ops', 'ops'],
])('keeps a second %s failure actionable and preserves visibility', async (surface, ground) => {
  const load = surface === 'lab' ? labLoad : opsLoad
  load.mockRejectedValue(new Error('Still unavailable'))
  const { rerender } = render(<SurfaceGrounds surface={surface} studioShell />)
  await screen.findByRole('alert')
  resetImportMock(surface)
  fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
  const alert = await screen.findByRole('alert')
  expect(load).toHaveBeenCalledTimes(2)
  expect(screen.getByRole('button', { name: 'Retry' })).toBeVisible()
  const section = alert.closest(`[data-ground="${ground}"]`)
  rerender(<SurfaceGrounds surface="browser" studioShell />)
  expect(section).toHaveAttribute('hidden')
  if (surface === 'ops') {
    rerender(<SurfaceGrounds surface="ios" leavingGround="integrations" studioShell />)
    expect(section).not.toHaveAttribute('hidden')
    expect(section).toHaveAttribute('aria-hidden', 'true')
    expect(section).toHaveAttribute('inert')
    rerender(<SurfaceGrounds surface="ops" studioShell />)
    expect(section).not.toHaveAttribute('aria-hidden')
    expect(section).not.toHaveAttribute('inert')
  }
  expect(reload).not.toHaveBeenCalled()
})

it('passes sampleEntry through to the browser ground by identity', () => {
  const sampleEntry = { id: 'sample-project' }
  render(<SurfaceGrounds surface="browser" studioShell sampleEntry={sampleEntry} />)
  expect(browserProps.mock.lastCall[0].sampleEntry).toBe(sampleEntry)
})

it('forwards explicit layout eligibility to the Studio ground and defaults to denied', () => {
  const { rerender } = render(<SurfaceGrounds surface="cad" studioShell />)
  expect(browserProps.mock.lastCall[0].canApplyLayout).toBe(false)
  rerender(<SurfaceGrounds surface="cad" studioShell canApplyLayout />)
  expect(browserProps.mock.lastCall[0].canApplyLayout).toBe(true)
  browserProps.mockClear()
  rerender(<SurfaceGrounds surface="cad" canApplyLayout />)
  expect(browserProps).not.toHaveBeenCalled()
})

it('keeps shell fallbacks named and ground recovery free of reloads', () => {
  const site = readFileSync(resolve(process.cwd(), 'src/site/SiteRoot.jsx'), 'utf8')
  const grounds = readFileSync(resolve(process.cwd(), 'src/site/SurfaceGrounds.jsx'), 'utf8')
  expect(site).not.toContain('fallback={null}')
  expect(grounds).not.toContain('fallback={null}')
  expect(grounds).not.toContain('location.reload')
  expect(site.match(/fallback={<GroundSkeleton label="Studio"/g)).toHaveLength(2)
  expect(site).toContain('fallback={<GroundSkeleton label="Sheets"')
})

it('shows a top-left boot skeleton and recovers a failed dynamic import with Retry', async () => {
  let fail = false
  const createDynamic = vi.fn((_loader, { loading: Loading }) => {
    return function DynamicRoot() {
      if (fail) return <Loading error={new Error('Chunk unavailable')} />
      return <Loading />
    }
  })
  vi.doMock('next/dynamic', () => ({ default: createDynamic }))
  const StudioClient = (await import('../app/StudioClient.jsx')).default
  const { rerender } = render(<StudioClient />)
  const status = await screen.findByRole('status')
  expect(status).toHaveTextContent('Loading workspace')
  expect(status.parentElement.style.minHeight).toBe('100dvh')
  expect(status.parentElement.style.display).toBe('')
  expect(status.parentElement.style.textAlign).toBe('')
  fail = true
  rerender(<StudioClient />)
  expect(await screen.findByRole('alert')).toHaveTextContent('The workspace did not load. Your project and session are unchanged.')
  fail = false
  createDynamic.mockImplementationOnce(() => () => <p>Ready Studio</p>)
  fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
  expect(await screen.findByText('Ready Studio')).toBeVisible()
  expect(createDynamic).toHaveBeenCalledTimes(2)
  expect(reload).not.toHaveBeenCalled()
})
