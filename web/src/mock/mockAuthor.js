// Mock tool-author — turns a natural-language description into a tool
// package (CONTRACT §2) + generated code + preview. Templated for a
// constrained family (select-by-layer / count / measure / near-edge),
// mirroring the "Stub allowed" note in CONTRACT §6. Marked provenance
// author=agent, kind=script so it is runnable by the mock engine.

function slugify(text) {
  const base = text.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 40)
  return base || 'authored-tool'
}

function classify(desc) {
  const d = desc.toLowerCase()
  if (/(area|square|sqft|sq ft|size|coverage)/.test(d)) return 'measure_area'
  if (/(edge|perimeter|within|near|setback|border|inches|distance)/.test(d)) return 'highlight_near_edge'
  if (/(count|how many|number of|tally)/.test(d)) return 'count_by_layer'
  return 'select_by_layer'
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
    code: (n) => `; ${n} — generated LISP (script) tool
(defun c:${n.replace(/-/g, '')} ( / dist box near)
  (setq dist (leaf:param "distance_in" 60.0))
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
}

export function authorMock(description) {
  const op = classify(description)
  const tmpl = TEMPLATES[op]
  const name = slugify(description)
  const tool = {
    name,
    version: '1.0.0',
    description: description.trim(),
    kind: 'script',
    engine_op: op,
    params: tmpl.params,
    returns: { type: 'object' },
    capabilities: tmpl.caps,
    provenance: { author: 'agent', created: new Date().toISOString() },
  }
  const code = tmpl.code(name)
  const preview =
    `Generated a "${op}" script tool from your description.\n` +
    `It runs read-only on the drawing and is ready to run on Leaf.`
  return { tool, code, preview }
}
