// @vitest-environment jsdom
import { act, cleanup, render, screen } from '@testing-library/react'
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
    advance(199)
    expect(container.querySelector('.skeleton-stack')).toBeNull()
    advance(1)
    const skeleton = container.querySelector('.skeleton-stack')
    expect(skeleton).not.toBeNull()
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
