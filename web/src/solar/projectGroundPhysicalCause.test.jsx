import { expect, it } from 'vitest'
import { SOLAR_FLOWS, projectSolarFlowSelect, solarFlowSelect } from './solarFlowModel.js'
import { FLOW_PANELS } from './solarWorkspacePanels.js'

const flow = SOLAR_FLOWS.find(({ id }) => id === 'ground-physical')
const families = [{ capabilities: flow.stages.flatMap((stage) => stage.capabilities).map((name, order) => ({
  name, solar: { schema: 'leaf.solar-tool-view.v1', name, family: 'physical', wave: 1, order,
    entitlement: 'run_read', interaction: { mode: 'form' } },
})) }]

it('Ground Physical has valid catalog rows and available standalone workspace panels', () => {
  expect(solarFlowSelect(families, flow.id, SOLAR_FLOWS, FLOW_PANELS).available).toBe(true)
  const catalogPositions = projectSolarFlowSelect(families, flow.id).positions.filter(({ rows }) => rows.length)
  expect(catalogPositions).toHaveLength(2)
  expect(catalogPositions.every(({ usable }) => usable)).toBe(true)
})

it.fails('Ground Physical is available in a project — defect: project selection supplies no workspace panels', () => {
  expect(projectSolarFlowSelect(families, flow.id).available).toBe(true)
})
