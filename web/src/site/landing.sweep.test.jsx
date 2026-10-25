// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import LandingCast from './LandingCast.jsx'

const mocks = vi.hoisted(() => ({ solve: vi.fn(), navigate: vi.fn() }))
vi.mock('./intakeCache.js', () => ({ loadDemoSolve: mocks.solve }))
vi.mock('./router.js', () => ({ navigate: mocks.navigate }))
vi.mock('../api.js', () => ({ submitDemandCapture: vi.fn() }))

beforeEach(() => {
  vi.resetAllMocks()
  mocks.solve.mockReturnValue(new Promise(() => {}))
})
afterEach(cleanup)

describe('landing issue sweep', () => {
  it('shows the fixed module as plain text without dropdown styling or a caret (S1-93)', () => {
    const { container } = render(<LandingCast />)
    const module = screen.getByText('Q.Peak 425')
    expect(module.tagName).toBe('SPAN')
    expect(module.hasAttribute('tabindex')).toBe(false)
    expect(module.hasAttribute('role')).toBe(false)
    expect(container.querySelector('.lp-solver-field, .lp-solver-caret')).toBeNull()
    expect(screen.queryByText('▾')).toBeNull()
  })

  it('replaces failed progress with safe copy and retries the solve (S1-93)', async () => {
    let resolveRetry
    mocks.solve.mockRejectedValueOnce(new Error('GET /api/site/demo-solve -> 503'))
      .mockImplementationOnce(() => new Promise((resolve) => { resolveRetry = resolve }))
    render(<LandingCast />)
    expect((await screen.findByRole('alert')).textContent).toBe('Could not load the string length. Please retry.')
    expect(screen.queryByText('Solving')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(mocks.solve).toHaveBeenCalledTimes(2))
    expect(screen.queryByRole('alert')).toBeNull()
    expect(screen.getByText('Solving')).toBeTruthy()
    resolveRetry({ electrical: { max_modules_per_string: 28 } })
    expect(await screen.findByText('28')).toBeTruthy()
    expect(screen.queryByText('Solving')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Retry' })).toBeNull()
  })

  it('offers retry when a response contains no string length (S1-93)', async () => {
    mocks.solve.mockResolvedValue({ ok: false })
    render(<LandingCast />)
    await screen.findByRole('alert')
    expect(screen.getByRole('button', { name: 'Retry' })).toBeTruthy()
    expect(screen.queryByText('Solving')).toBeNull()
  })

  it('offers another retry if the retry also fails (S1-93)', async () => {
    mocks.solve.mockRejectedValue(new Error('Failed to fetch'))
    render(<LandingCast />)
    fireEvent.click(await screen.findByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(mocks.solve).toHaveBeenCalledTimes(2))
    expect((await screen.findByRole('alert')).textContent).toBe('Could not load the string length. Please retry.')
    expect(screen.getByRole('button', { name: 'Retry' })).toBeTruthy()
    expect(screen.queryByText('Solving')).toBeNull()
    expect(screen.queryByText(/Failed to fetch/)).toBeNull()
  })

  it('counts the cover and five sheets while preserving the pricing destination (S1-96)', () => {
    const { container } = render(<LandingCast />)
    const tabs = container.querySelectorAll('.lp-index .lp-tab')
    expect(tabs.length).toBe(6)
    expect(screen.getByText('Sheet 01 of 06, Cover')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: '05 Pricing' }))
    expect(mocks.navigate).toHaveBeenCalledWith('/sheets#05')
  })

  it('uses the ratified product name in the top bar (S1-211)', () => {
    const { container } = render(<LandingCast />)
    expect(container.querySelector('.lp-brandsub').textContent).toBe('/ Leaf Automation Studio')
    expect(container.textContent).not.toMatch(/Leaf Build|utility-estimation/)
  })
})
