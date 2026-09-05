import {
  listMockCatalogTools,
  registerMockCatalogTool,
  resetMockCatalog,
} from '../src/mock/mockCatalog.js'
import { groupToolsByFamily } from '../src/mock/mockCapabilities.js'

function assert(condition, message) {
  if (!condition) throw new Error(message)
}

// Standardization slice 8c added a sixth baseline fixture (registry.json's
// mcp_source record, unmapped by name so it lands in "custom" like every
// other unmapped tool) — every count below ratchets by exactly one to match.
resetMockCatalog()
assert(listMockCatalogTools().length === 6, 'mock catalog must start with six tools')

const authored = {
  name: 'measure-all-panel-area',
  version: '1.0.0',
  description: 'Measure all panel area',
  kind: 'script',
  engine_op: 'measure_area',
  params: { type: 'object', properties: {} },
  capabilities: ['drawing.read'],
  provenance: { author: 'agent' },
}
registerMockCatalogTool(authored)
registerMockCatalogTool({ ...authored, description: 'Updated description' })

const tools = listMockCatalogTools()
assert(tools.length === 7, `mock catalog must upsert to seven tools, got ${tools.length}`)
assert(tools.find((tool) => tool.name === authored.name)?.description === 'Updated description', 'upsert must replace the existing authored tool')

const families = groupToolsByFamily(tools)
const custom = families.find((family) => family.family_id === 'custom')
assert(custom?.capabilities.length === 3, `Custom must contain three tools, got ${custom?.capabilities.length}`)

resetMockCatalog()
assert(listMockCatalogTools().length === 6, 'reset must restore the six-tool demo baseline')

console.log('MOCK_CATALOG_OK')
