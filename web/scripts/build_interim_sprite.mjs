// W4e slice G, the INTERIM sprite: until the icons8 key is in the credential
// store, the cockpit's icons come from two component designs the claude-design
// backend generated for this project (original line icons, ours to use), read
// from their HTML by <title> (or the nearest label) and mapped onto the
// manifest's keys. It writes the SAME sprite and inventory files the icons8
// fetch writes, with platform "interim-line-set", so `fetch_icons8.mjs` replaces
// it wholesale the moment it runs. Bounded: local files only, SVG-only,
// no script/foreignObject/image, at most MAX_ICONS symbols.
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

function titleOf(svg, before, after) {
  const t = svg.match(/<title>\s*([^<]+?)\s*<\/title>/i)
  if (t) return t[1].trim()
  const attr = before.slice(-220).match(/(?:title|aria-label|data-name|data-key)="([^"]+)"/)
  if (attr) return attr[1].trim()
  const text = after.slice(0, 220).match(/>\s*([A-Za-z][A-Za-z0-9 ()+\-]{1,28})\s*</)
  return text ? text[1].trim() : ''
}

function toSymbol(key, svg) {
  const open = svg.match(/<svg\b[^>]*>/i)
  if (!open) return ''
  const attrs = open[0]
  const viewBox = (attrs.match(/viewBox="([^"]+)"/i) || [])[1] || '0 0 24 24'
  const rootStroke = (attrs.match(/\sstroke-width="([^"]+)"/i) || [])[1]
  let inner = svg.slice(open.index + attrs.length).replace(/<\/svg>\s*$/i, '')
  inner = inner
    .replace(/<(title|desc|metadata)\b[\s\S]*?<\/\1>/gi, '')
    .replace(/<script\b[\s\S]*?<\/script>/gi, '')
    .replace(/\son[a-z]+="[^"]*"/gi, '')
    .replace(/\sfill="(?!none)[^"]*"/gi, ' fill="currentColor"')
    .replace(/\sstroke="(?!none)[^"]*"/gi, ' stroke="currentColor"')
    .replace(/\s{2,}/g, ' ')
    .trim()
  if (!inner || /<(script|foreignObject|image)\b/i.test(inner)) return ''
  if (Buffer.byteLength(inner, 'utf8') > MAX_SVG_BYTES) return ''
  // The root's stroke settings travel with the symbol as a wrapping group.
  const rootFill = /\sfill="none"/i.test(attrs) ? 'none' : 'currentColor'
  const g = `<g fill="${rootFill}" stroke="currentColor" stroke-width="${rootStroke || '1.5'}" stroke-linecap="round" stroke-linejoin="round">${inner}</g>`
  return `<symbol id="i8-${key}" viewBox="${viewBox}">${g}</symbol>`
}

function main() {
  const files = process.argv.slice(2)
  if (files.length === 0) throw new Error('usage: build_interim_sprite.mjs <design.html> [...]')
  const manifest = JSON.parse(readFileSync(MANIFEST, 'utf8'))
  const wanted = new Set(Object.keys(manifest.icons || {}))
  const symbols = new Map()
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
      if (symbols.size >= MAX_ICONS) break
    }
  }
  // The hand set fills every key the designs did not cover (never overrides one they did).
  for (const [key, inner] of Object.entries(INTERIM_ICONS)) {
    if (!wanted.has(key) || symbols.has(key)) continue
    if (/<(script|foreignObject|image)\b/i.test(inner)) continue
    symbols.set(key, `<symbol id="i8-${key}" viewBox="0 0 24 24"><g fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">${inner}</g></symbol>`)
  }
  if (symbols.size === 0) throw new Error('no manifest key matched an icon in the given designs')
  const ids = [...symbols.keys()].sort()
  const body = `<svg xmlns="http://www.w3.org/2000/svg" style="display:none" data-platform="interim-line-set">${ids.map((k) => symbols.get(k)).join('')}</svg>\n`
  const hash = createHash('sha256').update(body).digest('hex').slice(0, 12)
  mkdirSync(dirname(SPRITE), { recursive: true })
  writeFileSync(SPRITE, body)
  writeFileSync(BUILT, JSON.stringify({
    _comment: 'INTERIM: line icons generated by the claude-design backend for this project (designs a5db21fd7c41 + the icon sheet), keyed to the manifest; fetch_icons8.mjs replaces this file and the sprite when the icons8 key is in the store.',
    platform: 'interim-line-set', hash, fetchedAt: new Date().toISOString(), ids,
  }, null, 2) + '\n')
  const missing = [...wanted].filter((k) => !symbols.has(k)).sort()
  console.log(`interim sprite: ${ids.length} icons, hash ${hash}, ${Buffer.byteLength(body)} bytes; missing ${missing.length}: ${missing.join(', ')}`)
}

main()
