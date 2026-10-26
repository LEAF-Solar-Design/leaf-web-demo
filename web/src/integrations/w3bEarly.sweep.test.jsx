import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, render, screen, within } from '@testing-library/react'
import { OpsWorkspaceGround } from './OpsWorkspaceGround.jsx'
import { deriveWorkspaceProjectState } from '../site/workspaceProjectState.js'

vi.mock('./IntegrationSetupPanel.jsx', () => ({ IntegrationSetupPanel: () => <section aria-label="Integration setup" /> }))
vi.mock('./ManualWorkflowRunPanel.jsx', () => ({ ManualWorkflowRunPanel: () => null }))
vi.mock('./ManualWorkflowStartPanel.jsx', () => ({ ManualWorkflowStartPanel: () => null }))
vi.mock('./ManualWorkflowDefinitionPanel.jsx', () => ({ ManualWorkflowDefinitionPanel: () => null }))
vi.mock('./ActionDefinitionPanel.jsx', () => ({ ActionDefinitionPanel: () => null }))
vi.mock('./WorkflowSchedulePreviewPanel.jsx', () => ({ WorkflowSchedulePreviewPanel: () => null }))
vi.mock('./WebhookReceiptPanel.jsx', () => ({ WebhookReceiptPanel: () => null }))
vi.mock('./WorkflowScheduleDraftBrowser.jsx', () => ({ WorkflowScheduleDraftBrowser: () => null }))

const projectId = '11111111-1111-4111-8111-111111111111'
function opsProps(projectRole = 'owner') {
  return { active: true, signedIn: true, projectId, projectLabel: 'Maple Street',
    session: { status: 'active', generation: 1 }, client: {
      getScope: vi.fn().mockResolvedValue({ projectId, projectRole }),
      listDatasets: vi.fn().mockResolvedValue({ records: [], nextCursor: null }),
      listElements: vi.fn().mockResolvedValue({ records: [], nextCursor: null }),
    } }
}
afterEach(cleanup)

it.each(['editor', 'reviewer', 'read_only'])('gives %s owner guidance when no datasets are available', async (role) => {
  render(<OpsWorkspaceGround {...opsProps(role)} />)
  const datasets = await screen.findByRole('region', { name: 'Integration datasets' })
  expect(within(datasets).getByText('No datasets are available yet. Ask the project owner to connect your data.')).toBeInTheDocument()
  expect(screen.queryByRole('region', { name: 'Integration setup' })).toBeNull()
  expect(datasets).not.toHaveTextContent('Integration setup is needed')
})

it('directs owners to the setup panel they can use', async () => {
  render(<OpsWorkspaceGround {...opsProps()} />)
  expect(await screen.findByText('No datasets are available yet. Use integration setup to connect your data.')).toBeInTheDocument()
  expect(screen.getByRole('region', { name: 'Integration setup' })).toBeInTheDocument()
})

it('names the Ops project without exposing its identifier', () => {
  render(<OpsWorkspaceGround active projectId={projectId} projectLabel="Maple Street" />)
  expect(screen.getByText('Maple Street', { exact: true })).toBeInTheDocument()
  expect(screen.getByRole('region', { name: 'Ops workspace' })).not.toHaveTextContent(projectId)
})

it('explains a missing project name without a generic Project placeholder', () => {
  render(<OpsWorkspaceGround active projectId={projectId} projectLabel="   " />)
  expect(screen.getByText('Project name unavailable')).toBeInTheDocument()
  expect(screen.getByRole('region', { name: 'Ops workspace' })).not.toHaveTextContent(projectId)
})

it('states when no project is open in the Ops header', () => {
  render(<OpsWorkspaceGround active />)
  expect(screen.getByText('No project open')).toBeInTheDocument()
})

it('gives empty-board guidance without sending users to the hidden header menu', () => {
  const state = deriveWorkspaceProjectState({})
  expect(state.explainer).toContain('Add a drawing to start working in CAD.')
  expect(state.explainer).not.toMatch(/header|Project menu/i)
})

it('explains the workspace requirement without sending users to the hidden header menu', () => {
  const state = deriveWorkspaceProjectState({ drawingName: 'Roof plan' })
  expect(state.action.disabled).toBe(true)
  expect(state.action.reason).toBe('Create a workspace first, then create a project.')
  expect(state.action.reason).not.toMatch(/header|Project menu/i)
})
