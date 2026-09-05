// @vitest-environment jsdom
//
// Standardization slice 7b: the provenance chip on the product tab band.
// It renders ONLY on a tab whose overlay actually touched `chrome` (the slot
// that gates this tab's existence), never on a default tenant — an empty
// overlay must render the same four tabs, no chip, byte-identical to today.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'

vi.mock('../api.js', () => ({ getSurfaceConfig: vi.fn() }))

import { getSurfaceConfig } from '../api.js'
import ProductSurfaceTabs from './ProductSurfaceTabs.jsx'
import { _resetSurfaceConfigOverlayForTests } from '../site/useSurfaceContract.js'
import { productSurfaceStates } from '../site/productSurfaces.js'

const states = productSurfaceStates({ sessionActive: true, hasDrawing: true, apsLive: true, iosReady: false })

beforeEach(() => {
  vi.clearAllMocks()
  _resetSurfaceConfigOverlayForTests()
})
afterEach(() => {
  cleanup()
  _resetSurfaceConfigOverlayForTests()
})

describe('slice 7b: surface-config provenance chip', () => {
  it('renders no chip for an empty overlay (default tenant)', async () => {
    getSurfaceConfig.mockResolvedValue({ surfaces: {} })
    render(<ProductSurfaceTabs activeSurface="cad" states={states} onSelect={() => {}} mock={false} />)
    await vi.waitFor(() => expect(getSurfaceConfig).toHaveBeenCalledTimes(1))
    expect(screen.queryByTestId('surface-config-provenance')).toBeNull()
    expect(screen.getAllByRole('tab')).toHaveLength(4)
  })

  it('renders the chip with the sha8 on the tab whose overlay flips chrome.tab', async () => {
    getSurfaceConfig.mockResolvedValue({
      surfaces: { sheets: { chrome: { tab: true } } },
      source: { sha256: 'abcdef0123456789', authored_at: '2026-09-04T00:00:00Z' },
    })
    render(<ProductSurfaceTabs activeSurface="cad" states={states} onSelect={() => {}} mock={false} />)
    const sheetsTab = await screen.findByRole('tab', { name: 'Sheets' })
    const chip = screen.getByTestId('surface-config-provenance')
    expect(sheetsTab.contains(chip)).toBe(true)
    expect(chip.textContent).toBe('surface config authored by Claude · abcdef01')
    // Only the newly-authored tab carries a chip — the four default tabs stay quiet.
    expect(screen.getAllByTestId('surface-config-provenance')).toHaveLength(1)
  })
})
