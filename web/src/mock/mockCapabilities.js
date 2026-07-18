// CLIENT-SIDE families grouping for MOCK mode (zero /api calls).
//
// Live mode fetches GET /api/capabilities (server-side filtered + grouped). In
// mock we group the flat registry client-side so the left-rail catalog reads the
// same way with no backend. The family labels/order mirror the server's
// capability_families.json; anything not mapped (e.g. an authored tool) lands in
// "Custom authored tools". Internal/QA tools (qa- / _ prefixes) are filtered out
// exactly as the server does for a tenant with no QA role.

const FAMILIES = [
  { family_id: 'measurement', label: 'Measurement', description: 'Count and measure entities in the drawing.' },
  { family_id: 'selection', label: 'Selection & highlighting', description: 'Select or highlight entities matching spatial criteria.' },
  { family_id: 'custom', label: 'Custom authored tools', description: 'Tools authored in this workspace via “Author a tool”.' },
]

const NAME_TO_FAMILY = {
  'count-by-layer': 'measurement',
  'measure-panel-area': 'measurement',
  'highlight-panels-near-edge': 'selection',
  'select-by-layer': 'selection',
  'delete-marked-panel': 'custom',
}

const QA_PREFIXES = ['qa-', '_']

function isInternal(tool) {
  if (tool.internal) return true
  const name = String(tool.name || '')
  return QA_PREFIXES.some((p) => name.startsWith(p))
}

function familyFor(tool) {
  if (tool.family_id) return String(tool.family_id)
  return NAME_TO_FAMILY[tool.name] || 'custom'
}

// tools[] (flat, tool-shaped) -> [{family_id, label, description, capabilities[]}]
// dropping empty families, preserving the declared order.
export function groupToolsByFamily(tools = []) {
  const grouped = {}
  for (const tool of tools) {
    if (isInternal(tool)) continue
    const fam = familyFor(tool)
    ;(grouped[fam] = grouped[fam] || []).push(tool)
  }
  const families = []
  for (const fam of FAMILIES) {
    const caps = grouped[fam.family_id]
    if (!caps || caps.length === 0) continue
    families.push({ ...fam, capabilities: caps })
    delete grouped[fam.family_id]
  }
  // any undeclared families (defensive) after the declared ones
  for (const [family_id, caps] of Object.entries(grouped)) {
    families.push({ family_id, label: family_id, description: '', capabilities: caps })
  }
  return families
}
