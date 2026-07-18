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
//   solve -> a cloud-solver job (string/combiner/route) — not wired in this demo
//   build -> author a NEW capability (routes to the author flow)
// Confidence >= 0.7 => a confident prefill; < 0.7 => best-guess + alternatives.

function firstMatch(re, text) {
  const m = text.match(re)
  return m ? m[1] : null
}

function alternatives(tools, exclude) {
  return tools
    .filter((t) => !exclude.includes(t.name))
    .slice(0, 3)
    .map((t) => ({ tool: t.name, description: t.description }))
}

function runMatch(tool, params, confidence, rationale, tools, extra = {}) {
  return { lane: 'run', tool, params, confidence, rationale, alternatives: alternatives(tools, [tool]), ...extra }
}

export function matchPrompt(text, tools = []) {
  const raw = text || ''
  const t = raw.toLowerCase().trim()
  const has = (...ws) => ws.some((w) => t.includes(w))
  const find = (name) => tools.find((x) => x.name === name)

  // ---- BUILD lane: author a new capability --------------------------------
  const buildVerb = /^(build|author|make|create|write|generate)\b/.test(t)
  if (has('build a tool', 'author a tool', 'make a tool', 'create a tool', 'new tool', 'build me a tool') ||
      (buildVerb && has('tool', 'capability', 'detector', 'flag ', 'flags ', 'checker', 'check for'))) {
    return {
      lane: 'build', tool: null, params: {}, confidence: 0.86, alternatives: [],
      rationale: 'Reads as a NEW capability request — routing to the author flow with your description prefilled.',
    }
  }

  // ---- SOLVE lane: cloud solver (string / combiner / optimal route) --------
  if (has('solve', 'combiner', 'string siz', 'size string', 'size strings', 'mppt', 'homerun',
          'optimal route', 'milp', 'annual shade', 'iv curve', 'inverter')) {
    return {
      lane: 'solve', tool: null, params: {}, confidence: 0.78, alternatives: [],
      rationale: 'This is a cloud-solver job (string / combiner / route). The solve lane is not wired into this demo yet.',
    }
  }

  // ---- RUN lane: match a catalog capability -------------------------------
  // delete / remove a panel -> versioned write tool
  if (has('delete', 'remove') && has('panel', 'module')) {
    const handle = firstMatch(/\b(?:panel|module|handle)\s*#?\s*([0-9a-fx]{2,})/i, raw) ||
      firstMatch(/\b([0-9]{3,})\b/, raw)
    return {
      lane: 'run', tool: 'delete-marked-panel', write: true,
      params: handle ? { handle: String(handle) } : {},
      confidence: 0.82,
      rationale: handle
        ? `Delete one panel (handle ${handle}) — a versioned write you can undo.`
        : 'Delete a marked panel — a versioned write you can undo.',
      alternatives: alternatives(tools, ['delete-marked-panel']),
    }
  }
  // count per layer
  if (has('count', 'how many', 'tally', 'number of', 'per layer') && find('count-by-layer')) {
    return runMatch('count-by-layer', {}, 0.9, 'Count entities on every layer — a safe read-only pass.', tools)
  }
  // area / measure
  if (has('area', 'sqft', 'sq ft', 'square', 'coverage', 'measure', 'size of') && find('measure-panel-area')) {
    return runMatch('measure-panel-area', {}, 0.85, 'Measure panel areas and totals; the largest panel is highlighted.', tools)
  }
  // near edge / setback
  if (has('edge', 'setback', 'within', 'near', 'border', 'perimeter') && find('highlight-panels-near-edge')) {
    const inches = firstMatch(/(\d+(?:\.\d+)?)\s*(?:in\b|inch|inches|")/i, raw)
    return runMatch('highlight-panels-near-edge',
      inches ? { distance_in: Number(inches) } : {}, 0.8,
      inches ? `Highlight panels within ${inches} in of the roof edge.` : 'Highlight panels near the roof edge.', tools)
  }
  // select by layer
  if (has('select', 'show', 'highlight') && has('layer') && find('select-by-layer')) {
    const layer = firstMatch(/layer\s+["']?([a-z0-9 _-]+?)["']?\s*$/i, raw)
    return runMatch('select-by-layer',
      layer ? { layer: layer.trim() } : {}, 0.78,
      `Select every entity on the ${layer ? layer.trim() + ' ' : ''}layer.`, tools)
  }

  // ---- low confidence: best guess + alternatives --------------------------
  const guess = find('count-by-layer') ? 'count-by-layer' : (tools[0]?.name || null)
  return {
    lane: 'run', tool: guess, params: {}, confidence: 0.42,
    rationale: "I'm not sure which capability you meant — here is my best guess.",
    alternatives: alternatives(tools, guess ? [guess] : []),
  }
}
