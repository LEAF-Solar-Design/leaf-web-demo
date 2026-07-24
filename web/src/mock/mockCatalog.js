import registry from './registry.json' with { type: 'json' }

let tools = []

function clone(tool) {
  return structuredClone(tool)
}

export function resetMockCatalog() {
  tools = (registry.tools || []).map(clone)
}

export function listMockCatalogTools() {
  return tools.map(clone)
}

export function registerMockCatalogTool(tool) {
  if (!tool?.name) throw new Error('A named tool is required for mock publication.')
  const next = clone(tool)
  tools = [...tools.filter((candidate) => candidate.name !== next.name), next]
  return clone(next)
}

resetMockCatalog()
