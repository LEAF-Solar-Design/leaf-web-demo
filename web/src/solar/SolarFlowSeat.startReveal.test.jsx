// @vitest-environment jsdom
import React from 'react'
import { render, fireEvent, act, cleanup, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import SolarFlowSeat from './SolarFlowSeat.jsx'

let matchMediaDescriptor

function stubMatchMedia(matches = true) {
  window.matchMedia = vi.fn(() => ({
    matches, media: '(min-width: 1024px)', addEventListener() {}, removeEventListener() {},
  }))
}

beforeEach(() => {
  matchMediaDescriptor = Object.getOwnPropertyDescriptor(window, 'matchMedia')
  stubMatchMedia()
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  if (matchMediaDescriptor) Object.defineProperty(window, 'matchMedia', matchMediaDescriptor)
  else delete window.matchMedia
})

function Fixture({ present = true, cockpit = true, formRef, startRef }) {
  const seat = <SolarFlowSeat seated>
    <div data-testid="rail"><p>rail</p></div>
    {present && <section data-testid="settings-section">
      <form className="params" ref={formRef} onSubmit={(e) => e.preventDefault()}>
        <input aria-label="Name" />
        <button type="submit" data-seat-start="" ref={startRef}>Start Solar design</button>
      </form>
    </section>}
  </SolarFlowSeat>
  return cockpit ? <div className="studio-shell"><div className="workspace-card">{seat}</div></div> : seat
}

function renderSeat({ sticky = true, naturalOffset = 2300, present = true, cockpit = true } = {}) {
  let st = 0
  const formRef = (form) => {
    if (!form) return
    vi.spyOn(form, 'getBoundingClientRect').mockImplementation(() => ({
      top: 1100 - st + 504, bottom: 1100 - st + 504 + 1250,
      left: 721, right: 1055, width: 334, height: 1250,
    }))
  }
  const startRef = (start) => {
    if (!start) return
    vi.spyOn(start, 'getBoundingClientRect').mockImplementation(() => {
      const natural = naturalOffset - st + 504
      const top = sticky && 1100 - st + 504 <= 865 - 21 ? Math.min(natural, 865 - 21) : natural
      return { top, bottom: top + 21, left: 735, right: 1042, width: 307, height: 21 }
    })
  }
  const fixture = (showForm) => <Fixture present={showForm} cockpit={cockpit} formRef={formRef} startRef={startRef} />
  const view = render(fixture(present))
  const seat = screen.getByTestId('solar-flow-seat-content')
  Object.defineProperties(seat, {
    clientHeight: { configurable: true, value: 361 },
    clientTop: { configurable: true, value: 0 },
    scrollTop: { configurable: true, get: () => st, set: (value) => { st = value } },
  })
  vi.spyOn(seat, 'getBoundingClientRect').mockReturnValue({
    top: 504, bottom: 865, left: 721, right: 1055, width: 334, height: 361,
  })
  return {
    seat,
    toggle: screen.getByRole('button', { name: /Solar flow tools/ }),
    mountForm: () => view.rerender(fixture(true)),
  }
}

function expectStartInside() {
  const rect = screen.getByRole('button', { name: 'Start Solar design' }).getBoundingClientRect()
  expect(rect.top).toBeGreaterThanOrEqual(504)
  expect(rect.bottom).toBeLessThanOrEqual(865)
}

describe('SolarFlowSeat settings Start reveal', () => {
  it('brings the open settings form into the desktop seat so Start is visible without scrolling', () => {
    const { seat, toggle } = renderSeat()
    fireEvent.click(toggle)
    expect(seat.scrollTop).toBe(1100)
    expectStartInside()
  })

  it('falls back to scrolling Start itself into view when it does not stick', () => {
    const { seat, toggle } = renderSeat({ sticky: false })
    fireEvent.click(toggle)
    expect(seat.scrollTop).toBe(1960)
    expectStartInside()
  })

  it('leaves the seat alone when Start is already visible', () => {
    // An absolute top of 600 is inside the seat: 96 + the seat's top of 504.
    const { seat, toggle } = renderSeat({ naturalOffset: 96 })
    fireEvent.click(toggle)
    expect(seat.scrollTop).toBe(0)
    expectStartInside()
  })

  it('does not scroll again while the same Start stays mounted', async () => {
    const { seat, toggle } = renderSeat()
    fireEvent.click(toggle)
    expect(seat.scrollTop).toBe(1100)
    seat.scrollTop = 0
    screen.getByTestId('rail').appendChild(document.createElement('span'))
    await act(async () => {})
    await act(async () => {})
    expect(seat.scrollTop).toBe(0)
  })

  it('brings a newly mounted settings form into view', async () => {
    const { seat, toggle, mountForm } = renderSeat({ present: false })
    fireEvent.click(toggle)
    expect(seat.scrollTop).toBe(0)
    mountForm()
    await act(async () => {})
    await act(async () => {})
    expect(seat.scrollTop).toBe(1100)
    expectStartInside()
  })

  it('does nothing below 1024px or outside the studio cockpit', () => {
    stubMatchMedia(false)
    const mobile = renderSeat()
    fireEvent.click(mobile.toggle)
    expect(mobile.seat.scrollTop).toBe(0)
    cleanup()

    stubMatchMedia(true)
    const outside = renderSeat({ cockpit: false })
    fireEvent.click(outside.toggle)
    expect(outside.seat.scrollTop).toBe(0)
  })

  it('does not move focus', () => {
    const { seat, toggle } = renderSeat()
    toggle.focus()
    fireEvent.click(toggle)
    expect(seat.scrollTop).toBe(1100)
    expect(document.activeElement).toBe(toggle)
  })
})
