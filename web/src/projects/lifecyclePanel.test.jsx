/**
 * ProjectLifecyclePanel mount oracle, plus the ToolCast wiring that decides
 * when it mounts at all.
 *
 * Two things have to be true for the graft to be correct, and neither one
 * proves the other:
 *   1. The panel renders the lifecycle block for an open project when the flag
 *      is on, and renders NOTHING when it is off (this file, DOM assertions).
 *   2. ToolCast reaches it through the exact gate — the build flag first, then
 *      the public-demo / mock-transport / operator fences, then an open project
 *      id (this file, source assertions against ToolCast.jsx).
 *
 * ToolCast itself is not mounted here on purpose: it pulls three.js, the wasm
 * CAD harness, seven controllers and the whole mock engine, so a jsdom mount of
 * it would test the mocks, not the wiring. The wiring is a static fact about
 * the file, so it is asserted as one — the same technique
 * web/src/app-wiring.test.mjs already uses for App.jsx's bindings.
 */
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'

vi.mock('./api.js', () => ({
  createBlankProject: vi.fn(),
  cloneProject: vi.fn(),
  deleteProject: vi.fn(),
  exportProject: vi.fn(),
  getProjectLifecycle: vi.fn(),
  getStoredActorBindingId: vi.fn(() => 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'),
  inviteMember: vi.fn(),
  resetProject: vi.fn(),
  revokeMember: vi.fn(),
}))

import { getProjectLifecycle } from './api.js'
import ProjectLifecyclePanel from './ProjectLifecyclePanel.jsx'

afterEach(cleanup)

const PROJECT_ID = '11111111-2222-4333-8444-555555555555'
const VIEWER_BINDING = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'

const SNAPSHOT = {
  project: { project_id: PROJECT_ID, name: 'Rooftop Array', status: 'active', profile: 'blank_browser' },
  members: [
    { membership_id: 'm-1', binding_id: VIEWER_BINDING, role: 'owner', status: 'active', created_at: '2026-08-01T00:00:00+00:00', revoked_at: null },
    { membership_id: 'm-2', binding_id: 'bbbbbbbb-cccc-4ddd-8eee-ffffffffffff', role: 'read_only', status: 'active', created_at: '2026-08-02T00:00:00+00:00', revoked_at: null },
  ],
  files: [],
  receipts: [
    { receipt_id: 'r-1', project_id: PROJECT_ID, action: 'project_created', input_digest: 'd'.repeat(64), created_at: '2026-08-01T00:00:00+00:00' },
  ],
}

describe('lifecycle block renders for an open project with the flag on', () => {
  it('mounts membership, the timeline, the danger zone, and the clone/export affordances', async () => {
    getProjectLifecycle.mockResolvedValue(SNAPSHOT)
    render(<ProjectLifecyclePanel enabled projectId={PROJECT_ID} projectName="Rooftop Array" />)

    await waitFor(() => expect(screen.getByTestId('membership-panel')).toBeTruthy())
    expect(screen.getByTestId('projects-surface')).toBeTruthy()
    expect(getProjectLifecycle).toHaveBeenCalledTimes(1) // ONE read feeds every child
    expect(getProjectLifecycle).toHaveBeenCalledWith(PROJECT_ID)

    // Roster comes from the snapshot verbatim, with the wire's `read_only`
    // rendered in the component's own `read-only` vocabulary.
    expect(screen.getByLabelText(`Role for ${VIEWER_BINDING}`).value).toBe('owner')
    expect(screen.getByLabelText('Role for bbbbbbbb-cccc-4ddd-8eee-ffffffffffff').value).toBe('read-only')
    // The invite field collects a binding id, which is what the route takes.
    expect(screen.getByLabelText(/invite by binding id/i)).toBeTruthy()

    expect(screen.getByRole('region', { name: 'Project timeline' })).toBeTruthy()
    expect(screen.getByText('project_created')).toBeTruthy()
    expect(screen.getByRole('region', { name: 'Danger zone' })).toBeTruthy()
    expect(screen.getByRole('button', { name: /^clone project$/i })).toBeTruthy()
    expect(screen.getByRole('button', { name: /^export project$/i })).toBeTruthy()

    // Reset and delete are TERMINAL on this platform (no restore token is
    // minted), so no undo may be offered.
    expect(screen.queryByText(/undo/i)).toBeNull()
  })

  it('renders nothing at all with the flag off, and never touches the transport', async () => {
    getProjectLifecycle.mockResolvedValue(SNAPSHOT)
    const { container } = render(
      <ProjectLifecyclePanel enabled={false} projectId={PROJECT_ID} projectName="Rooftop Array" />,
    )
    expect(container).toBeEmptyDOMElement()
    expect(screen.queryByTestId('projects-surface')).toBeNull()
    expect(screen.queryByTestId('membership-panel')).toBeNull()
    expect(getProjectLifecycle).not.toHaveBeenCalled()
  })

  it('renders nothing when no project is open', () => {
    getProjectLifecycle.mockResolvedValue(SNAPSHOT)
    const { container } = render(<ProjectLifecyclePanel enabled projectId={null} />)
    expect(container).toBeEmptyDOMElement()
    expect(getProjectLifecycle).not.toHaveBeenCalled()
  })
})

describe('ToolCast mounts the lifecycle block behind the exact ratified gate', () => {
  // vitest's root is web/ (vitest.config.js), and import.meta.url is not a
  // file: URL under the jsdom transform, so the path is resolved from the root.
  const source = readFileSync(join(process.cwd(), 'src', 'site', 'ToolCast.jsx'), 'utf8')

  it('gates the panel on the build flag FIRST, then the demo/mock/operator fences and an open project', () => {
    const gate = source.match(
      /\{ENV_LIFECYCLE_UI && leftView === 'workspace'[\s\S]{0,200}?<ProjectLifecyclePanel/,
    )
    expect(gate).toBeTruthy()
    const [text] = gate
    for (const fence of ['!PUBLIC_DEMO', '!transportMock', 'canOperate', 'workspace.openProjectId']) {
      expect(text).toContain(fence)
    }
  })

  it('is the only ProjectLifecyclePanel mount, and ProjectList is gone from the tree', () => {
    expect(source.match(/<ProjectLifecyclePanel/g).length).toBe(1)
    expect(source).not.toContain('ProjectList')
  })

  it('creates projects through the idempotent blank-project factory, not /api/projects', () => {
    expect(source).toContain('createProject: createBlankProject')
    expect(source).toContain("import { createBlankProject } from '../projects/api.js'")
  })
})
