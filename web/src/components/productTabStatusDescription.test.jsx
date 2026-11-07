// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import '@testing-library/jest-dom/vitest'
import ProductSurfaceTabs from './ProductSurfaceTabs.jsx'
import { productSurface, productSurfaceStates } from '../site/productSurfaces.js'
import * as labSurfaceStatus from '../lab/useLabSurfaceStatus.js'

const originalUrl = window.location.href

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  window.history.replaceState({}, '', originalUrl)
})

it('keeps every tab name and describes it with its own status', () => {
  window.history.replaceState({}, '', '/app')
  const labStatus = { state: 'on', posture: 'local' }
  vi.spyOn(labSurfaceStatus, 'useLabSurfaceStatus').mockReturnValue(labStatus)
  const states = productSurfaceStates({ sessionActive: true, hasDrawing: true, apsLive: true, iosReady: false, labStatus })

  render(<ProductSurfaceTabs activeSurface="cad" states={states} onSelect={() => {}} />)

  const surfaceIds = ['browser', 'cad', 'solar', 'ios', 'ops', 'lab']
  expect(screen.getAllByRole('tab')).toHaveLength(surfaceIds.length)
  expect(screen.getByRole('tab', { name: 'Lab' })).toHaveAccessibleDescription('Ready')
  expect(screen.getByRole('tab', { name: productSurface('solar').label })).toHaveAccessibleDescription('Beta')

  for (const id of surfaceIds) {
    const label = productSurface(id).label
    const tab = screen.getByRole('tab', { name: label, exact: true })
    expect(tab).toHaveAccessibleName(label)
    expect(tab).toHaveAccessibleDescription(states[id].label)
    expect(tab).toHaveAttribute('aria-describedby', `product-surface-tab-${id}-status`)
    const status = document.getElementById(tab.getAttribute('aria-describedby'))
    expect(status).toBeInTheDocument()
    expect(tab).toContainElement(status)
    expect(status).toHaveTextContent(states[id].label)
  }
})
