/**
 * The signed-out gate on /try, as a stranger meets it (pilot round 2, R2B).
 *
 * Two findings pinned here: the gate had no single primary (Sign in and
 * Notify me were accent chips while the only route to the sample rooftop,
 * Explore the demo, was quiet), and the demand form asked for a work email
 * and an automation goal before the stranger had seen the product.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'

vi.mock('../api.js', () => ({ submitDemandCapture: vi.fn() }))

import SessionGate from './SessionGate.jsx'

afterEach(cleanup)

describe('SessionGate', () => {
  it('makes Explore the demo the single filled primary when onDemo is given', () => {
    render(<SessionGate configured onSignIn={() => {}} onDemo={() => {}} />)
    const demo = screen.getByRole('button', { name: 'Explore the demo' })
    const signIn = screen.getByRole('button', { name: 'Sign in' })
    expect(demo.className).toBe('chip-act')
    expect(signIn.className).toBe('tc-bar-chip')
    const primaries = screen.getAllByRole('button').filter((b) => b.classList.contains('chip-act'))
    expect(primaries).toEqual([demo])
  })

  it('keeps Sign in filled when there is no demo to offer', () => {
    render(<SessionGate configured onSignIn={() => {}} />)
    expect(screen.getByRole('button', { name: 'Sign in' }).className).toBe('chip-act')
    expect(screen.queryByRole('button', { name: 'Explore the demo' })).toBe(null)
  })

  it('folds the demand form closed until the toggle is clicked', () => {
    render(<SessionGate configured onSignIn={() => {}} onDemo={() => {}} />)
    expect(screen.queryByLabelText('Work email')).toBe(null)
    expect(screen.queryByLabelText('What are you trying to automate?')).toBe(null)
    expect(screen.queryByRole('button', { name: 'Notify me' })).toBe(null)
    expect(screen.queryByRole('heading', { name: 'Need a plan that fits?' })).toBe(null)

    const toggle = screen.getByRole('button', { name: 'Need a plan that fits?' })
    expect(toggle.getAttribute('aria-expanded')).toBe('false')
    fireEvent.click(toggle)

    expect(toggle.getAttribute('aria-expanded')).toBe('true')
    expect(screen.getByLabelText('Work email')).toBeTruthy()
    expect(screen.getByLabelText('What are you trying to automate?')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Notify me' })).toBeTruthy()
  })
})
