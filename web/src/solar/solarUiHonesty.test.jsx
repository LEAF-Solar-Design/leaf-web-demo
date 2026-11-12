import React from 'react'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import SolarFlowRail from './SolarFlowRail.jsx'
import SolarPhysicalReadPanel from './SolarPhysicalReadPanel.jsx'
import SolarSettingsForm from './SolarSettingsForm.jsx'
import { PROJECT_SOLAR_ROOFTOP_STAGES } from './solarFlowModel.js'
import { SEED_SETTINGS } from './solarSettingsModel.js'
import { TERRAIN_ROUTE_REASONS } from './solarTerrainClient.js'

afterEach(cleanup)

const binding = { kind: 'project', organization_id: '11111111-1111-1111-1111-111111111111',
  project_id: '22222222-2222-2222-2222-222222222222', drawing_id: '33333333-3333-3333-3333-333333333333',
  input_version_id: '44444444-4444-4444-4444-444444444444' }
const authorityKey = 'honesty-scope'
const digest = `sha256:${'d'.repeat(64)}`
const availability = { admission_checked: true, admission: { status_code: 200, error: null },
  entitled: true, engine_ready: true, implemented: true, input_ready: true, runnable: true, refusal_reasons: [] }
const families = [{ capabilities: PROJECT_SOLAR_ROOFTOP_STAGES.flatMap((stage) => stage.capabilities)
  .map((name, order) => ({ name, catalog_digest: digest, availability,
    solar: { schema: 'leaf.solar-tool-view.v1', name, family: 'solar', wave: 1, order,
      entitlement: 'run_write', interaction: { mode: 'form' } } })) }]

it.fails('every project rail step has a human label — defect: multi-tool stages fall back to raw solar-* ids', () => {
  const projectClient = { getSnapshot: () => ({ binding, authorityKey }) }
  const view = render(<SolarFlowRail solarBinding={binding} authorityKey={authorityKey} projectClient={projectClient}
    projectCatalog={{ client: projectClient, binding, authorityKey, families, isCurrent: () => true }}
    onOpenStep={() => {}} />)
  const labels = [...view.container.querySelectorAll('.solar-flow-label')].map((node) => node.textContent)
  expect(labels).toHaveLength(families[0].capabilities.length)
  expect(labels.filter((label) => /^solar-/.test(label))).toEqual([])
})

it.fails('terrain 409 names the missing import — defect: Physical reads replaces the cause with a read failure', async () => {
  const terrainClient = { getTerrain: vi.fn().mockResolvedValue({ ok: false, status: 409,
    code: 'TERRAIN_STATE_NOT_FOUND', retryable: false }) }
  render(<SolarPhysicalReadPanel drawingId="solar" projectId="p" drawingVersion={1} terrainClient={terrainClient} />)
  const alert = await screen.findByRole('alert')
  expect(terrainClient.getTerrain).toHaveBeenCalledTimes(1)
  expect(alert.textContent).toContain(TERRAIN_ROUTE_REASONS.TERRAIN_STATE_NOT_FOUND)
  expect(alert.textContent).not.toContain('could not be read')
})

// solar-local-graph-commit.spec.mjs:20-23 documents the old ParamForm gap.
// The dedicated project settings form now composes expected_rev and changes;
// keep this as a passing test rather than an expected failure of working behavior.
it('project settings can be submitted through the form with a revision and changes', async () => {
  const projectClient = {
    getSnapshot: () => ({ binding, authorityKey }), subscribe: () => () => {},
    captureCatalogScope: () => () => true,
    readContext: vi.fn().mockResolvedValue({ ok: true, data: {
      context: { ...binding, head_version_id: binding.input_version_id, is_head: true, intake_sha256: 'a'.repeat(64) },
      intake: { solar_design_graph: { rev: 6, settings: { ...SEED_SETTINGS },
        project: { name: 'Field', zip_code: '', latitude: null, longitude: null } },
      solar_design_graph_sha256: 'b'.repeat(64) },
    } }),
    prepareRun: (request) => ({ ...request, binding, authorityKey }),
    catalog: vi.fn().mockResolvedValue({ ok: true, data: { families } }),
  }
  const onProjectSubmit = vi.fn().mockResolvedValue({ ok: true, status: 202,
    data: { job_id: '55555555-5555-5555-5555-555555555555', status: 'submitted' } })
  render(<SolarSettingsForm solarBinding={binding} authorityKey={authorityKey} projectClient={projectClient}
    catalogDigest={digest} newProjectIntentKey={() => 'settings-intent'} onProjectSubmit={onProjectSubmit} />)
  fireEvent.change(await screen.findByRole('textbox', { name: 'MPPT count' }), { target: { value: '4' } })
  const apply = screen.getByRole('button', { name: 'Apply settings' })
  await waitFor(() => expect(apply.disabled).toBe(false))
  fireEvent.click(apply)
  await waitFor(() => expect(onProjectSubmit).toHaveBeenCalledTimes(1))
  expect(onProjectSubmit.mock.calls[0][0]).toMatchObject({ tool: 'solar-settings',
    params: { expected_rev: 6, changes: { num_mppt: 4 } }, binding, authorityKey })
})
