import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import {
  DEFAULT_PRODUCT_SURFACE,
  PRODUCT_SURFACES,
  SHARED_WORKSPACE_CAPABILITIES,
  normalizeProductSurface,
  productSurface,
  productSurfaceFromSearch,
  productSurfaceStates,
  searchForProductSurface,
} from './productSurfaces.js'

describe('product surface contract', () => {
  it('defines the four profiles once over one shared capability substrate', () => {
    expect(PRODUCT_SURFACES.map(({ id }) => id)).toEqual(['browser', 'cad', 'solar', 'ios'])
    expect(new Set(PRODUCT_SURFACES.map(({ id }) => id)).size).toBe(4)
    expect(SHARED_WORKSPACE_CAPABILITIES).toEqual(expect.arrayContaining([
      'conversation', 'annotations', 'approvals', 'marathons', 'one-shot execution',
    ]))
  })

  it('fails closed to the live CAD profile for missing or invalid selectors', () => {
    expect(normalizeProductSurface('unknown')).toBe(DEFAULT_PRODUCT_SURFACE)
    expect(productSurfaceFromSearch('?surface=unknown')).toBe(DEFAULT_PRODUCT_SURFACE)
    expect(productSurface('unknown').id).toBe(DEFAULT_PRODUCT_SURFACE)
  })

  it('preserves auth and proof parameters while changing only the product surface', () => {
    const next = searchForProductSurface('?code=abc&state=xyz&proof=1&surface=cad', 'ios')
    const params = new URLSearchParams(next)
    expect(params.get('code')).toBe('abc')
    expect(params.get('state')).toBe('xyz')
    expect(params.get('proof')).toBe('1')
    expect(params.get('surface')).toBe('ios')
  })

  it('reports capability truth without inventing unavailable routes', () => {
    expect(productSurfaceStates({ sessionActive: false, hasDrawing: true, apsLive: true }).cad.state).toBe('sign-in')
    expect(productSurfaceStates({ sessionActive: true, hasDrawing: false, apsLive: true }).cad.state).toBe('setup')
    expect(productSurfaceStates({ sessionActive: true, hasDrawing: true, apsLive: false }).cad.state).toBe('unavailable')
    expect(productSurfaceStates({ sessionActive: true, hasDrawing: true, apsLive: true }).cad.state).toBe('available')
    expect(productSurfaceStates({ sessionActive: true }).solar.state).toBe('beta')
    expect(productSurfaceStates({ sessionActive: true }).ios).toEqual({ state: 'setup', label: 'Setup required' })
  })

  it('describes the browser projection without a bare "arrive in the next product wave" placeholder', () => {
    const frame = readFileSync(`${process.cwd()}/src/components/ProductSurfaceTabs.jsx`, 'utf8')
    expect(frame).toContain('Project-scoped files, conversation, and browser composition')
    expect(frame).not.toContain('arrive in the next product wave')
  })

  it('places the profile rail below the persistent header', () => {
    const css = readFileSync(`${process.cwd()}/src/site/landing.css`, 'utf8')
    expect(css).toMatch(/\.lp-topbar\s*\{[^}]*height:\s*52px;[^}]*z-index:\s*50;/s)
    expect(css).toMatch(/\.tc-product-nav\s*\{[^}]*inset:\s*52px 0 auto;[^}]*z-index:\s*34;/s)
    expect(css).toMatch(/\.tc-product-frame\s*\{[^}]*inset:\s*96px 0 0;/s)
  })
})
