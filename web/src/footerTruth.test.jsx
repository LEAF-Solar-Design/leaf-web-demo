// @vitest-environment jsdom
import { readFileSync } from 'node:fs'
import { parse } from '@babel/parser'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import DetailsDrawer, { ServiceStatus } from './components/DetailsDrawer.jsx'
import { createPlatformTrustController } from './controllers/platform/createPlatformTrustController.js'
import { humanizeError } from './errorHumanize.js'

const source = readFileSync(`${process.cwd()}/src/App.jsx`, 'utf8')
const tree = parse(source, { sourceType: 'module', plugins: ['jsx'] })
function callback(name, env) {
  let found
  function visit(node) {
    if (!node || typeof node !== 'object') return
    if (node.type === 'VariableDeclarator' && node.id.name === name) found = node.init.arguments[0]
    for (const value of Object.values(node)) {
      if (Array.isArray(value)) value.forEach(visit)
      else if (value && typeof value === 'object') visit(value)
    }
  }
  visit(tree)
  if (!found) throw new Error(`Missing production callback: ${name}`)
  return new Function(...Object.keys(env), `return (${source.slice(found.start, found.end)})`)(...Object.values(env))
}

const clipboardDescriptor = Object.getOwnPropertyDescriptor(navigator, 'clipboard')
afterEach(() => {
  cleanup()
  if (clipboardDescriptor) Object.defineProperty(navigator, 'clipboard', clipboardDescriptor)
  else delete navigator.clipboard
})

function runDetails() {
  let data
  callback('openRunDetails', {
    result: { tool: 'Example', version: 1 }, currentJobId: 'job-123', selectedTool: null,
    cadTimingRows: () => [], humanizeError, setDrawer: value => { data = value },
  })()
  return data
}

function sessionDetails(overrides = {}) {
  return callback('sessionDetails', {
    drawer: { urlKey: 'details' }, org: '', tenantLabel: 'Personal', tierDisplay: 'Starter',
    mock: false, health: null, healthError: null, healthLoading: false, usageLoading: false,
    usage: null, knownGuest: true, isSignedIn: () => false,
    sessionActions: {}, loadUsage: vi.fn(), loadHealth: vi.fn(),
    composeDiagnostics: value => JSON.stringify(value), __BUILD_HASH__: 'test',
    taskRevisionOf: () => null, checkout: {}, lock: {}, heldByUs: false, otherHeldCheckout: false,
    recentRequestFailures: () => [], collectRefusals: () => [],
    ...overrides,
  })()
}

it('renders a failed health read as unreachable without a live claim', async () => {
  const controller = createPlatformTrustController({ services: {
    getHealth: vi.fn().mockRejectedValue(new Error('Failed to fetch')),
  } })
  await controller.loadHealth()
  const { container } = render(<ServiceStatus {...controller.getSnapshot()} />)
  expect(screen.getByText('Service unreachable').className).toBe('warn-txt')
  expect(container.textContent).not.toMatch(/live|ready|Failed to fetch/)
})

it('keeps unanswered health neutral and turns green only after a live response', () => {
  const { rerender, container } = render(<ServiceStatus health={null} />)
  expect(screen.getByText('Checking service...').className).toBe('dim')
  expect(container.textContent).not.toMatch(/live|ready/)
  rerender(<ServiceStatus health={{ aps_live: true }} />)
  expect(screen.getByText('Service live').className).toBe('ok-txt')
})

it('shows Copy failed when the real run action cannot write to the clipboard', async () => {
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: {
    writeText: vi.fn().mockRejectedValue(new Error('denied')),
  } })
  render(<DetailsDrawer data={runDetails()} />)
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Copy job id' })) })
  expect(screen.getByRole('button', { name: 'Copy failed' })).toBeTruthy()
})

it('shows Copied after the real run action succeeds', async () => {
  const writeText = vi.fn().mockResolvedValue(undefined)
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } })
  render(<DetailsDrawer data={runDetails()} />)
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Copy job id' })) })
  expect(writeText).toHaveBeenCalledWith('job-123')
  expect(screen.getByRole('button', { name: 'Copied' })).toBeTruthy()
})

it('keeps keyboard and layout jargon out of job and run feet', async () => {
  let jobData
  await callback('onSelectJob', {
    getJob: async () => ({ status: 'complete', tool: 'Example' }), recordToEnvelope: () => ({}),
    cadTimingRows: () => [], humanizeError, setDrawer: value => { jobData = value }, setRunErr: vi.fn(),
  })({ status: 'complete', job_id: 'job-123' })
  expect(jobData.foot).not.toMatch(/never re-flows|Esc|Provenance/)
  expect(runDetails().foot).not.toMatch(/Esc|Provenance/)
})

it('updates open session rows after refresh and disables the pending action', () => {
  const { rerender } = render(<DetailsDrawer data={sessionDetails({ healthLoading: true })} />)
  expect(screen.getByRole('button', { name: 'Refreshing...' }).disabled).toBe(true)
  rerender(<DetailsDrawer data={sessionDetails({ health: { aps_live: true }, usage: { today: { runs: 2, usd_est: 1 } } })} />)
  expect(screen.getByText('Service connected')).toBeTruthy()
  expect(screen.getByText('Usage: $1.000 today · 2 runs')).toBeTruthy()
  expect(screen.getByRole('button', { name: 'Refresh' }).disabled).toBe(false)
})

it('offers no refresh in a demo session', () => {
  render(<DetailsDrawer data={sessionDetails({ mock: true })} />)
  expect(screen.getByText('Demo (sample data)')).toBeTruthy()
  expect(screen.queryByRole('button', { name: 'Refresh' })).toBeNull()
})
