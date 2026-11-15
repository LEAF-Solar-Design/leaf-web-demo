import React from 'react'
import { cleanup, render, screen, within } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import SolarReadResult from './SolarReadResult.jsx'
import { ARTIFACT_UNVERIFIED, PROJECT_READ_REASONS } from './solarReadResultModel.js'
import fixture from './__fixtures__/solaredge-canonical-tracking.json'

// Actual canonical accept, later settings publication and historical read from
// the synthetic memory pipeline at the producer head recorded in the fixture.
// No live project, reference output or hand-authored accepted row is used.
const data = fixture.result
const binding = { kind: 'project', ...Object.fromEntries(
  ['organization_id', 'project_id', 'drawing_id', 'input_version_id'].map((key) => [key, data[key]])),
}

afterEach(cleanup)

function show(result = data) {
  const projectClient = { downloadArtifact: vi.fn(), getSnapshot: () => ({ binding, authorityKey: 'synthetic-session' }),
    subscribe: () => () => {}, captureCatalogScope: () => () => true }
  const view = render(<SolarReadResult data={result} solarBinding={binding} authorityKey="synthetic-session"
    projectClient={projectClient} catalogDigest={data.tool_manifest_sha256} projectCurrent={false} />)
  return { ...view, projectClient }
}
it('shows partial tracking as correction work rather than full verification', () => {
  const result = structuredClone(data)
  result.output.acceptance = { mode: 'matched-only', status: 'partial', unresolved_groups: 1 }
  result.output.one_liner = 'Matched tracking is saved. Unresolved groups still need correction. Labels are not electrical verification.'
  show(result)
  expect(screen.getByText(result.output.one_liner)).toBeTruthy()
  expect(screen.getByRole('rowheader', { name: 'Status', exact: true }).closest('tr')).toHaveTextContent('partial')
  expect(screen.getByRole('rowheader', { name: 'Unresolved groups', exact: true }).closest('tr')).toHaveTextContent('1')
})

it('shows saved historical choices, complete bridge membership and PDF provenance as labels only', () => {
  expect(data.is_head).toBe(false)
  expect(data.current_required).toBe(false)
  const { container, projectClient } = show()
  const labels = screen.getByRole('rowheader', { name: 'Labels only', exact: true }).closest('tr')
  expect(within(labels).getByRole('cell')).toHaveTextContent('yes')
  const table = within(screen.getByRole('region', { name: 'Items', exact: true })).getByRole('table')
  const rows = within(table).getAllByRole('row')
  expect(rows).toHaveLength(data.output.items.length + 1)
  const bridge = data.output.items.find((row) => row.source === 'bridge')
  const bridgeRow = within(table).getByRole('cell', { name: 'bridge', exact: true }).closest('tr')
  expect(within(bridgeRow).getByRole('cell', { name: bridge.panel_refs.join(', '), exact: true })).toBeTruthy()
  const headers = within(table).getAllByRole('columnheader').map((cell) => cell.textContent)
  const cells = within(bridgeRow).getAllByRole('cell')
  expect(cells[headers.indexOf('Pdf inverter id')]).toHaveTextContent(String(bridge.pdf_inverter_id))
  expect(cells[headers.indexOf('Pdf string input')]).toHaveTextContent(String(bridge.pdf_string_input))
  const provenance = screen.getByRole('region', { name: 'Provenance', exact: true })
  expect(provenance.textContent).toContain(data.output.provenance.source.content_sha256)
  const request = within(provenance).getByRole('rowheader', { name: 'Request', exact: true }).closest('tr')
  expect(JSON.parse(within(request).getByRole('cell').textContent).associations)
    .toEqual(data.output.provenance.request.associations)
  for (const key of ['Organization id', 'Project id', 'Graph project id']) {
    expect(screen.queryByRole('rowheader', { name: key, exact: true })).toBeNull()
  }
  expect(container.textContent).not.toContain(data.organization_id)
  expect(container.textContent).not.toContain(data.project_id)
  expect(container.textContent).not.toContain(data.graph_project_id)
  expect(screen.queryByRole('button', { name: /Download/ })).toBeNull()
  expect(projectClient.downloadArtifact).not.toHaveBeenCalled()
})

it.each(['organization_id', 'project_id', 'drawing_id', 'input_version_id'])(
  'refuses foreign saved tracking %s without displaying its rows', (key) => {
    const foreign = { ...data, [key]: '99999999-9999-4999-8999-999999999999' }
    const { projectClient } = show(foreign)
    expect(screen.getByText(PROJECT_READ_REASONS.result_unreadable)).toBeTruthy()
    expect(screen.queryByRole('region', { name: 'Items', exact: true })).toBeNull()
    expect(projectClient.downloadArtifact).not.toHaveBeenCalled()
  },
)

it('never turns nested accepted-report provenance into an implicit download grant', () => {
  const forged = { ...data, output: { ...data.output, artifact: data.output.provenance.report } }
  const { projectClient } = show(forged)
  expect(screen.getByText(ARTIFACT_UNVERIFIED)).toBeTruthy()
  expect(screen.queryByRole('button', { name: /Download/ })).toBeNull()
  expect(projectClient.downloadArtifact).not.toHaveBeenCalled()
})
