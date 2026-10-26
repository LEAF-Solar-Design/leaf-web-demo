// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import DemoConversationPanel from './DemoConversationPanel.jsx'
import { authorMock } from '../mock/mockAuthor.js'
import LandingCast from '../site/LandingCast.jsx'
import { DeviceGround } from '../ios/DeviceGround.jsx'
import DeploysView from '../lab/deploys/DeploysView.jsx'
import LabCanvas from '../lab/canvas/LabCanvas.jsx'

vi.mock('../site/intakeCache.js', () => ({
  loadDemoSolve: vi.fn(async () => ({ ok: true, electrical: { max_modules_per_string: 24 } })),
}))
vi.mock('../api.js', () => ({
  config: { tenant: 'demo', apiBase: '' }, authHeaders: () => ({}), submitDemandCapture: vi.fn(), noteUnauthorized: vi.fn(),
}))
vi.mock('@xyflow/react', () => ({
  ReactFlowProvider: ({ children }) => children,
  ReactFlow: ({ children }) => <div>{children}</div>,
  Background: () => null, Controls: () => null,
  useReactFlow: () => ({ getNodes: () => [], screenToFlowPosition: point => point }),
}))
vi.mock('../lab/canvas/Palette.jsx', () => ({ default: () => null, BLOCK_MIME: 'application/lab-block' }))
vi.mock('../lab/canvas/Inspector.jsx', () => ({ default: () => null }))
vi.mock('../lab/canvas/NodeCard.jsx', () => ({ default: () => null }))
vi.mock('../lab/canvas/LatencyPipe.jsx', () => ({ default: () => null }))
vi.mock('../lab/canvas/dependencyLayout.js', () => ({
  createDependencyLayoutRunner: () => ({ run: async () => ({}), cancel: () => {} }),
}))

afterEach(() => { cleanup(); vi.clearAllMocks() })

const client = () => ({
  state: { head: 2, published: null, lastSaved: null },
  subscribe: () => () => {},
})
const noSchedules = async () => ({ ok: true, json: async () => ({ activations: [] }) })
const mountDeploys = (fetchImpl = noSchedules) => render(<DeploysView client={client()} fetchImpl={fetchImpl} />)

describe('demo and Lab user-facing copy', () => {
  it('offers Claude sign-in without credential jargon', () => {
    render(<DemoConversationPanel canSignIn onSignIn={vi.fn()} />)
    expect(screen.getByRole('button', { name: 'Sign in to use your Claude subscription' })).toBeInTheDocument()
    expect(document.body).not.toHaveTextContent('mounted')
  })

  it('describes a read-only authored tool without exposing its operation id', () => {
    const result = authorMock('Highlight panels near the edge')
    expect(result.preview).toContain('Created a tool from your description.')
    expect(result.preview).not.toContain(result.tool.engine_op)
    expect(result.preview).toContain('read-only')
  })

  it('describes drawing changes without exposing the write operation id', () => {
    const result = authorMock('Delete marked panel')
    expect(result.preview).toContain('Created a tool from your description.')
    expect(result.preview).not.toContain(result.tool.engine_op)
    expect(result.preview).toContain('undoable')
  })

  it('explains that landing results may come from a saved sample', async () => {
    render(<LandingCast />)
    await screen.findByText('24')
    expect(screen.getByText(/This preview can show saved sample results/)).toBeInTheDocument()
    expect(document.body).not.toHaveTextContent('live behind this page')
    expect(screen.getByText('Sample stringing results')).toBeInTheDocument()
  })

  it('points to the available pricing sheet', async () => {
    render(<LandingCast />)
    await screen.findByText('24')
    expect(screen.getByText('Free to try. See the Pricing sheet for plan details.')).toBeInTheDocument()
    expect(document.body).not.toHaveTextContent('Pricing coming soon')
  })

  it('describes device setup and build stages in plain words', () => {
    const { container } = render(<DeviceGround active ship={{ phase: 'setup-required', execution: { stage: 'GRANT_READY' } }} />)
    expect(screen.getByTestId('device-state')).toHaveTextContent('Setup needed')
    expect(container.querySelector('.device-stage')).toHaveTextContent('Apple account connected')
    expect(container).not.toHaveTextContent(/setup-required|GRANT_READY/)
  })

  it('does not print unknown provider phases or stages on the device', () => {
    const { container } = render(<DeviceGround active ship={{ phase: 'provider_unavailable', execution: { failed_stage: 'http_503' } }} />)
    expect(screen.getByTestId('device-state')).toHaveTextContent('Status unavailable')
    expect(container).not.toHaveTextContent(/provider_unavailable|http_503/)
  })

  it('shows deployment actions without an internal release runbook', async () => {
    const { container } = mountDeploys()
    await waitFor(() => expect(screen.getByText('No scheduled runs.')).toBeInTheDocument())
    expect(screen.getByRole('link', { name: 'Open revision history' })).toHaveAttribute('href', expect.stringContaining('/app/lab/history'))
    expect(container).not.toHaveTextContent(/SEL-309|\.claude|deploy-web\.py|ECS|\.yml|Tenant ledger/)
    const widget = screen.getByRole('button', { name: 'Publish widget' })
    expect(widget).toBeDisabled()
    expect(widget).toHaveAttribute('title', 'Widget publication is not available in this workspace yet.')
  })

  it('explains why an unpublished revision cannot be activated', async () => {
    mountDeploys()
    await screen.findByText('No scheduled runs.')
    expect(screen.getByRole('button', { name: 'Activate published revision' })).toBeDisabled()
    expect(screen.getByText('Publish a saved revision from History before activating it.')).toBeInTheDocument()
  })

  it('humanizes schedule failures and retries with a read request', async () => {
    const fetchImpl = vi.fn().mockRejectedValueOnce(new Error('GET /api/lab/activations -> 503'))
      .mockImplementation(noSchedules)
    mountDeploys(fetchImpl)
    expect(await screen.findByRole('alert')).toHaveTextContent('The service had a temporary problem. Try again.')
    const retry = screen.getByRole('button', { name: 'Reload schedules' })
    retry.focus()
    fireEvent.click(retry)
    await waitFor(() => expect(screen.queryByRole('alert')).not.toBeInTheDocument())
    expect(fetchImpl).toHaveBeenCalledTimes(2)
    expect(fetchImpl.mock.calls[1][1].method).toBe('GET')
    expect(retry).toHaveFocus()
  })

  it('keeps canvas readiness visible without raw benchmark timings', async () => {
    const { container } = render(<LabCanvas />)
    await screen.findByText('Layout updated')
    expect(container.querySelector('footer')).not.toHaveTextContent(/\bms\b|not measured|Layout \d|Validation \d/)
    expect(container.querySelector('footer')).toHaveTextContent(/Ready|Review the blocks and connections before continuing/)
  })
})
