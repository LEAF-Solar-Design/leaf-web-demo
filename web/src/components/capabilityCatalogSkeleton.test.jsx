// @vitest-environment jsdom
import { act, cleanup, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import CapabilityCatalog from './CapabilityCatalog.jsx'

const family = {
  family_id: 'measurement',
  label: 'Measurement',
  capabilities: [{ name: 'count_panels', description: 'Count panels', capabilities: [], kind: 'read' }],
}
const props = { openFamilies: { measurement: true }, onToggleFamily: () => {}, tools: [] }
const emptyCatalog = { families: [] }
const loadedCatalog = { source: 'endpoint', families: [family] }
const advance = (ms) => act(() => vi.advanceTimersByTime(ms))

beforeEach(() => {
  vi.useFakeTimers()
  vi.setSystemTime(0)
})

afterEach(() => {
  cleanup()
  vi.useRealTimers()
})

describe('CapabilityCatalog skeleton timing', () => {
  it('waits 200 ms, holds the skeleton for 400 ms, then shows real family and tool rows', () => {
    const { container, rerender } = render(<CapabilityCatalog {...props} catalog={emptyCatalog} />)
    expect(container.querySelector('.skeleton-stack')).toBeNull()
    advance(199)
    expect(container.querySelector('.skeleton-stack')).toBeNull()
    advance(1)
    const skeleton = screen.getByLabelText('Loading capability families')
    expect(skeleton.querySelectorAll('.skeleton-row')).toHaveLength(3)
    expect(skeleton.style.getPropertyValue('--skeleton-h')).toBe('46px')

    advance(50)
    rerender(<CapabilityCatalog {...props} catalog={loadedCatalog} />)
    expect(screen.getByLabelText('Loading capability families')).toBe(skeleton)
    expect(screen.queryByRole('button', { name: /Measurement/ })).toBeNull()
    expect(screen.queryByText('count_panels')).toBeNull()
    advance(349)
    expect(screen.getByLabelText('Loading capability families')).toBe(skeleton)
    expect(screen.queryByText('count_panels')).toBeNull()
    advance(1)
    expect(container.querySelector('.skeleton-stack')).toBeNull()
    expect(screen.getByRole('button', { name: /Measurement/ })).toBeTruthy()
    expect(screen.getByText('count_panels')).toBeTruthy()
    expect(vi.getTimerCount()).toBe(0)
  })

  it('never flashes when the catalog finishes loading under 200 ms', () => {
    const { container, rerender } = render(<CapabilityCatalog {...props} catalog={emptyCatalog} />)
    advance(199)
    rerender(<CapabilityCatalog {...props} catalog={loadedCatalog} />)
    expect(container.querySelector('.skeleton-stack')).toBeNull()
    expect(screen.getByText('count_panels')).toBeTruthy()
    advance(10000)
    expect(container.querySelector('.skeleton-stack')).toBeNull()
    expect(screen.getByRole('button', { name: /Measurement/ })).toBeTruthy()
    expect(vi.getTimerCount()).toBe(0)
  })
})
