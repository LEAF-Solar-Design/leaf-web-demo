// @vitest-environment jsdom
//
// Lane 3 (one-shell narrow): at phone width the tour card is a compact coach
// anchored at the bottom of the viewport, above the cockpit's command line
// and status bar, with Next and Skip always inside the viewport. The
// placement is computed from window.innerWidth and innerHeight (the old math
// assumed a 380px card plus margins, which pushed the controls off a 390px
// screen). Desktop placement is unchanged, pinned by the last block.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'

import DemoTour from './DemoTour.jsx'

const STEPS = [
  { id: 'welcome', title: 'Welcome to the cockpit', body: 'A real drawing, real tools.', target: '.app' },
  { id: 'command', title: 'Ask in plain English', body: 'Type a request in the command line.', target: '.bar' },
  { id: 'done', title: 'That is the loop', body: 'Explore freely from here.', target: null, action: 'exit' },
]

function setViewport(width, height) {
  Object.defineProperty(window, 'innerWidth', { configurable: true, writable: true, value: width })
  Object.defineProperty(window, 'innerHeight', { configurable: true, writable: true, value: height })
  vi.stubGlobal('matchMedia', vi.fn((query) => ({
    matches: /max-width: 600px/.test(query) ? width <= 600 : false,
    media: query,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  })))
}

// jsdom lays nothing out: every rect is zero. The command bar beat needs a
// real spotlight rectangle, so the target reports one from the bottom of a
// 390x844 phone (the cockpit's command line sits above the status bar).
function mountTarget(rect) {
  document.body.innerHTML = '<div class="app"><div class="bar"></div></div>'
  const bar = document.querySelector('.bar')
  bar.getBoundingClientRect = () => ({ ...rect, right: rect.left + rect.width, bottom: rect.top + rect.height, x: rect.left, y: rect.top, toJSON() {} })
  return bar
}

const px = (value) => (typeof value === 'number' ? value : parseFloat(value))

afterEach(() => {
  cleanup()
  document.body.innerHTML = ''
  vi.unstubAllGlobals()
})

describe('DemoTour at 390x844: the coach', () => {
  beforeEach(() => setViewport(390, 844))

  it('renders Next and Skip, and its computed box lies inside the viewport', () => {
    render(<DemoTour steps={STEPS} onExit={() => {}} />)
    expect(screen.getByRole('button', { name: 'Next' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Skip' })).toBeTruthy()
    const card = document.querySelector('.tour-card')
    expect(card.dataset.placement).toBe('coach')
    const { left, top, width, maxHeight } = card.style
    expect(px(left)).toBeGreaterThanOrEqual(0)
    expect(px(left) + px(width)).toBeLessThanOrEqual(390)
    expect(px(top)).toBeGreaterThanOrEqual(0)
    expect(px(top) + px(maxHeight)).toBeLessThanOrEqual(844)
    expect(card.style.transform).toBe('')
  })

  it('rises above a spotlight that sits in the bottom zone (the command bar beat)', () => {
    const bar = mountTarget({ top: 740, left: 8, width: 374, height: 40 })
    render(<DemoTour steps={STEPS} index={1} onExit={() => {}} />)
    const card = document.querySelector('.tour-card')
    const { top, maxHeight } = card.style
    expect(px(top)).toBeGreaterThanOrEqual(0)
    // The coach's box ends above the spotlight rectangle (the 6px halo
    // included), so the bar it points at stays visible and tappable.
    expect(px(top) + px(maxHeight)).toBeLessThanOrEqual(bar.getBoundingClientRect().top - 6)
    expect(px(top)).toBeLessThan(bar.getBoundingClientRect().top)
    expect(screen.getByRole('button', { name: 'Next' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Skip' })).toBeTruthy()
  })

  it('starts collapsed, expands on More, and carries no banner strip', () => {
    render(<DemoTour steps={STEPS} onExit={() => {}} />)
    expect(document.querySelector('.tour-banner')).toBeNull()
    expect(screen.queryByText(STEPS[0].body)).toBeNull()
    const toggle = screen.getByRole('button', { name: 'More' })
    expect(toggle.getAttribute('aria-expanded')).toBe('false')
    fireEvent.click(toggle)
    expect(screen.getByText(STEPS[0].body)).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Less' }).getAttribute('aria-expanded')).toBe('true')
  })

  it('Skip exits the tour', () => {
    const onExit = vi.fn()
    render(<DemoTour steps={STEPS} onExit={onExit} />)
    fireEvent.click(screen.getByRole('button', { name: 'Skip' }))
    expect(onExit).toHaveBeenCalledTimes(1)
  })
})

describe('DemoTour at 1440x900: desktop placement is unchanged', () => {
  beforeEach(() => setViewport(1440, 900))

  it('keeps the banner, the body copy, and the centered fallback with no coach toggle', () => {
    render(<DemoTour steps={STEPS} onExit={() => {}} />)
    expect(document.querySelector('.tour-banner')).not.toBeNull()
    expect(screen.getByText(STEPS[0].body)).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'More' })).toBeNull()
    const card = document.querySelector('.tour-card')
    expect(card.dataset.placement).toBeUndefined()
    expect(card.classList.contains('is-coach')).toBe(false)
    expect(card.classList.contains('is-centered')).toBe(true)
    expect(card.style.left).toBe('50%')
    expect(card.style.transform).toBe('translate(-50%, -50%)')
  })

  it('places a spotlit card below its target with the 380px width math', () => {
    mountTarget({ top: 100, left: 200, width: 300, height: 40 })
    render(<DemoTour steps={STEPS} index={1} onExit={() => {}} />)
    const card = document.querySelector('.tour-card')
    // below: rect.top + rect.height + MARGIN, with the 6px spotlight halo.
    expect(px(card.style.top)).toBe(100 - 6 + 40 + 12 + 14)
    expect(px(card.style.left)).toBe(200 - 6)
    expect(card.style.width).toBe('')
  })
})
