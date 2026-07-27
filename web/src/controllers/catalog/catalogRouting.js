const WRITE_CAPABILITY = 'drawing.write'

export function entitlementAllows(entitlements, key) {
  const policy = entitlements && entitlements.entitlements
  if (!policy || typeof policy[key] === 'undefined' || policy[key] === null) return true
  return policy[key] !== false
}
export function isWriteTool(tool) {
  return (tool?.capabilities || []).includes(WRITE_CAPABILITY)
}

export function runnableCatalogTools(tools, entitlements) {
  const canRunWrite = entitlementAllows(entitlements, 'run_write')
  return (tools || []).filter((tool) => canRunWrite || !isWriteTool(tool))
}

export function previewLane(prompt, tools, previewRoute) {
  const text = String(prompt || '').trim()
  if (!text) return null
  if (text.startsWith('/')) return 'run'
  try {
    return previewRoute(text, tools || []).lane
  } catch {
    return null
  }
}

function commonPrefixLength(a, b) {
  let length = 0
  while (length < a.length && length < b.length && a[length] === b[length]) length += 1
  return length
}

export function slashDecision(text, tools) {
  const value = String(text || '').trim()
  if (!value.startsWith('/')) return { handled: false, decision: null }

  const name = value.slice(1).split(/\s+/)[0]
  if (!name) return { handled: true, decision: null }

  const catalog = tools || []
  const tool = catalog.find((candidate) =>
    String(candidate?.name || '').toLowerCase() === name.toLowerCase())

  if (tool) {
    return {
      handled: true,
      decision: {
        lane: 'run',
        tool: tool.name,
        params: {},
        confidence: 1,
        rationale: `Explicit /${tool.name} — you picked this tool.`,
        alternatives: [],
        slash: true,
      },
    }
  }

  const query = name.toLowerCase()
  const alternatives = catalog
    .map((candidate) => {
      const candidateName = String(candidate?.name || '').toLowerCase()
      const score = candidateName.includes(query)
        ? 1000 + query.length
        : commonPrefixLength(candidateName, query)
      return { candidate, score }
    })
    .filter(({ score }) => score >= 3)
    .sort((left, right) => right.score - left.score)
    .slice(0, 3)
    .map(({ candidate }) => ({ tool: candidate.name, description: candidate.description }))

  return {
    handled: true,
    decision: {
      lane: 'run',
      tool: name,
      params: {},
      confidence: 0,
      alternatives,
      slash: true,
    },
  }
}

export function alternativeDecision(previous, name) {
  return {
    lane: 'run',
    tool: name,
    params: {},
    confidence: 0.99,
    rationale: 'You picked this capability from the alternatives.',
    alternatives: (previous?.alternatives || []).filter((item) => item.tool !== name),
    stub: previous?.stub,
    stubReason: previous?.stubReason,
  }
}
