// @vitest-environment jsdom
import { act, cleanup, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('./labRoute.js', async (original) => ({
  ...await original(), fetchLabStatus: vi.fn(),
}))
vi.mock('../site/useSurfaceContract.js', async (original) => ({
  ...await original(),
  useSurfaceConfigOverlay: () => ({ lab: { authoring: true, chrome: { tab: true, rail: null } } }),
}))

import { fetchLabStatus } from './labRoute.js'
import { _resetLabSurfaceStatusForTests } from './useLabSurfaceStatus.js'
import LabGround from './LabGround.jsx'
import ProductSurfaceTabs from '../components/ProductSurfaceTabs.jsx'
import {
  DEFAULT_PRODUCT_SURFACE, normalizeProductSurface, productSurfaceFromSearch,
  productSurfaceStates, searchForProductSurface, surfaceContract, surfaceRailState,
} from '../site/productSurfaces.js'

beforeEach(() => {
  _resetLabSurfaceStatusForTests()
  vi.clearAllMocks()
})
afterEach(() => {
  cleanup()
  _resetLabSurfaceStatusForTests()
})

describe('Lab rail selection and tab visibility', () => {
  it.each([null, ...['off', 'auth-off', 'signed-out', 'unavailable'].map((state) => ({ state }))])(
    'hides a pending or dormant rail despite an authored tab and authoring overlay: %j', async (status) => {
      fetchLabStatus.mockReturnValue(status === null ? new Promise(() => {}) : Promise.resolve(status))
      render(<><ProductSurfaceTabs activeSurface="cad" states={productSurfaceStates()} onSelect={() => {}} mock={false} /><LabGround active /></>)
      await act(async () => {})
      expect(screen.queryByRole('tab', { name: 'Lab' })).toBeNull()
      const dormant = status?.state === 'off' ? 'Lab is turned off.'
        : status?.state === 'auth-off' ? 'Lab sign-in service is unavailable.'
          : status?.state === 'signed-out' ? 'Sign in to use Lab.' : 'Lab is unavailable right now.'
      expect(screen.getAllByRole('status', { hidden: true }).filter((el) => el.textContent === dormant)).toHaveLength(1)
      expect(surfaceRailState('lab', status)).toBe(false)
      expect(normalizeProductSurface('lab', status)).toBe(DEFAULT_PRODUCT_SURFACE)
      expect(productSurfaceFromSearch('?surface=lab', status)).toBe(DEFAULT_PRODUCT_SURFACE)
      expect(productSurfaceFromSearch('?surface=lab', status)).toBe(productSurfaceFromSearch('?surface=unknown'))
      expect(new URLSearchParams(searchForProductSurface('', 'lab')).get('surface')).toBe(DEFAULT_PRODUCT_SURFACE)
      expect(productSurfaceStates({ labStatus: status }).lab.state).toBe('hidden')
    },
  )

  it('shares the first probe, then inserts Lab after Ops and enables URL selection only when on', async () => {
    let resolve
    fetchLabStatus.mockReturnValue(new Promise((done) => { resolve = done }))
    const states = productSurfaceStates()
    render(<><ProductSurfaceTabs activeSurface="cad" states={states} onSelect={() => {}} mock={false} /><LabGround active /></>)
    expect(screen.queryByRole('tab', { name: 'Lab' })).toBeNull()
    expect(fetchLabStatus).toHaveBeenCalledTimes(1)
    await act(async () => { resolve({ state: 'on', posture: 'local' }) })
    expect(screen.getAllByRole('tab').map((tab) => tab.dataset.surface))
      .toEqual(['browser', 'cad', 'solar', 'ios', 'ops', 'lab'])
    expect(screen.getByRole('tab', { name: 'Lab' })).toHaveTextContent('Ready')
    expect(normalizeProductSurface('lab')).toBe('lab')
    expect(productSurfaceFromSearch('?surface=lab')).toBe('lab')
    expect(new URLSearchParams(searchForProductSurface('', 'lab')).get('surface')).toBe('lab')
    expect(surfaceContract('lab').toolbar.ribbon).toBe(false)
    expect(surfaceContract('lab').authoring).toBe(true)
    expect(surfaceContract('lab').entitlements).toEqual([])
  })
})

it('retains the workbench and all views while propagating hidden', async () => {
  fetchLabStatus.mockResolvedValue({ state: 'on', posture: 'local' })
  const { container, rerender } = render(<LabGround active />)
  await act(async () => {})
  const workbench = container.querySelector('.lab-workbench')
  const views = [...container.querySelectorAll('[data-lab-view]')]
  expect(views).toHaveLength(5)
  expect(views[0]).not.toHaveAttribute('hidden')
  rerender(<LabGround active={false} />)
  expect(container.querySelector('.lab-workbench')).toBe(workbench)
  expect(workbench).toHaveAttribute('hidden')
  expect(views.every((view) => view.hidden)).toBe(true)
  rerender(<LabGround active />)
  expect(container.querySelector('.lab-workbench')).toBe(workbench)
  expect([...container.querySelectorAll('[data-lab-view]')]).toEqual(views)
  expect(fetchLabStatus).toHaveBeenCalledTimes(1)
})
