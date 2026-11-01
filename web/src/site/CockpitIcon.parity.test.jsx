// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import CockpitIcon, { SPRITE_URL } from './CockpitIcon.jsx'

afterEach(() => {
  cleanup()
  vi.unstubAllEnvs()
})

describe('CockpitIcon accessibility and fallback parity', () => {
  it('hides decorative sprites and preserves their lookup', () => {
    const { container } = render(<CockpitIcon id="line" label="Ignored decorative label" />)
    const icon = container.querySelector('svg')
    expect(icon).toHaveAttribute('aria-hidden', 'true')
    expect(icon).toHaveAttribute('focusable', 'false')
    expect(icon).not.toHaveAttribute('role')
    expect(icon).not.toHaveAttribute('aria-label')
    expect(screen.queryByRole('img')).toBeNull()
    expect(container.querySelector('use')).toHaveAttribute('href', `${SPRITE_URL}#i8-line`)
    expect(icon.tabIndex).not.toBe(0)
  })

  it('exposes exactly one labeled image without adding a Tab stop', () => {
    render(<CockpitIcon id="line" decorative={false} label="Stringing" />)
    const icon = screen.getByRole('img', { name: 'Stringing' })
    expect(screen.getAllByRole('img')).toHaveLength(1)
    expect(icon).toHaveAttribute('aria-label', 'Stringing')
    expect(icon).not.toHaveAttribute('aria-hidden')
    expect(icon).toHaveAttribute('focusable', 'false')
    expect(icon.tabIndex).not.toBe(0)
  })

  it('keeps a missing sprite as a hidden monogram in the same size variant', () => {
    const { container } = render(<CockpitIcon id="not-a-sprite-key" fallback="Solar Layout" size="large" />)
    const icon = container.querySelector('.ci.ci-glyph')
    expect(icon).toHaveTextContent('SL')
    expect(icon).toHaveAttribute('data-size', 'large')
    expect(icon).toHaveAttribute('aria-hidden', 'true')
    expect(icon).not.toHaveAttribute('role')
    expect(container.querySelector('svg')).toBeNull()
    expect(screen.queryByRole('img')).toBeNull()
    expect(icon.tabIndex).not.toBe(0)
  })

  it('uses the id for the monogram when fallback text is absent', () => {
    const { container } = render(<CockpitIcon id="unresolved" />)
    expect(container.querySelector('.ci-glyph')).toHaveTextContent('UN')
  })

  it('names a labeled monogram once using its label', () => {
    render(<CockpitIcon id="not-a-sprite-key" fallback="Solar Layout" decorative={false} label="Stringing" />)
    const icon = screen.getByRole('img', { name: 'Stringing' })
    expect(screen.getAllByRole('img')).toHaveLength(1)
    expect(icon).toHaveClass('ci-glyph')
    expect(icon).toHaveTextContent('SL')
    expect(icon).not.toHaveAttribute('aria-hidden')
    expect(icon.tabIndex).not.toBe(0)
  })

  it.each([
    ['small', 18],
    ['large', 39],
    ['row', 16],
    ['quick', 16],
    ['strip', 16],
  ])('preserves the %s size (%ipx) and sprite viewBox behavior', (size, pixels) => {
    const { container } = render(<CockpitIcon id="line" size={size} />)
    const icon = container.querySelector('svg')
    expect(icon).toHaveAttribute('data-size', size)
    expect(icon).toHaveAttribute('width', String(pixels))
    expect(icon).toHaveAttribute('height', String(pixels))
    // At d20fe8ef the outer SVG has no viewBox; the referenced symbol supplies it.
    expect(icon).not.toHaveAttribute('viewBox')
  })

  it.each([undefined, '', '   ', 42])('rejects an invalid non-decorative label (%s) in development', (label) => {
    vi.stubEnv('DEV', true)
    expect(() => CockpitIcon({ id: 'line', decorative: false, label }))
      .toThrow('CockpitIcon requires a non-empty label when decorative is false')
  })
})
