// W4e slice G, the INTERIM sprite: until the icons8 key is in the credential
// store, the cockpit's icons come from a component design the claude-design
// backend generated for this project (original line icons, ours to use), read
// from its HTML by <title> (or the nearest label) and mapped onto the
// manifest's keys, plus the hand-drawn set in ./interim-icons.mjs for every
// key the design did not cover. It writes the SAME sprite and inventory files
// the icons8 fetch writes, with platform "interim-line-set", so
// `fetch_icons8.mjs` replaces it wholesale the moment it runs.
//
// HARDENING CONTRACT (fails closed, never sanitizes by deletion): every
// harvested icon is re-serialized from an ALLOWLIST of SVG shape tags and
// numeric/keyword attributes. Any other tag, any attribute outside the list,
// any attribute value outside the safe grammar, any non-whitespace text
// node, or any oversize icon rejects THAT icon; nothing from the source
// passes through except allowlisted attribute values that matched their
// grammar. Local files only; at most MAX_ICONS symbols.
//
// Usage: node scripts/build_interim_sprite.mjs <design.html> [<design2.html> ...]
import { readFileSync, writeFileSync, mkdirSync } from 'node:fs'
import { createHash } from 'node:crypto'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

import { INTERIM_ICONS } from './interim-icons.mjs'

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..')
const MANIFEST = join(ROOT, 'src', 'assets', 'icons8', 'manifest.json')
const BUILT = join(ROOT, 'src', 'assets', 'icons8', 'built.json')
const SPRITE = join(ROOT, 'public', 'icons8-sprite.svg')
const MAX_ICONS = 120
const MAX_SVG_BYTES = 16 * 1024

// Design titles -> manifest keys (the ribbon take names tools by command).
const ALIASES = {
  'cad ribbon': 'line', line: 'line', 'polyline (pl)': 'polyline', 'circle (c)': 'circle', 'arc (a)': 'arc',
  'rectangle (rec)': 'rectangle', 'ellipse (el)': 'ellipse', 'point (po)': 'point', 'move (m)': 'move',
  'copy (co)': 'copy', 'rotate (ro)': 'rotate', 'scale (sc)': 'scale', 'mirror (mi)': 'mirror', 'trim (tr)': 'trim',
  'extend (ex)': 'extend', 'mtext (mt)': 'text', 'dimension (dim)': 'dimension', 'leader (le)': 'leader',
  'create block (b)': 'block-create', 'insert block (i)': 'block-insert', 'match properties (ma)': 'match',
  'group (g)': 'group', 'ungroup (ug)': 'ungroup', 'paste (ctrl+v)': 'paste', 'cut (ctrl+x)': 'cut', 'copy (ctrl+c)': 'copy',
  dimensions: 'bulb', hidden: 'bulb-off',
}

// The allowlist: shape tags, and per-attribute value grammars. `g` may
// nest; `title` / `desc` are skipped with their text (the title was read
// before serialization). Nothing else exists as far as the sprite is concerned.
const SHAPE_TAGS = new Set(['path', 'circle', 'rect', 'line', 'polyline', 'polygon', 'ellipse', 'g'])
const SKIP_TAGS = new Set(['title', 'desc'])
const NUMBER = /^-?\d+(\.\d+)?%?$/
const NUMBER_LIST = /^[-\d.,\s]+$/
const PATH_DATA = /^[MmZzLlHhVvCcSsQqTtAa\d.,\s-]+$/
const COLOR = /^(none|currentColor|#[0-9a-fA-F]{3,8})$/
const KEYWORD = /^[a-z-]+$/
const TRANSFORM = /^[a-zA-Z()\d.,\s-]+$/
const ATTRS = {
  d: PATH_DATA, cx: NUMBER, cy: NUMBER, r: NUMBER, rx: NUMBER, ry: NUMBER, x: NUMBER, y: NUMBER,
  x1: NUMBER, y1: NUMBER, x2: NUMBER, y2: NUMBER, width: NUMBER, height: NUMBER, points: NUMBER_LIST,
  fill: COLOR, stroke: COLOR, 'stroke-width': NUMBER, 'stroke-linecap': KEYWORD, 'stroke-linejoin': KEYWORD,
  'stroke-dasharray': NUMBER_LIST, opacity: NUMBER, 'fill-opacity': NUMBER, 'stroke-opacity': NUMBER,
  'fill-rule': KEYWORD, 'clip-rule': KEYWORD, transform: TRANSFORM,
}
const TAG_RE = /<\s*(\/?)\s*([a-zA-Z][a-zA-Z0-9:-]*)\s*([^<>]*?)\s*(\/?)\s*>/g
const ATTR_RE = /([a-zA-Z][a-zA-Z0-9:-]*)\s*=\s*"([^"<>]*)"/g

function titleOf(svg, before, after) {
  const t = svg.match(/<title>\s*([^<]+?)\s*<\/title>/i)
  if (t) return t[1].trim()
  const attr = before.slice(-220).match(/(?:title|aria-label|data-name|data-key)="([^"]+)"/)
  if (attr) return attr[1].trim()
  const text = after.slice(0, 220).match(/>\s*([A-Za-z][A-Za-z0-9 ()+\-]{1,28})\s*</)
  return text ? text[1].trim() : ''
}

// Re-serialize inner SVG markup through the allowlist. Returns '' (reject)
// on anything outside it: unknown tag, unknown attribute, value outside its
// grammar, text content, unbalanced nesting, or size.
export function serializeAllowlisted(inner) {
  if (typeof inner !== 'string' || Buffer.byteLength(inner, 'utf8') > MAX_SVG_BYTES) return ''
  const out = []
  const stack = []
  let cursor = 0
  let skipping = 0
  for (const m of inner.matchAll(TAG_RE)) {
    const between = inner.slice(cursor, m.index)
    if (!skipping && between.trim() !== '') return ''
    cursor = m.index + m[0].length
    const closing = m[1] === '/'
    const tag = m[2].toLowerCase()
    const selfClosing = m[4] === '/'
    if (SKIP_TAGS.has(tag)) {
      if (closing) { if (skipping > 0) skipping -= 1; else return '' }
      else if (!selfClosing) skipping += 1
      continue
    }
    if (skipping) return ''
    if (!SHAPE_TAGS.has(tag)) return ''
    if (closing) {
      if (stack.pop() !== tag) return ''
      out.push(`</${tag}>`)
      continue
    }
    const attrs = []
    let consumed = 0
    for (const a of m[3].matchAll(ATTR_RE)) {
      consumed += a[0].length
      const name = a[1].toLowerCase()
      const value = a[2]
      const grammar = ATTRS[name]
      if (!grammar || !grammar.test(value)) return ''
      attrs.push(`${name}="${value}"`)
    }
    // Every byte of the attribute string must have been an allowlisted pair.
    if (m[3].replace(/\s+/g, '').length !== attrs.join('').replace(/\s+/g, '').length && m[3].trim() !== '') {
      if (consumed === 0 || m[3].replace(ATTR_RE, '').trim() !== '') return ''
    }
    out.push(`<${tag}${attrs.length ? ' ' + attrs.join(' ') : ''}${selfClosing ? '/>' : '>'}`)
    if (!selfClosing) stack.push(tag)
  }
  if (inner.slice(cursor).trim() !== '' || stack.length !== 0 || skipping !== 0) return ''
  return out.join('')
}

function toSymbol(key, svg) {
  const open = svg.match(/<svg\b[^>]*>/i)
  if (!open) return ''
  const attrs = open[0]
  const viewBox = (attrs.match(/viewBox="([^"]+)"/i) || [])[1] || '0 0 24 24'
  if (!NUMBER_LIST.test(viewBox)) return ''
  const rootStroke = (attrs.match(/\sstroke-width="([^"]+)"/i) || [])[1] || '1.5'
  if (!NUMBER.test(rootStroke)) return ''
  const rootFill = /\sfill="none"/i.test(attrs) ? 'none' : 'currentColor'
  const inner = serializeAllowlisted(svg.slice(open.index + attrs.length).replace(/<\/svg>\s*$/i, ''))
  if (!inner) return ''
  // Colours become currentColor so the chrome tints the icon.
  const tinted = inner.replace(/(fill|stroke)="#[0-9a-fA-F]{3,8}"/g, '$1="currentColor"')
  const g = `<g fill="${rootFill}" stroke="currentColor" stroke-width="${rootStroke}" stroke-linecap="round" stroke-linejoin="round">${tinted}</g>`
  return `<symbol id="i8-${key}" viewBox="${viewBox}">${g}</symbol>`
}

function main() {
  const files = process.argv.slice(2)
  if (files.length === 0) throw new Error('usage: build_interim_sprite.mjs <design.html> [...]')
  const manifest = JSON.parse(readFileSync(MANIFEST, 'utf8'))
  const wanted = new Set(Object.keys(manifest.icons || {}))
  const symbols = new Map()
  const rejected = []
  for (const file of files) {
    const html = readFileSync(file, 'utf8')
    for (const m of html.matchAll(/<svg\b[^>]*>[\s\S]*?<\/svg>/gi)) {
      const before = html.slice(Math.max(0, m.index - 300), m.index)
      const after = html.slice(m.index + m[0].length, m.index + m[0].length + 300)
      const title = titleOf(m[0], before, after)
      const key = ALIASES[title.toLowerCase()] || title.toLowerCase()
      if (!wanted.has(key) || symbols.has(key)) continue
      const symbol = toSymbol(key, m[0])
      if (symbol) symbols.set(key, symbol)
      else rejected.push(key)
      if (symbols.size >= MAX_ICONS) break
    }
  }
  // The hand set fills every key the designs did not cover (never overrides
  // one they did); it goes through the same allowlist.
  for (const [key, inner] of Object.entries(INTERIM_ICONS)) {
    if (!wanted.has(key) || symbols.has(key)) continue
    const safe = serializeAllowlisted(inner)
    if (!safe) { rejected.push(key); continue }
    symbols.set(key, `<symbol id="i8-${key}" viewBox="0 0 24 24"><g fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">${safe}</g></symbol>`)
  }
  if (symbols.size === 0) throw new Error('no manifest key matched an icon in the given designs')
  const ids = [...symbols.keys()].sort()
  const body = `<svg xmlns="http://www.w3.org/2000/svg" style="display:none" data-platform="interim-line-set">${ids.map((k) => symbols.get(k)).join('')}</svg>\n`
  const hash = createHash('sha256').update(body).digest('hex').slice(0, 12)
  mkdirSync(dirname(SPRITE), { recursive: true })
  writeFileSync(SPRITE, body)
  writeFileSync(BUILT, JSON.stringify({
    _comment: 'INTERIM: line icons generated by the claude-design backend for this project (design a5db21fd7c41) plus the hand set in scripts/interim-icons.mjs, keyed to the manifest and re-serialized through an allowlist; fetch_icons8.mjs replaces this file and the sprite when the icons8 key is in the store.',
    platform: 'interim-line-set', hash, fetchedAt: new Date().toISOString(), ids,
  }, null, 2) + '\n')
  const missing = [...wanted].filter((k) => !symbols.has(k)).sort()
  console.log(`interim sprite: ${ids.length} icons, hash ${hash}, ${Buffer.byteLength(body)} bytes; rejected ${rejected.length} (${rejected.join(', ')}); missing ${missing.length}: ${missing.join(', ')}`)
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) main()
