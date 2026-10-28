import { readFileSync } from 'node:fs'
import { createHash } from 'node:crypto'
import { execFileSync } from 'node:child_process'
import vm from 'node:vm'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { FORMATS, PRESETS, PRIMITIVES } from './widgetCatalog.js'
import { migrate, validateWidgetDoc } from './widgetDoc.js'
import { LA_USABLE_HEIGHT, laFitProblem } from './laFit.js'
import { renderWidget } from './widgetRender.js'
import { formatMoney, formatPercent, resolveBinding } from './widgetResolve.js'
import WidgetCard from './WidgetCard.jsx'
import { kashbotWidgetPack } from './kashbotWidgetPack.js'

// npm --prefix web runs Vitest from web; jsdom may expose an HTTP import.meta.url.
const root = resolve(process.cwd(), '..')
const read = (path) => readFileSync(resolve(root, path), 'utf8')
const fixture = (path) => JSON.parse(read('contract/fixtures/widget/' + path))
const designer = fixture('designer-v2-default.json')
const parity = fixture('parity/basic.json')
const golden = fixture('geometry-golden.json')
const clone = (value) => JSON.parse(JSON.stringify(value))

it('accepts a pinned graph output binding and refuses incomplete references', () => {
  const doc = clone(parity.doc)
  const binding = { source: 'graph.output', doc_id: 'metrics', output: 'total', min_renderer: '1.0.0', scale: 'identity', tone: 'auto' }
  doc.formats.la.els[1].binding = binding
  expect(validateWidgetDoc(doc)).toEqual(doc)
  delete binding.doc_id
  expect(() => validateWidgetDoc(doc)).toThrow('Unknown binding.')
})

describe('frozen designer migration', () => {
  it('derives the fixture exactly from the frozen defaultDoc, without sample values', () => {
    const html = read('contract/fixtures/widget/sources/kashbot-widget-designer-v2.html')
    const context = vm.createContext({})
    vm.runInContext(html.slice(html.indexOf('const FORMATS ='), html.indexOf('// Sample data,')) + ';result = defaultDoc()', context)
    const upstream = JSON.parse(JSON.stringify(context.result))
    // SEL-403c2 explicitly overrides the frozen upstream la canvas and footer.
    upstream.formats.la.h = 128
    upstream.formats.la.els = upstream.formats.la.els.filter((e) => !['divider', 'history'].includes(e.id))
    expect(upstream).toEqual(designer)
    expect(Object.keys(designer.formats)).toHaveLength(13)
    // SEL-403c2 removes the two la footer elements.
    expect(Object.values(designer.formats).flatMap((f) => f.els)).toHaveLength(53)
    expect(Object.keys(PRESETS)).toHaveLength(30)
    expect(kashbotWidgetPack.presets).toHaveLength(30)
  })
  it('preserves every frozen element coordinate within the point tolerance', () => {
    const doc = migrate(parity.doc)
    expect(doc).toEqual(parity.doc)
    const studio = clone(doc)
    // SEL-403c2 changes only the default la height and removes its footer.
    studio.formats.la.h = 128
    studio.formats.la.els = studio.formats.la.els.filter((e) => !['divider', 'history'].includes(e.id))
    expect(migrate(designer)).toEqual(studio)
    expect(migrate(doc)).toEqual(doc)
    for (const [id, f] of Object.entries(golden.formats)) {
      expect(FORMATS[id].w).toBe(f.width)
      expect(doc.formats[id].h).toBe(f.height)
      for (const expected of f.elements) {
        const actual = doc.formats[id].els.find((e) => e.id === expected.id)
        for (const axis of ['x', 'y', 'w', 'h']) expect(Math.abs(actual[axis] - expected[axis])).toBeLessThanOrEqual(golden.tolerance_pt)
      }
    }
  })
  it('refuses unknown designer kinds and oversized layouts instead of dropping them', () => {
    const unknown = clone(designer)
    unknown.formats.la.els[0].kind = 'html'
    expect(() => migrate(unknown)).toThrow('Unknown')
    const large = clone(designer)
    large.formats.la.els = Array.from({ length: 33 }, (_, i) => ({ ...large.formats.la.els[0], id: 'e' + i }))
    expect(() => migrate(large)).toThrow('32')
  })
  it('maps ring sources into explicit board scale, tone and UTC window bindings', () => {
    const doc = migrate(designer)
    expect(doc.formats.diMin.els[0].binding).toMatchObject({ source: 'kashbot.board', field: 'topOdds', scale: 'fraction', tone: 'leaf' })
    expect(doc.formats.watchCorner.els[0].binding.scale).toBe('today-percent')
    expect(doc.formats.lockCirc.els[0].binding.window).toBe('today-utc')
  })
  it('migrates and renders each of the 30 catalog presets', () => {
    for (const [kind, defaults] of Object.entries(PRESETS)) {
      const source = clone(designer)
      const e = { id: kind, kind, x: 0, y: 0, w: defaults.w, h: defaults.h, scale: 1, weight: defaults.weight ?? 600, color: defaults.color ?? 'ink', align: defaults.align ?? 'left', visible: true }
      for (const key of ['arrows', 'count', 'tracking', 'showPct', 'source', 'text', 'opacity']) if (defaults[key] !== undefined) e[key] = defaults[key]
      if (defaults.col) e.maxRows = 3
      source.formats.la.els = [e]
      const doc = migrate(source)
      expect(doc.formats.la.els[0].preset).toBe(kind)
      expect(renderWidget(doc).nodes.length).toBeGreaterThan(0)
    }
  })
})

describe('closed bounded document', () => {
  it('ignores drafts without a usable la format', () => {
    for (const doc of [
      undefined, null, false, 42, 'draft', [], {},
      ...[null, false, 42, 'draft', []].map((formats) => ({ formats })),
      ...[undefined, null, false, 42, 'draft', []].map((la) => ({ formats: { la } })),
    ]) {
      expect(() => laFitProblem(doc)).not.toThrow()
      expect(laFitProblem(doc)).toBeNull()
    }
  })
  it('judges partial la drafts using only finite geometry', () => {
    const message = 'The lock screen layout must fit in 128 pt. Lower its height or move elements up.'
    for (const els of [undefined, null, false, 42, 'draft', {}, { every: true }]) {
      for (const h of [undefined, null, '160', NaN, Infinity, -Infinity, 128, 160]) {
        const doc = { formats: { la: { h, els } } }
        expect(() => laFitProblem(doc)).not.toThrow()
        expect(laFitProblem(doc)).toBe(h === 160 ? message : null)
      }
    }
    const unmeasurable = [
      null, undefined, false, 42, 'draft', {}, { y: 129 }, { h: 129 },
      { y: '129', h: 1 }, { y: 129, h: '1' }, { y: null, h: 129 },
      { y: NaN, h: 129 }, { y: Infinity, h: 1 }, { y: 129, h: -Infinity },
    ]
    for (const h of [undefined, null, '160', {}, NaN, Infinity, -Infinity, 100, 128, 160]) {
      const height = Number.isFinite(h) ? h : LA_USABLE_HEIGHT
      const la = { h, els: [...unmeasurable, { y: height - 1, h: 1 }] }
      const doc = { formats: { la } }
      expect(() => laFitProblem(doc)).not.toThrow()
      expect(laFitProblem(doc)).toBe(h === 160 ? message : null)
      la.els.push({ y: height, h: 1 })
      expect(() => laFitProblem(doc)).not.toThrow()
      expect(laFitProblem(doc)).toBe(message)
    }
  })
  it('caps Studio la publication without changing the renderer frame or validation', () => {
    expect(FORMATS.la).toMatchObject({ w: 361, h: 160 })
    expect(LA_USABLE_HEIGHT).toBe(128)
    const doc = migrate(designer)
    expect(doc.formats.la.els.map((e) => e.id)).toEqual(['brand', 'total', 'symbols', 'pnl'])
    expect(doc.formats.la.els.every((e) => e.y + e.h <= doc.formats.la.h)).toBe(true)
    expect(validateWidgetDoc(doc)).toEqual(doc)
    expect(laFitProblem(doc)).toBeNull()
    expect(laFitProblem({ ...doc, active: 'diExp', formats: { diExp: doc.formats.diExp } })).toBeNull()
    const fitting = clone(doc)
    fitting.formats.la.els[0].y = 128 - fitting.formats.la.els[0].h
    expect(laFitProblem(fitting)).toBeNull()
    const tall = clone(doc)
    tall.formats.la.h = 160
    expect(validateWidgetDoc(tall)).toEqual(tall)
    expect(laFitProblem(tall)).toContain('128 pt')
    const overflow = clone(doc)
    overflow.formats.la.els[0].y = 129 - overflow.formats.la.els[0].h
    expect(laFitProblem(overflow)).toContain('128 pt')
    overflow.formats.la.els[0].y -= 1
    expect(validateWidgetDoc(overflow)).toEqual(overflow)
    expect(laFitProblem(overflow)).toBeNull()
  })
  it.each(parity.verdicts)('$name: accepted=$accepted', ({ path, value, accepted }) => {
    const doc = clone(parity.doc)
    if (path.length) {
      const parent = path.slice(0, -1).reduce((v, key) => v[key], doc)
      parent[path.at(-1)] = value
    }
    if (accepted) expect(validateWidgetDoc(doc)).toEqual(doc)
    else expect(() => validateWidgetDoc(doc)).toThrow()
  })
  it('refuses duplicate IDs, non-finite geometry, unknown formats and executable paths', () => {
    for (const change of [
      (d) => { d.formats.la.els[1].id = d.formats.la.els[0].id },
      (d) => { d.formats.la.els[0].x = Infinity },
      (d) => { d.formats.evil = d.formats.la },
      (d) => { d.formats.diMin.els[0].path = 'url(javascript:bad)' },
    ]) {
      const doc = clone(parity.doc); change(doc)
      expect(() => validateWidgetDoc(doc)).toThrow()
    }
    expect(() => validateWidgetDoc(parity.doc, '0.9.0')).toThrow('min_renderer')
  })
  it('publishes closed primitives, binding support and a bounded vendor schema', () => {
    const schema = JSON.parse(read('contract/widget-doc.v1.schema.json'))
    const support = JSON.parse(read('contract/widget-renderer-support.v1.json'))
    expect(schema.additionalProperties).toBe(false)
    expect(schema.$defs.element.additionalProperties).toBe(false)
    expect(schema.$defs.format.properties.els.maxItems).toBe(32)
    expect(Object.keys(support.primitives)).toEqual(PRIMITIVES)
    for (const f of Object.values(parity.doc.formats)) for (const e of f.els) {
      expect(support.primitives[e.kind].min_renderer).toBe(e.min_renderer)
      if (e.binding) expect(support.bindings[e.binding.field].min_renderer).toBe(e.binding.min_renderer)
    }
  })
})

describe('portable renderer and figures', () => {
  it('renders all formats deterministically without mutating their documents', () => {
    const before = JSON.stringify(parity.doc)
    for (const format of Object.keys(FORMATS)) {
      const tree = renderWidget(parity.doc, { format })
      expect(tree).toEqual(renderWidget(parity.doc, { format }))
      expect(tree.nodes.every((n) => PRIMITIVES.includes(n.kind))).toBe(true)
      expect(tree.nodes.every((n) => n.color.startsWith('--leaf-'))).toBe(true)
      expect(renderToStaticMarkup(React.createElement(WidgetCard, { doc: parity.doc, format }))).toContain('<svg')
    }
    expect(JSON.stringify(parity.doc)).toBe(before)
  })
  it('escapes literal text in the card and applies visibility', () => {
    const doc = clone(parity.doc)
    const e = doc.formats.la.els[0]
    e.preset = 'text'; e.text = '<script>bad()</script>'
    const markup = renderToStaticMarkup(React.createElement(WidgetCard, { doc }))
    expect(markup).toContain('&lt;script&gt;')
    expect(markup).not.toContain('<script>')
    e.visible = false
    expect(renderWidget(doc).nodes.some((n) => n.id.startsWith(e.id))).toBe(false)
  })
  it('keeps fraction input, exact half-away rounding and null-as-Not reported', () => {
    expect(formatMoney('123456.5')).toBe('$1,234.57')
    expect(formatMoney('-0.5')).toBe('-$0.01')
    expect(formatMoney('-0.1')).toBe('$0.00')
    expect(formatPercent('0.625')).toBe('62.5%')
    expect(formatPercent('-0.00005')).toBe('-0.01%')
    expect(formatMoney(null)).toBe('Not reported')
    for (const value of ['1e3', ' 1', '+1', '-0', '1,000', '1000001']) expect(() => formatMoney(value)).toThrow()
  })
  it('bounds board and ledger rows independently of the target', () => {
    const board = { rows: Array.from({ length: 9 }, () => ({ symbols: 'BTC' })), history: Array(9).fill('100') }
    const binding = (field) => ({ source: 'kashbot.board', field, scale: 'identity', tone: 'ink' })
    expect(resolveBinding(binding('symbols'), board, 10)).toHaveLength(3)
    expect(resolveBinding(binding('history'), board, 10)).toHaveLength(4)
    expect(resolveBinding(binding('cash'))[0].text).toBe('Not reported')
    for (const limit of [-1, 0, 1.5, NaN, Infinity, '3']) {
      expect(() => resolveBinding(binding('symbols'), board, limit)).toThrow('limit')
      expect(() => resolveBinding(binding('history'), board, limit)).toThrow('limit')
    }
  })
  it('requires the explicit UTC day for winRate on every target', () => {
    const binding = parity.doc.formats.lockCirc.els[0].binding
    expect(() => resolveBinding(binding, { winRate: '0.5' })).toThrow('UTC')
    const board = { winRate: '0.5', window: { start: '2026-10-21T00:00:00Z', end: '2026-10-22T00:00:00Z' } }
    expect(resolveBinding(binding, board)[0]).toMatchObject({ text: '50%', fraction: 0.5 })
    expect(renderWidget(parity.doc, { format: 'lockCirc', board }).nodes.some((n) => n.text === '50%')).toBe(true)
  })
  it('pins frozen source bytes and the hook-free renderer import closure', () => {
    const sources = fixture('sources/SOURCES.json')
    for (const source of sources.sources) {
      const bytes = readFileSync(resolve(root, source.frozen_path))
      expect(bytes.length).toBe(source.bytes)
      expect(createHash('sha256').update(bytes).digest('hex')).toBe(source.sha256)
    }
    expect(execFileSync(process.execPath, [resolve(root, 'scripts/sync-widget-renderer.mjs'), '--check'], { encoding: 'utf8' })).toContain('Widget renderer pins match')
  })
})
