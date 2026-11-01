// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import NavRail from './NavRail.jsx'

const family = {
  family_id: 'measurement',
  label: 'Measurement',
  capabilities: [{ name: 'count_panels', description: 'Count panels', capabilities: [], kind: 'read' }],
}
const props = { activeSurface: 'browser', openFamilies: { measurement: true }, authorContent: null }
const loaded = { catalogFamilyCount: 1, railFamilies: [family], catalogSource: 'endpoint' }
const advance = (ms) => act(() => vi.advanceTimersByTime(ms))

beforeEach(() => {
  vi.useFakeTimers()
  vi.setSystemTime(0)
})

afterEach(() => {
  cleanup()
  vi.useRealTimers()
})

describe('NavRail skeleton timing', () => {
  it('waits 200 ms, holds the skeleton for 400 ms, then shows real family and tool rows', () => {
    const { container, rerender } = render(<NavRail {...props} />)
    expect(container.querySelector('.skeleton-stack')).toBeNull()
    expect(screen.queryByRole('status')).toBeNull()
    advance(199)
    expect(container.querySelector('.skeleton-stack')).toBeNull()
    expect(screen.queryByRole('status')).toBeNull()
    advance(1)
    const skeleton = container.querySelector('.skeleton-stack')
    expect(skeleton).not.toBeNull()
    expect(screen.getAllByRole('status')).toHaveLength(1)
    expect(screen.getByRole('status')).toBe(skeleton)
    expect(skeleton.textContent).toBe('Loading tools')
    expect(skeleton.querySelectorAll('.skeleton-row')).toHaveLength(3)
    expect(skeleton.style.getPropertyValue('--skeleton-h')).toBe('46px')

    advance(50)
    rerender(<NavRail {...props} {...loaded} />)
    expect(container.querySelector('.skeleton-stack')).toBe(skeleton)
    expect(screen.queryByRole('button', { name: /Measurement/ })).toBeNull()
    expect(screen.queryByText('count_panels')).toBeNull()
    advance(349)
    expect(container.querySelector('.skeleton-stack')).toBe(skeleton)
    expect(screen.queryByText('count_panels')).toBeNull()
    advance(1)
    expect(container.querySelector('.skeleton-stack')).toBeNull()
    expect(screen.getByRole('button', { name: /Measurement/ })).toBeTruthy()
    expect(screen.getByText('count_panels')).toBeTruthy()
    expect(vi.getTimerCount()).toBe(0)
  })

  it('never flashes for a fast load or an empty surface filter on a loaded catalog', () => {
    const { container, rerender } = render(<NavRail {...props} />)
    advance(199)
    rerender(<NavRail {...props} {...loaded} />)
    expect(container.querySelector('.skeleton-stack')).toBeNull()
    expect(screen.getByText('count_panels')).toBeTruthy()
    advance(10000)
    expect(container.querySelector('.skeleton-stack')).toBeNull()
    rerender(<NavRail {...props} {...loaded} railFamilies={[]} />)
    advance(10000)
    expect(container.querySelector('.skeleton-stack')).toBeNull()
    expect(screen.queryByRole('button', { name: /Measurement/ })).toBeNull()
    expect(vi.getTimerCount()).toBe(0)
  })
})

describe('NavRail catalog states', () => {
  it('shows a loaded empty catalog without starting a loading phase', () => {
    render(<NavRail {...props} catalogSource="endpoint" />)
    expect(screen.getByText('No tools yet. Add a tool to get started.')).toBeTruthy()
    advance(10000)
    expect(screen.queryByRole('status')).toBeNull()
    expect(screen.queryByRole('alert')).toBeNull()
    expect(vi.getTimerCount()).toBe(0)
  })

  it('shows the surface-specific no-match message without a clear filter action', () => {
    // Option A: design-parity-forks-20261101/receipts/operator-forks-all-a-20261101.json
    render(<NavRail {...props} {...loaded} railFamilies={[]} />)
    expect(screen.getByText('No tools for this surface yet.')).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Clear filter' })).toBeNull()
    expect(screen.queryByText('No tools yet. Add a tool to get started.')).toBeNull()
    advance(10000)
    expect(screen.queryByRole('status')).toBeNull()
    expect(screen.queryByRole('alert')).toBeNull()
    expect(vi.getTimerCount()).toBe(0)
  })

  it('replaces loading with an alert, retries only the catalog, and keeps fallback tools reachable', () => {
    const onRetryCatalog = vi.fn()
    const onRetryTools = vi.fn()
    const onCatalogChanged = vi.fn()
    const onToggleTools = vi.fn()
    const failure = {
      ...loaded,
      catalogErr: 'Network unavailable',
      catalogSource: 'flat-fallback',
      tools: family.capabilities,
      retryTarget: 'catalog',
      onRetryCatalog,
      onRetryTools,
      onCatalogChanged,
      onToggleTools,
    }
    const { container, rerender } = render(<NavRail {...props} />)
    advance(200)
    expect(screen.getByRole('status')).toBeTruthy()
    rerender(<NavRail {...props} {...failure} />)
    expect(screen.getByRole('alert').textContent).toBe('The tool list did not load. Your saved tools are still available below.Retry')
    expect(screen.queryByRole('status')).toBeNull()
    expect(screen.queryByRole('button', { name: /Measurement/ })).toBeNull()
    const key = container.querySelector('.key')
    expect(key.textContent).toBe('R')
    expect(key.getAttribute('aria-hidden')).toBe('true')
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    expect(onRetryCatalog).toHaveBeenCalledTimes(1)
    expect(onRetryTools).not.toHaveBeenCalled()
    expect(onCatalogChanged).not.toHaveBeenCalled()
    expect(screen.queryByText('count_panels')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: /Tools · 1/ }))
    expect(onToggleTools).toHaveBeenCalledTimes(1)
    rerender(<NavRail {...props} {...failure} toolsOpen />)
    expect(screen.getByText('count_panels')).toBeTruthy()
    expect(screen.getByRole('alert')).toBeTruthy()
    expect(screen.queryByRole('status')).toBeNull()
  })

  it('does not announce a catalog failure when signed out', () => {
    render(<NavRail {...props} catalogErr="Signed out" signedOut />)
    advance(10000)
    expect(screen.queryByRole('alert')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Retry' })).toBeNull()
    expect(screen.queryByRole('status')).toBeNull()
  })

  it('uses full words in the rendered catalog title, rail text, and aria labels', () => {
    const { container, rerender } = render(<NavRail {...props} {...loaded} catalogSource="flat-fallback" capCount={7} />)
    const assertNoShorthand = () => {
      const rail = container.querySelector('aside.nav')
      expect(rail.textContent).not.toMatch(/\b(caps|flat)\b/i)
      rail.querySelectorAll('[aria-label]').forEach((node) => {
        expect(node.getAttribute('aria-label')).not.toMatch(/\b(caps|flat)\b/i)
      })
    }
    expect(container.querySelector('.fam-title').textContent).toContain('7 tools · tool list')
    assertNoShorthand()
    rerender(<NavRail {...props} {...loaded} studio={{}} catalogSource="flat-fallback" capCount={7} />)
    expect(container.querySelector('.fam-title').textContent).toContain('1 tools · tool list')
    assertNoShorthand()
  })
})
