import glossary from '../contract/sources/leaf-glossary.v1.json'
import { manifests } from './registry.js'

export const LANES = Object.freeze([
  ['processing', 'Processing'], ['ordering', 'Ordering'], ['cashout', 'Cash out'],
  ['engines', 'Strategies'], ['sizing', 'Sizing'], ['admission', 'Admission'],
  ['observation', 'Observation'], ['capture', 'Capture'], ['lifecycle', 'Lifecycle'],
].map(([id, title]) => Object.freeze({ id, title: glossary.lanes.find(lane => lane.id === id)?.title.split(' (')[0] || title })))

// Families follow the fixture IDs. Calibration and variance have two variants each.
const familyId = id => id.startsWith('adjust-') ? 'adjust' : id.startsWith('variance-') ? 'variance' : id
export const ELEMENT_FAMILIES = Object.freeze([...new Set(manifests.map(m => familyId(m.id)))].map(id =>
  Object.freeze({ id, variants: Object.freeze(manifests.filter(m => familyId(m.id) === id).map(m => m.id)) })))
export const strategySection = manifest => manifest.lane === 'engines'
  ? (manifest.id.startsWith('adjust-') ? 'Rules' : 'Signals') : null
export const laneLabel = id => LANES.find(lane => lane.id === id)?.title || id
export const blockLabel = manifest => manifest.title

// Original implementation; no Activepieces code, ee files or React Flow Pro assets copied.
// Design reference only: activepieces/activepieces, LICENSE (MIT), packages/react-ui/.
export const DESIGN_REFERENCE = Object.freeze({
  repository: 'activepieces/activepieces', path: 'packages/react-ui/', license: 'MIT', licensePath: 'LICENSE',
})
