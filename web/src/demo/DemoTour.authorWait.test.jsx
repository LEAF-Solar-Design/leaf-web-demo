// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { act, cleanup, render, screen } from '@testing-library/react'

import DemoTour, { AUTHOR_WAIT_MS } from './DemoTour.jsx'

const STEPS = [
  { id: 'welcome', title: 'Welcome', body: 'w', target: null },
  { id: 'author', title: 'Author', body: 'b', target: null, prompt: 'build a tool', action: 'author' },
  { id: 'done', title: 'Done', body: 'd', target: null, action: 'exit' },
]
const TIMEOUT_TEXT = 'Staging did not finish here, so this step shows no tool. Select Next to continue the tour.'

function tour(props = {}) {
  return <DemoTour steps={STEPS} index={1} landed={false} onCannedPrompt={vi.fn()} {...props} />
}

function advance(ms) {
  act(() => { vi.advanceTimersByTime(ms) })
}

beforeEach(() => {
  vi.useFakeTimers()
  vi.stubGlobal('innerWidth', 1600)
  vi.stubGlobal('innerHeight', 1000)
})

afterEach(() => {
  cleanup()
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

it('unlocks Next when the full author wait expires', () => {
  render(tour())
  const next = screen.getByRole('button', { name: 'Next' })
  expect(next.disabled).toBe(true)
  advance(AUTHOR_WAIT_MS - 1)
  expect(next.disabled).toBe(true)
  advance(1)
  expect(next.disabled).toBe(false)
  expect(screen.getByRole('status').textContent).toBe(TIMEOUT_TEXT)
})

it('shows the timeout while busy and keeps Next disabled', () => {
  render(tour({ busy: true }))
  advance(AUTHOR_WAIT_MS)
  expect(screen.getByRole('status').textContent).toBe(TIMEOUT_TEXT)
  expect(screen.getByRole('button', { name: 'Next' }).disabled).toBe(true)
})

it('continues waiting for an unlanded run prompt', () => {
  const run = { ...STEPS[1], id: 'prompt', action: 'run' }
  render(tour({ steps: [STEPS[0], run, STEPS[2]] }))
  advance(2 * AUTHOR_WAIT_MS)
  expect(screen.getByRole('button', { name: 'Next' }).disabled).toBe(true)
  expect(screen.getByRole('status').textContent).toBe('Running it for real. Next unlocks when the result lands.')
})

it('unlocks immediately when staging lands before the timeout', () => {
  const { rerender } = render(tour())
  advance(AUTHOR_WAIT_MS - 1)
  rerender(tour({ landed: true }))
  expect(screen.getByRole('button', { name: 'Next' }).disabled).toBe(false)
  advance(1)
  expect(screen.getByRole('status').textContent).toBe('')
})
