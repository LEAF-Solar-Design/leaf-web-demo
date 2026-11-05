import { expect, it } from 'vitest'
import { SOLAR_FLOWS, projectSolarFlowSelect, solarFlowSelect } from './solarFlowModel.js'
import { FLOW_PANELS } from './solarWorkspacePanels.js'

const flow = SOLAR_FLOWS.find(({ id }) => id === 'solaredge-import')
const families = [{ capabilities: flow.stages.flatMap((stage) => stage.capabilities).map((name, order) => ({
  name, solar: { schema: 'leaf.solar-tool-view.v1', name, family: 'import', wave: 1, order,
    entitlement: 'run_read', interaction: { mode: 'form' } },
})) }]

it('SolarEdge has valid catalog rows and available standalone workspace panels', () => {
  expect(solarFlowSelect(families, flow.id, SOLAR_FLOWS, FLOW_PANELS).available).toBe(true)
  const catalogPositions = projectSolarFlowSelect(families, flow.id).positions.filter(({ rows }) => rows.length)
  expect(catalogPositions).toHaveLength(2)
  expect(catalogPositions.every(({ usable }) => usable)).toBe(true)
})

it('SolarEdge PDF Import is available in a project with supported tracking stages', () => {
  expect(projectSolarFlowSelect(families, flow.id).available).toBe(true)
})
