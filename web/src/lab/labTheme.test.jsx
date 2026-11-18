// @vitest-environment jsdom
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { cleanup, render, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('./useLabSurfaceStatus.js', () => ({ useLabSurfaceStatus: vi.fn() }))
vi.mock('./views.js', () => ({
  registerLabViews: () => ['graph', 'widgets', 'history', 'runs', 'deploys', 'test-trade'].map((id) => ({
    id, label: id, path: id === 'graph' ? '/app/lab' : `/app/lab/${id}`,
    component: ({ hidden }) => <p hidden={hidden}>Standalone {id}</p>,
  })),
}))

import LabGround from './LabGround.jsx'
import { useLabSurfaceStatus } from './useLabSurfaceStatus.js'

let originalUrl
beforeEach(() => {
  originalUrl = window.location.href
  window.history.replaceState({}, '', '/app/lab')
  useLabSurfaceStatus.mockReturnValue({ state: 'on', posture: 'local' })
})
afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  window.history.replaceState({}, '', originalUrl)
})

function luminance(hex) {
  expect(hex).toMatch(/^#[\da-f]{6}$/i)
  const channels = hex.slice(1).match(/../g).map((channel) => {
    const value = parseInt(channel, 16) / 255
    return value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4
  })
  return channels[0] * 0.2126 + channels[1] * 0.7152 + channels[2] * 0.0722
}

describe('Lab dark default', () => {
  it('scopes the active workbench to the dark theme', () => {
    const { container } = render(<LabGround active />)
    const ground = container.querySelector('section[data-ground="lab"]')
    expect(ground).toHaveAttribute('data-leaf-theme', 'dark')
    const workbench = container.querySelector('.lab-workbench')
    expect(workbench).toBeInTheDocument()
    expect(ground).toContainElement(workbench)
  })

  it('keeps the dark theme while the ground is inactive', () => {
    const { container } = render(<LabGround active={false} />)
    expect(container.querySelector('section[data-ground="lab"]'))
      .toHaveAttribute('data-leaf-theme', 'dark')
  })

  it.each([
    ['foreground', 'background'],
    ['foreground', 'surface'],
    ['primary', 'surface'],
    ['data-series-1', 'surface'],
    ['success', 'surface'],
  ])('vendored dark %s on %s meets AA contrast', (foreground, background) => {
    // jsdom uses an HTTP import.meta.url; resolve from the Vitest web root.
    const css = readFileSync(resolve(process.cwd(), 'src/lab/contract/sources/leaf-tokens.css'), 'utf8')
      .replace(/\/\*[\s\S]*?\*\//g, '')
    const profile = [...css.matchAll(/([^{}]+)\{([^{}]*)\}/g)].find(([, selector]) => {
      const selectors = selector.split(',').map((part) => part.replace(/\s+/g, ''))
      return selectors.length === 2 && selectors.includes(':root') && selectors.includes('[data-leaf-profile="mushy"]')
    })
    expect(profile).toBeDefined()
    const colors = Object.fromEntries([...profile[2].matchAll(/--leaf-dark-color-([\w-]+)\s*:\s*([^;]+);/g)]
      .map(([, role, value]) => [role, value.trim()]))
    const values = [foreground, background].map((role) => luminance(colors[role]))
    const contrast = (Math.max(...values) + 0.05) / (Math.min(...values) + 0.05)
    expect(contrast).toBeGreaterThanOrEqual(4.5)
  })
})


describe('Lab command dock clearance', () => {
  it('keeps the scroll window above the measured dock and follows its resize', async () => {
    let dockTop = 985
    const rectangle = (left, top, width, height) => ({ left, top, width, height,
      right: left + width, bottom: top + height, x: left, y: top, toJSON() {} })
    vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function () {
      if (this.dataset.ground === 'lab') return rectangle(0, 0, 1600, 1100)
      if (this.classList.contains('bar-dock')) return rectangle(440, dockTop, 720, 85)
      if (this.classList.contains('test-lab-header')) return rectangle(0, 0, 1600, 95)
      return rectangle(0, 0, 0, 0)
    })
    const { container } = render(<>
      <header className="test-lab-header" />
      <div className="bar-dock"><textarea aria-label="Command" /></div>
      <LabGround active occluders={[[".test-lab-header", "top"]]} />
    </>)
    const viewport = container.querySelector('[data-measured]')
    await waitFor(() => expect(viewport.style.top).toBe('111px'))
    expect(Number.parseFloat(viewport.style.top) + Number.parseFloat(viewport.style.height)).toBe(969)
    const workbench = container.querySelector('.lab-workbench')
    dockTop = 920
    window.dispatchEvent(new Event('resize'))
    await waitFor(() => expect(Number.parseFloat(viewport.style.top) + Number.parseFloat(viewport.style.height)).toBe(904))
    expect(container.querySelector('.lab-workbench')).toBe(workbench)
  })

  it('uses the full contained window when no command dock is present', async () => {
    vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function () {
      return this.dataset.ground === 'lab'
        ? { left: 0, top: 0, width: 800, height: 600 }
        : { left: 0, top: 0, width: 0, height: 0 }
    })
    const { container } = render(<LabGround active />)
    const viewport = container.querySelector('[data-measured]')
    await waitFor(() => expect(viewport.style.height).toBe('568px'))
    expect(viewport.style.top).toBe('16px')
  })
})
