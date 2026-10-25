// @vitest-environment jsdom
import React from 'react'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import SolarFlowRail from './SolarFlowRail.jsx'
import { SOLAR_FLOW_UNAVAILABLE_REASONS } from './solarFlowModel.js'

afterEach(cleanup)

describe('SolarFlowRail demo mode', () => {
  it('explains once and offers sign-in instead of unavailable flows', () => {
    const spy = vi.fn()
    render(<SolarFlowRail families={[]} drawingId="demo-drawing" familiesDrawingId="demo-drawing" demo onSignIn={spy} />)
    expect(screen.getByTestId('solar-flow-demo').textContent).toContain('Solar design runs on your own drawings, so the demo has no Solar steps.')
    expect(screen.getByTestId('solar-flow-rail').getAttribute('data-demo')).toBe('true')
    expect(screen.queryByRole('combobox')).toBeNull()
    expect(screen.queryByText(SOLAR_FLOW_UNAVAILABLE_REASONS.rooftop_steps_missing)).toBeNull()
    expect(screen.getByTestId('solar-flow-rail').textContent).not.toMatch(/unavailable/i)
    fireEvent.click(screen.getByRole('button', { name: 'Sign in to design' }))
    expect(spy).toHaveBeenCalledTimes(1)
    expect(spy.mock.calls[0]).toEqual([])
  })

  it('shows the note without a button when no sign-in handler is passed', () => {
    render(<SolarFlowRail families={[]} drawingId="demo-drawing" familiesDrawingId="demo-drawing" demo />)
    expect(screen.getByTestId('solar-flow-demo').textContent).toContain('Solar design runs on your own drawings, so the demo has no Solar steps.')
    expect(screen.queryByRole('button')).toBeNull()
  })

  it('outside demo mode the rail keeps the unavailable picker', () => {
    render(<SolarFlowRail families={[]} drawingId="d1" familiesDrawingId="d1" />)
    expect(screen.getByRole('combobox', { name: 'Solar flow' })).toBeTruthy()
    expect(screen.getByText(SOLAR_FLOW_UNAVAILABLE_REASONS.rooftop_steps_missing)).toBeTruthy()
    expect(screen.queryByTestId('solar-flow-demo')).toBeNull()
    expect(screen.getByTestId('solar-flow-rail').hasAttribute('data-demo')).toBe(false)
  })

  it('App passes demo mode and the sign-in handler to the rail', () => {
    const source = readFileSync(resolve(process.cwd(), 'src/App.jsx'), 'utf8')
    const rail = source.indexOf('<SolarFlowRail')
    const props = source.slice(rail, source.indexOf('/>', rail))
    expect(source.split('<SolarFlowRail').length).toBe(2)
    expect(props).toContain('demo={mock}')
    expect(props).toContain('onSignIn={mock && authConfigured && !signedIn ? onLogin : undefined}')
  })
})
