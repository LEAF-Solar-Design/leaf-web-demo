import { cleanup, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import SolarReadResult from './SolarReadResult.jsx'

const binding = {
  kind: 'project',
  organization_id: '11111111-1111-4111-8111-111111111111',
  project_id: '22222222-2222-4222-8222-222222222222',
  drawing_id: '33333333-3333-4333-8333-333333333333',
  input_version_id: '44444444-4444-4444-8444-444444444444',
}
const previousVersion = '55555555-5555-4555-8555-555555555555'
const authorityKey = 'epoch-1'
const catalogDigest = 'sha256:' + 'c'.repeat(64)
const intakeHash = 'a'.repeat(64)
const graphHash = 'b'.repeat(64)

function show(comparison) {
  const { kind, ...scope } = binding
  const artifactId = 'd'.repeat(64)
  const artifact = {
    schema: 'leaf.solar-project-artifact-ref.v1',
    organization_id: scope.organization_id, project_id: scope.project_id, drawing_id: scope.drawing_id,
    source_version_id: scope.input_version_id, source_intake_sha256: intakeHash, graph_sha256: graphHash,
    artifact_id: artifactId, content_sha256: '9'.repeat(64), byte_length: 200, filename: 'CableLengths.xlsx',
    media_type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    download: '/api/projects/' + scope.project_id + '/drawings/' + scope.drawing_id + '/solar-artifacts/' + artifactId,
  }
  const data = {
    schema: 'leaf.solar-project-read.v1', adapter: 'project-local-graph-read', ...scope,
    input_intake_sha256: intakeHash, graph_sha256: graphHash, graph_project_id: 'solar-project',
    tool: 'solar-cable-export', tool_manifest_sha256: catalogDigest,
    head_version_id: scope.input_version_id, is_head: true, current_required: true, drawing_changed: false,
    request_sha256: 'e'.repeat(64), output_sha256: 'f'.repeat(64), output_bytes: 200,
    output: { artifact }, circuit_comparison: comparison,
  }
  const projectClient = {
    downloadArtifact: vi.fn(),
    getSnapshot: () => ({ binding, authorityKey }),
    captureCatalogScope: () => () => true,
    subscribe: () => () => {},
  }
  render(<SolarReadResult data={data} solarBinding={binding} authorityKey={authorityKey}
    projectClient={projectClient} catalogDigest={catalogDigest} />)
  expect(screen.getByRole('button', { name: 'Download CableLengths.xlsx (200 bytes)' })).toBeEnabled()
  return within(screen.getByRole('region', { name: 'Circuit changes since the previous saved version' }))
}

afterEach(cleanup)

describe('project Cable Export comparison', () => {
  it.each([
    ['36.09', '55.77', '19.68'],
    ['49.21', '36.09', '-13.12'],
    ['42.65', '42.65', '0.00'],
  ])('shows a reroute from %s ft to %s ft, a %s ft change', (before, after, delta) => {
    const comparison = show({
      status: 'ready', before_version_id: previousVersion, after_version_id: binding.input_version_id,
      changed_circuits: [{
        string_id: 'string-1', before: { label: '1', length_ft: before },
        after: { label: '1', length_ft: after }, delta_ft: delta, geometry_changed: true,
      }],
    })
    const rowHeader = comparison.getByRole('rowheader', { name: '1' })
    const cells = within(rowHeader.closest('tr')).getAllByRole('cell')
    expect(cells.map((cell) => cell.textContent)).toEqual([before, after, delta, 'Rerouted'])
  })

  it('keeps the workbook available when there is no previous saved version', () => {
    const comparison = show({ status: 'no_previous_version' })
    expect(comparison.getByText('This drawing has no previous saved version.')).toBeVisible()
    expect(comparison.queryByRole('table')).toBeNull()
  })

  it('withholds comparison rows associated with a different input version', () => {
    const comparison = show({
      status: 'ready', before_version_id: binding.input_version_id, after_version_id: previousVersion,
      changed_circuits: [{ string_id: 'stale', before: { label: 'Stale circuit' } }],
    })
    expect(comparison.getByText('Circuit comparison is unavailable for these saved versions.')).toBeVisible()
    expect(comparison.queryByRole('table')).toBeNull()
  })
})
