// @vitest-environment jsdom
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { cleanup, render } from '@testing-library/react'
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

function studioDarkColors() {
  // jsdom uses an HTTP import.meta.url; resolve from the Vitest web root.
  const css = readFileSync(resolve(process.cwd(), 'src/lab/contract/sources/leaf-tokens.css'), 'utf8')
    .replace(/\/\*[\s\S]*?\*\//g, '')
  const profile = [...css.matchAll(/([^{}]+)\{([^{}]*)\}/g)]
    .find(([, selector]) => selector.trim() === '[data-leaf-profile="studio"]')
  expect(profile).toBeDefined()
  return Object.fromEntries([...profile[2].matchAll(/--leaf-dark-color-([\w-]+)\s*:\s*([^;]+);/g)]
    .map(([, role, value]) => [role, value.trim()]))
}

describe('Lab studio profile', () => {
  it.each([true, false])('keeps the studio profile and dark theme when active is %s', (active) => {
    const { container } = render(<LabGround active={active} />)
    const ground = container.querySelector('section[data-ground="lab"]')
    expect(ground).toHaveAttribute('data-leaf-profile', 'studio')
    expect(ground).toHaveAttribute('data-leaf-theme', 'dark')
  })

  it('declares the Leaf standard v1.2 Stage dark colors in the studio rule', () => {
    expect(studioDarkColors()).toMatchObject({
      background: '#050706',
      foreground: '#f4f4f5',
      surface: '#0e110f',
      border: '#262a27',
      primary: '#28d66f',
    })
  })

  it.each([
    ['foreground', 'background'],
    ['foreground', 'surface'],
    ['primary', 'surface'],
    ['success', 'surface'],
  ])('vendored studio dark %s on %s meets AA contrast', (foreground, background) => {
    const colors = studioDarkColors()
    const values = [foreground, background].map((role) => luminance(colors[role]))
    const contrast = (Math.max(...values) + 0.05) / (Math.min(...values) + 0.05)
    expect(contrast).toBeGreaterThanOrEqual(4.5)
  })
})
