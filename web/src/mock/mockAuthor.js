// Mock tool-author — turns a natural-language description into a tool
// package (CONTRACT §2) + generated code + preview. Templated for a
// constrained family (select-by-layer / count / measure / near-edge /
// delete), mirroring the "Stub allowed" note in CONTRACT §6. Marked
// provenance author=agent, kind=script so it is runnable by the mock engine.

// Greedy WHOLE-WORD slug: join words with '-', appending the next word only
// while the result stays <=40 chars. If the very first word exceeds 40, keep
// just that first word sliced to 40. Never cut mid-word or leave a trailing '-'.
// A leading imperative phrase ("build a tool that …") names the ACT of authoring,
// not the tool — strip it so the tool is named after what it does. Trailing
// stopwords are dropped so the name never ends mid-phrase.
const LEAD = /^(?:build|create|make|write|author|generate|give me)\s+(?:me\s+)?(?:a|an|the)?\s*(?:new\s+)?(?:tool|script|command|function)\s+(?:that|which|to|for)?\s*/i
const STOP = new Set(['the', 'a', 'an', 'of', 'in', 'on', 'to', 'that', 'by', 'for', 'and', 'with', 'within', 'per'])

function slugify(text) {
  const stripped = String(text).replace(LEAD, '')
  const words = stripped.toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim().split(/\s+/).filter(Boolean)
  if (words.length === 0) return 'authored-tool'
  let slug = words[0].slice(0, 40)
  for (let i = 1; i < words.length; i++) {
    const candidate = slug + '-' + words[i]
    if (candidate.length <= 40) slug = candidate
    else break
  }
  const segs = slug.split('-')
  while (segs.length > 1 && STOP.has(segs[segs.length - 1])) segs.pop()
  return segs.join('-').replace(/-+$/g, '') || 'authored-tool'
}

// Parse a distance from the description: the first number followed by
// in/inch/inches/" — or, failing that, the first standalone integer.
function parseDistance(desc) {
  const d = String(desc)
  let m = d.match(/(\d+(?:\.\d+)?)\s*(?:in\b|inch(?:es)?|")/i)
  if (m) return Number(m[1])
  m = d.match(/\b(\d+)\b/)
  if (m) return Number(m[1])
  return null
}

const WRITE_VERBS = /\b(delete|remove|erase|move|insert|add)\b/
const SELECT_VERBS = /\b(select(?:s|ed|ing)?|highlight(?:s|ed|ing)?|mark(?:s|ed|ing)?|show(?:s|ed|ing)?|pick(?:s|ed|ing)?)\b/
const ORDINALS = new Map([
  ['second', 2], ['third', 3], ['fourth', 4], ['fifth', 5],
  ['sixth', 6], ['seventh', 7], ['eighth', 8], ['ninth', 9],
  ['tenth', 10], ['eleventh', 11], ['twelfth', 12],
])

function parseStride(desc) {
  const text = String(desc).toLowerCase()
  const numeric = text.match(/\bevery\s+(\d+)(?:st|nd|rd|th)?\b/)
  if (numeric) return Math.max(2, Number(numeric[1]))
  const word = text.match(/\bevery\s+([a-z]+)\b/)
  return word ? (ORDINALS.get(word[1]) || null) : null
}

function classify(desc) {
  const d = desc.toLowerCase()
  const stride = parseStride(d)
  if (stride && WRITE_VERBS.test(d)) return null
  if (stride && SELECT_VERBS.test(d)) return 'select_every_nth'
  // Write verbs win first — a "delete panels near the edge" request is a write,
  // not a read-only near-edge highlight.
  if (WRITE_VERBS.test(d)) return 'delete_marked_panel'
  if (/(area|square|sqft|sq ft|size|coverage)/.test(d)) return 'measure_area'
  if (/(edge|perimeter|within|near|setback|border|inches|distance)/.test(d)) return 'highlight_near_edge'
  if (/(count|how many|number of|tally)/.test(d)) return 'count_by_layer'
  if (SELECT_VERBS.test(d)) return 'select_by_layer'
  return null
}

const TEMPLATES = {
  count_by_layer: {
    caps: ['drawing.read'],
    params: { type: 'object', properties: {}, required: [] },
    code: (n) => `; ${n} — generated LISP (script) tool
(defun c:${n.replace(/-/g, '')} ( / ss i ent counts)
  (setq counts (list))
  (setq ss (ssget "X"))          ; all entities
  (repeat (setq i (sslength ss))
    (setq ent (ssname ss (setq i (1- i))))
    (leaf:tally counts (cdr (assoc 8 (entget ent)))))  ; group 8 = layer
  (leaf:emit counts))`,
  },
  highlight_near_edge: {
    caps: ['drawing.read'],
    params: {
      type: 'object',
      properties: { distance_in: { type: 'number', title: 'Distance (inches)', default: 60 } },
      required: [],
    },
    // `d` is the distance parsed from the description — the generated code must
    // agree with the params chip the prospect just read (falls back to 60).
    code: (n, d = 60) => `; ${n} — generated LISP (script) tool
(defun c:${n.replace(/-/g, '')} ( / dist box near)
  (setq dist (leaf:param "distance_in" ${Number(d).toFixed(1)}))
  (setq box (leaf:drawing-bounds))
  (setq near (leaf:filter-entities
               (lambda (e) (<= (leaf:dist-to-box (leaf:centroid e) box) dist))))
  (leaf:highlight near))`,
  },
  measure_area: {
    caps: ['drawing.read'],
    params: { type: 'object', properties: {}, required: [] },
    code: (n) => `; ${n} — generated LISP (script) tool
(defun c:${n.replace(/-/g, '')} ( / ss i ent total)
  (setq total 0.0)
  (setq ss (ssget "X" '((0 . "LWPOLYLINE"))))
  (repeat (setq i (sslength ss))
    (setq ent (ssname ss (setq i (1- i))))
    (setq total (+ total (leaf:poly-area ent))))
  (leaf:emit (list (cons "total_area_sqin" total))))`,
  },
  select_by_layer: {
    caps: ['drawing.read'],
    params: {
      type: 'object',
      properties: { layer: { type: 'string', title: 'Layer', default: 'Panels' } },
      required: ['layer'],
    },
    code: (n) => `; ${n} — generated LISP (script) tool
(defun c:${n.replace(/-/g, '')} ( / lyr ss)
  (setq lyr (leaf:param "layer" "Panels"))
  (setq ss (ssget "X" (list (cons 8 lyr))))
    (leaf:highlight ss))`,
  },
  select_every_nth: {
    caps: ['drawing.read'],
    params: {
      type: 'object',
      properties: {
        layer: { type: 'string', title: 'Layer', default: 'Panels' },
        stride: { type: 'number', title: 'Every Nth entity', default: 10 },
      },
      required: ['layer', 'stride'],
    },
    code: (n, stride = 10) => `; ${n} - generated LISP (script) tool
(defun c:${n.replace(/-/g, '')} ( / lyr stride ss i picked)
  (setq lyr (leaf:param "layer" "Panels"))
  (setq stride (leaf:param "stride" ${Math.max(2, Number(stride) || 10)}))
  (setq ss (ssget "X" (list (cons 8 lyr))))
  (setq i (1- stride) picked (ssadd))
  (while (< i (sslength ss))
    (ssadd (ssname ss i) picked)
    (setq i (+ i stride)))
  (leaf:highlight picked))`,
  },
  // Write template — aligns with M3's engine op `delete_marked_panel`. Creates
  // a new undoable version instead of reading the drawing read-only.
  delete_marked_panel: {
    caps: ['drawing.read', 'drawing.write'],
    params: {
      type: 'object',
      properties: { handle: { type: 'string', title: 'Handle', default: '' } },
      required: [],
    },
    code: (n) => `; ${n} — generated LISP (script) tool
(defun c:${n.replace(/-/g, '')} ( / h ent)
  (setq h (leaf:param "handle" ""))
  (setq ent (handent h))
  (if ent (progn (entdel ent) (leaf:emit (list (cons "removed" h))))
          (leaf:emit (list (cons "removed" nil)))))`,
  },
}

export function authorMock(description) {
  const op = classify(description)
  if (!op) {
    return {
      unsupported: true,
      message: 'This demo can author count, area, edge-distance, layer-selection, every-N selection, and single-panel delete tools. Rephrase the request as one of those operations.',
      supported: ['count', 'area', 'edge distance', 'layer selection', 'every-N selection', 'single-panel delete'],
    }
  }
  const tmpl = TEMPLATES[op]
  const name = slugify(description)

  // Numeric-param parse: the near-edge template respects the distance the user
  // typed instead of the hardcoded 60. Build a fresh params object so the shared
  // template is never mutated.
  let params = tmpl.params
  if (op === 'highlight_near_edge') {
    const dist = parseDistance(description)
    params = {
      type: 'object',
      properties: { distance_in: { type: 'number', title: 'Distance (inches)', default: dist == null ? 60 : dist } },
      required: [],
    }
  }
  if (op === 'select_every_nth') {
    const stride = parseStride(description) || 10
    params = {
      type: 'object',
      properties: {
        layer: { type: 'string', title: 'Layer', default: 'Panels' },
        stride: { type: 'number', title: 'Every Nth entity', default: stride },
      },
      required: ['layer', 'stride'],
    }
  }

  const caps = [...tmpl.caps]
  const isWrite = caps.includes('drawing.write')
  const created = new Date().toISOString()

  const tool = {
    name,
    version: '1.0.0',
    description: description.trim(),
    kind: 'script',
    engine_op: op,
    params,
    returns: { type: 'object' },
    capabilities: caps,
    provenance: { author: 'agent', created, grants: caps, reviewer: null },
  }
  const codeArg = op === 'select_every_nth'
    ? params.properties?.stride?.default
    : params.properties?.distance_in?.default
  const code = tmpl.code(name, codeArg)
  const preview = isWrite
    ? `Generated a "${op}" script tool from your description.\n` +
      `It creates a new undoable version of the drawing when you run it on Leaf.`
    : `Generated a "${op}" script tool from your description.\n` +
      `It runs read-only on the drawing and is ready to run on Leaf.`
  // `source: 'harness'` + `run_id` are the honest agent-authored signals the
  // AuthorPanel reads (readProvenance / authoredProvLine). The demo's authoring
  // IS the agent story, so the card must read "Authored by agent", not
  // "Templated". Shape stays a superset of { tool, code, preview }.
  return { tool, code, preview, source: 'harness', run_id: `demo-${op}-${name}`.slice(0, 24) }
}
