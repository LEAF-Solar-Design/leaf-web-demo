// CLIENT-SIDE STUB for the §12 nl-prompt contract:
//   POST /api/nl-prompt {text} -> {lane, tool, params, confidence, rationale}
//
// This is a heuristic matcher over the CURRENT tool registry — it is NOT the
// real server router. It exists so the prompt-first surface is fully functional
// in VITE_MOCK with ZERO /api calls, and so a live drive still works if the
// sibling lane's /api/nl-prompt endpoint has not been deployed yet (api.js falls
// back to this and flags stub:true). The real router replaces it in live mode.
//
// Lanes (mirroring the design's Run/Solve/Build hit-indicators):
//   run   -> execute a catalog capability (returns tool + params)
//   solve -> routes a solve request to a solver tool in live mode; the offline mock returns no tool
//   build -> author a NEW capability (routes to the author flow)
// Confidence >= 0.7 => a confident prefill; < 0.7 => best-guess + alternatives.

function firstMatch(re, text) {
  const m = text.match(re)
  return m ? m[1] : null
}

export function parseLayer(text) {
  const raw = String(text || '').trim().replace(/[.!?]+$/, '')
  return firstMatch(/\blayer\s+["']([^"']+)["']$/i, raw) ||
    firstMatch(/\b(?:on|in)\s+(?:the\s+)?["']?([^"']+?)["']?\s+layer$/i, raw) ||
    firstMatch(/\blayer\s+([a-z0-9 _-]+)$/i, raw)
}

function alternatives(tools, exclude) {
  return tools
    .filter((t) => !exclude.includes(t.name))
    .slice(0, 3)
    .map((t) => ({ tool: t.name, description: t.description }))
}

export const MIN_RUN_MATCH_CONF = 0.55
const NO_DEMO_MATCH = 'No matching tool in this demo. Try another description or browse available tools.'

function runMatch(tool, params, confidence, rationale, tools, extra = {}) {
  if (confidence < MIN_RUN_MATCH_CONF) {
    return { lane: 'run', tool: null, params: {}, confidence, stubKind: 'demo', rationale: NO_DEMO_MATCH, alternatives: alternatives(tools, []) }
  }
  return { lane: 'run', tool, params, confidence, rationale, alternatives: alternatives(tools, [tool]), ...extra }
}

export function matchPrompt(text, tools = []) {
  const raw = text || ''
  const t = raw.toLowerCase().trim()
  const has = (...ws) => ws.some((w) => t.includes(w))
  const find = (name) => tools.find((x) => x.name === name)

  // Commands are resolved by the command bar, not by prose keyword matching.
  if (t.startsWith('/')) return runMatch(null, {}, 0, NO_DEMO_MATCH, tools)

  // ---- BUILD lane: author a new capability --------------------------------
  const buildVerb = /^(build|author|make|create|write|generate)\b/.test(t)
  if (has('build a tool', 'author a tool', 'make a tool', 'create a tool', 'new tool', 'build me a tool') ||
      (buildVerb && has('tool', 'capability', 'detector', 'flag ', 'flags ', 'checker', 'check for'))) {
    return {
      lane: 'build', tool: null, params: {}, confidence: 0.86, alternatives: [],
      rationale: 'Reads as a NEW capability request, routing to the author flow with your description prefilled.',
    }
  }

  // ---- SOLVE lane: cloud solver (string / combiner / optimal route) --------
  if (has('solve', 'combiner', 'string siz', 'size string', 'size strings', 'mppt', 'homerun',
          'optimal route', 'milp', 'annual shade', 'iv curve', 'inverter')) {
    return {
      lane: 'solve', tool: null, params: {}, confidence: 0.78, alternatives: [],
      rationale: 'This is a solver job (string / combiner / route). Pick a solver to run it.',
    }
  }

  // ---- RUN lane: match a catalog capability -------------------------------
  // delete / remove a panel -> versioned write tool
  if (has('delete', 'remove') && has('panel', 'module')) {
    const handle = firstMatch(/\b(?:panel|module|handle)\s*#?\s*([0-9a-f]+)\b(?![-\w])/i, raw)
    return {
      lane: 'run', tool: 'delete-marked-panel', write: true,
      params: handle ? { handle: String(handle) } : {},
      confidence: 0.82,
      rationale: handle
        ? 'Delete the specified panel. You can undo this change.'
        : 'Select a panel before deleting it. You can undo this change.',
      alternatives: alternatives(tools, ['delete-marked-panel']),
    }
  }
  // Spatial constraints must not fall through to an unconstrained count.
  if (has('edge', 'setback', 'within', 'near', 'border', 'perimeter')) {
    if (!find('highlight-panels-near-edge')) {
      return {
        lane: 'build', tool: null, params: {}, confidence: 0.8, alternatives: [],
        rationale: 'This distance limit needs a new tool. Describe which panels to include.',
      }
    }
    const inches = firstMatch(/(\d+(?:\.\d+)?)\s*(?:in\b|inch|inches|")/i, raw)
    return runMatch('highlight-panels-near-edge',
      inches ? { distance_in: Number(inches) } : {}, 0.8,
      inches ? `Highlight panels within ${inches} in of the roof edge.` : 'Highlight panels near the roof edge.', tools)
  }
  // count per layer
  if (has('count', 'how many', 'tally', 'number of', 'per layer') && find('count-by-layer')) {
    return runMatch('count-by-layer', {}, 0.9, 'Count entities on every layer: a safe read-only pass.', tools)
  }
  // area / measure
  if (has('area', 'sqft', 'sq ft', 'square', 'coverage', 'measure', 'size of') && find('measure-panel-area')) {
    return runMatch('measure-panel-area', {}, 0.85, 'Measure panel areas and totals; the largest panel is highlighted.', tools)
  }
  // select by layer
  if (has('select', 'show', 'highlight') && has('layer') && find('select-by-layer')) {
    const layer = parseLayer(raw)
    return runMatch('select-by-layer',
      layer ? { layer: layer.trim() } : {}, 0.78,
      `Select every entity on the ${layer ? layer.trim() + ' ' : ''}layer.`, tools)
  }

  // No rule matched, so there is no candidate to authorize.
  return runMatch(null, {}, 0, NO_DEMO_MATCH, tools)
}
