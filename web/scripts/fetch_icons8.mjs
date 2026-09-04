// W4e slice G: resolve every cockpit icon key in src/assets/icons8/manifest.json
// through the icons8 API in ONE style, and write ONE sprite
// (public/icons8-sprite.svg) plus its inventory (src/assets/icons8/built.json).
//
// Usage:
//   node scripts/fetch_icons8.mjs               # resolve + build the sprite
//   node scripts/fetch_icons8.mjs --dry-run     # search only, print matches
//   node scripts/fetch_icons8.mjs --pin         # also write resolved ids back into the manifest
//   node scripts/fetch_icons8.mjs --only line,arc
//
// The key is read IN-PROCESS from the operator's credential store
// (~/.cadwalk/credentials.json, ICONS8_API_TOKEN) or the environment; it never
// appears in argv, in a URL that gets logged, or in any output. Bounded: at
// most MAX_ICONS keys, one search + one fetch per key, 10s per request, one
// retry, fails closed on anything that is not an SVG document.
import { readFileSync, writeFileSync, mkdirSync, existsSync, readdirSync } from 'node:fs'
import { createHash } from 'node:crypto'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'
import { homedir } from 'node:os'

import { serializeAllowlisted } from './build_interim_sprite.mjs'

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..')
const MANIFEST = join(ROOT, 'src', 'assets', 'icons8', 'manifest.json')
const BUILT = join(ROOT, 'src', 'assets', 'icons8', 'built.json')
const SPRITE = join(ROOT, 'public', 'icons8-sprite.svg')
const API = 'https://api-icons.icons8.com/publicApi/icons'
// The icons8 MCP endpoint (Basic API plan keys authenticate HERE, with a
// Bearer header; the REST publicApi refused the same key with 401 on
// 2026-09-03). Same search + SVG shape, JSON-RPC over streamable HTTP.
const MCP = 'https://mcp.icons8.com/mcp/'
const MAX_ICONS = 120
const TIMEOUT_MS = 10_000
const MAX_SVG_BYTES = 64 * 1024

const args = process.argv.slice(2)
const flag = (name) => args.includes(name)
const only = (() => {
  const i = args.indexOf('--only')
  return i >= 0 && args[i + 1] ? new Set(args[i + 1].split(',').map((s) => s.trim()).filter(Boolean)) : null
})()

function readToken() {
  const fromEnv = process.env.ICONS8_API_TOKEN
  if (fromEnv && fromEnv.trim()) return fromEnv.trim()
  const store = join(homedir(), '.cadwalk', 'credentials.json')
  if (!existsSync(store)) return ''
  try {
    const data = JSON.parse(readFileSync(store, 'utf8'))
    const value = data && typeof data === 'object' ? data.ICONS8_API_TOKEN : ''
    return typeof value === 'string' ? value.trim() : ''
  } catch {
    return ''
  }
}

async function getJson(url, token) {
  for (let attempt = 0; attempt < 2; attempt += 1) {
    const ctrl = new AbortController()
    const timer = setTimeout(() => ctrl.abort(), TIMEOUT_MS)
    try {
      const res = await fetch(url, { headers: { 'Api-Key': token, Accept: 'application/json' }, signal: ctrl.signal })
      clearTimeout(timer)
      if (res.status === 401 || res.status === 403) throw new Error(`icons8 refused the key (HTTP ${res.status})`)
      if (!res.ok) { if (attempt === 0) continue; throw new Error(`HTTP ${res.status}`) }
      return await res.json()
    } catch (err) {
      clearTimeout(timer)
      if (attempt === 1 || /refused the key/.test(String(err?.message))) throw err
    }
  }
  throw new Error('unreachable')
}

function scoreMatch(icon, term, platform) {
  const t = term.toLowerCase()
  const names = [icon.commonName, icon.name].filter(Boolean).map((s) => String(s).toLowerCase())
  let score = 0
  if (icon.platform === platform) score += 100
  if (names.some((n) => n === t)) score += 50
  else if (names.some((n) => n.replace(/[-_]/g, ' ') === t)) score += 40
  else if (names.some((n) => n.includes(t))) score += 10
  return score
}

function decodeSvg(icon) {
  // publicApi returns the SVG body base64-encoded in `svg`; some responses
  // carry it raw. Accept both, refuse anything that is not an <svg> document.
  const raw = icon?.svg
  if (typeof raw !== 'string' || !raw) return ''
  let text = raw.trim().startsWith('<') ? raw : Buffer.from(raw, 'base64').toString('utf8')
  text = text.trim()
  if (!/^(<\?xml[^>]*>\s*)?(<!DOCTYPE[^>]*>\s*)?<svg[\s>]/i.test(text)) return ''
  if (Buffer.byteLength(text, 'utf8') > MAX_SVG_BYTES) return ''
  return text
}

// Normalize to a <symbol>: keep the viewBox and re-serialize the body through
// the allowlist (shape tags and numeric/keyword attributes only, fails closed
// on anything else); hard-coded fills and strokes become currentColor so the
// chrome tints the icon. Never sanitizes by deletion.
export function toSymbol(key, svgText) {
  const open = svgText.match(/<svg\b[^>]*>/i)
  if (!open) return ''
  const attrs = open[0]
  const viewBox = (attrs.match(/viewBox="([^"]+)"/i) || [])[1] || '0 0 24 24'
  if (!/^[-\d.,\s]+$/.test(viewBox)) return ''
  const body = svgText.slice(open.index + attrs.length).replace(/<\/svg>\s*$/i, '')
  const inner = serializeAllowlisted(body)
  if (process.env.ICONS8_DEBUG) console.error(`[debug ${key}] svg ${svgText.length} chars, body ${body.length}, head ${JSON.stringify(body.slice(0, 40))}, serialized ${inner.length}`)
  if (!inner) return ''
  const tinted = inner.replace(/(fill|stroke)="#[0-9a-fA-F]{3,8}"/g, '$1="currentColor"')
  return `<symbol id="i8-${key}" viewBox="${viewBox}">${tinted}</symbol>`
}

// `--dir <folder>`: build from SVG files the operator downloaded from icons8
// with the icon subscription (no API plan needed). A key matches a file named
// after the key or its search term, case- and separator-insensitive
// ("opened folder" -> opened-folder.svg / opened_folder.svg / OpenedFolder.svg).
function fileFor(dir, key, term) {
  const norm = (s) => String(s).toLowerCase().replace(/\.svg$/, '').replace(/[^a-z0-9]+/g, '')
  const wanted = new Set([norm(key), norm(term)])
  for (const name of readdirSync(dir)) {
    if (!name.toLowerCase().endsWith('.svg')) continue
    if (wanted.has(norm(name))) return join(dir, name)
  }
  return ''
}

function buildFromDir(manifest, entries, dir) {
  const platform = String(manifest.platform || '')
  const symbols = []
  const built = []
  const failures = []
  for (const [key, spec] of entries) {
    const file = fileFor(dir, key, spec.term || key)
    if (!file) { failures.push(`${key}: no file`); continue }
    const text = readFileSync(file, 'utf8')
    if (Buffer.byteLength(text, 'utf8') > MAX_SVG_BYTES || !/<svg[\s>]/i.test(text)) { failures.push(`${key}: not an SVG or too large`); continue }
    const symbol = toSymbol(key, text)
    if (!symbol) { failures.push(`${key}: could not be normalized`); continue }
    symbols.push(symbol)
    built.push(key)
  }
  return { platform, symbols, built, failures }
}

// ---- MCP transport: initialize once, then tools/call; SSE or JSON bodies. ----
async function mcpPost(token, payload) {
  for (let attempt = 0; attempt < 2; attempt += 1) {
    const ctrl = new AbortController()
    const timer = setTimeout(() => ctrl.abort(), TIMEOUT_MS)
    try {
      const res = await fetch(MCP, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Accept: 'application/json, text/event-stream', Authorization: `Bearer ${token}` },
        body: JSON.stringify(payload),
        signal: ctrl.signal,
      })
      clearTimeout(timer)
      if (res.status === 401 || res.status === 403) throw new Error(`icons8 MCP refused the key (HTTP ${res.status})`)
      if (!res.ok) { if (attempt === 0) continue; throw new Error(`HTTP ${res.status}`) }
      const body = await res.text()
      let out = null
      for (const raw of body.split(/\r?\n/)) {
        const line = raw.startsWith('data:') ? raw.slice(5).trim() : raw.trim()
        if (!line.startsWith('{')) continue
        try { out = JSON.parse(line) } catch { /* not this line */ }
      }
      if (!out) throw new Error('no JSON-RPC body')
      if (out.error) throw new Error(`MCP error ${out.error.code}: ${out.error.message}`)
      return out.result
    } catch (err) {
      clearTimeout(timer)
      if (attempt === 1 || /refused the key|MCP error/.test(String(err?.message))) throw err
    }
  }
  throw new Error('unreachable')
}

async function mcpCall(token, name, args, id) {
  const result = await mcpPost(token, { jsonrpc: '2.0', id, method: 'tools/call', params: { name, arguments: args } })
  const text = (result?.content || []).filter((c) => c.type === 'text').map((c) => c.text).join('')
  if (result?.isError) throw new Error(`${name}: ${text.slice(0, 200)}`)
  return text
}

// The SVG can come back raw, JSON-wrapped, or base64; refuse anything else.
export function svgFromMcpText(text) {
  const t = String(text || '').trim()
  // JSON first: the MCP wraps the markup as {"svg": "<svg ...>"} with escaped
  // quotes, so a raw <svg scan on the JSON text would return broken markup.
  if (t.startsWith('{')) {
    try {
      const j = JSON.parse(t)
      const cand = j.svg || j.data || j.content || j.result || ''
      if (typeof cand === 'string') {
        if (/<svg[\s>]/i.test(cand)) return cand.slice(cand.search(/<svg[\s>]/i))
        const dec = Buffer.from(cand, 'base64').toString('utf8')
        if (/<svg[\s>]/i.test(dec)) return dec
      }
      return ''
    } catch { return '' }
  }
  if (/<svg[\s>]/i.test(t)) return t.slice(t.search(/<svg[\s>]/i))
  return ''
}

async function fetchViaMcp(manifest, entries, platform, token) {
  await mcpPost(token, { jsonrpc: '2.0', id: 1, method: 'initialize', params: { protocolVersion: '2025-03-26', capabilities: {}, clientInfo: { name: 'leaf-w4e-fetch', version: '1' } } })
  const symbols = []
  const built = []
  const failures = []
  let id = 10
  for (const [key, spec] of entries) {
    const term = String(spec.term || key)
    try {
      let iconId = spec.id
      let picked = null
      if (!iconId) {
        const text = await mcpCall(token, 'search_icons', { query: term, platform, amount: 8 }, id += 1)
        const list = JSON.parse(text)?.icons || []
        picked = list.map((i) => ({ i, s: scoreMatch(i, term, platform) })).sort((a, b) => b.s - a.s)[0]?.i || null
        if (!picked || picked.platform !== platform) throw new Error(`no ${platform} icon for "${term}"`)
        iconId = picked.id
      }
      if (flag('--dry-run')) {
        console.log(`${key.padEnd(14)} ${term.padEnd(18)} -> ${iconId}${picked ? ` (${picked.commonName || picked.name})` : ''}`)
        continue
      }
      const svg = svgFromMcpText(await mcpCall(token, 'get_icon_svg', { icon_id: iconId }, id += 1))
      if (!svg || Buffer.byteLength(svg, 'utf8') > MAX_SVG_BYTES) throw new Error(`icon ${iconId} returned no usable SVG`)
      const symbol = toSymbol(key, svg)
      if (!symbol) throw new Error(`icon ${iconId} could not be normalized`)
      symbols.push(symbol)
      built.push(key)
      if (flag('--pin')) manifest.icons[key] = { ...spec, id: iconId, name: picked?.commonName || picked?.name || spec.name }
      console.log(`${key.padEnd(14)} ok (${iconId})`)
    } catch (err) {
      failures.push(`${key}: ${err?.message || err}`)
      console.error(`${key.padEnd(14)} FAILED ${err?.message || err}`)
    }
  }
  return { symbols, built, failures }
}

async function main() {
  const manifest = JSON.parse(readFileSync(MANIFEST, 'utf8'))
  const platform = String(manifest.platform || '')
  const entries = Object.entries(manifest.icons || {}).filter(([k]) => !only || only.has(k))
  if (!platform || entries.length === 0) throw new Error('manifest has no platform or no icons')
  if (entries.length > MAX_ICONS) throw new Error(`manifest lists ${entries.length} icons, cap is ${MAX_ICONS}`)
  // `--merge-interim`: keep the served icons8 sprite as it is and ADD a
  // hand-drawn symbol (scripts/interim-icons.mjs) for every manifest key the
  // sprite lacks, through the same allowlist. For a new cockpit tool that
  // arrives between icons8 fetches (W4g-4 EXPLODE): the icons8 set is never
  // replaced, the receipt names the interim keys, and the next fetch with
  // the key pinned swaps them out.
  if (args.includes('--merge-interim')) {
    const { INTERIM_ICONS } = await import('./interim-icons.mjs')
    const sprite = readFileSync(SPRITE, 'utf8')
    const present = new Set([...sprite.matchAll(/<symbol id="i8-([^"]+)"/g)].map((m) => m[1]))
    const added = []
    const rejected = []
    let extra = ''
    for (const [key] of entries) {
      if (present.has(key)) continue
      const inner = INTERIM_ICONS[key]
      if (!inner) { rejected.push(key); continue }
      const safe = serializeAllowlisted(inner)
      if (!safe) { rejected.push(key); continue }
      extra += `<symbol id="i8-${key}" viewBox="0 0 24 24"><g fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">${safe}</g></symbol>`
      added.push(key)
    }
    if (added.length === 0 && rejected.length === 0) { console.log('sprite already carries every manifest key; nothing merged'); return }
    if (rejected.length) { console.error(`no interim symbol for: ${rejected.join(', ')}`); process.exit(1) }
    const body = sprite.replace(/<\/svg>\s*$/, `${extra}</svg>\n`)
    const hash = createHash('sha256').update(body).digest('hex').slice(0, 12)
    writeFileSync(SPRITE, body)
    const built = JSON.parse(readFileSync(BUILT, 'utf8'))
    const ids = [...new Set([...(built.ids || []), ...added])].sort()
    writeFileSync(BUILT, JSON.stringify({
      ...built, hash, fetchedAt: new Date().toISOString(), ids,
      interim: [...new Set([...(built.interim || []), ...added])].sort(),
    }, null, 2) + '\n')
    console.log(`merged ${added.length} interim symbol(s) (${added.join(', ')}) into the sprite: ${ids.length} icons, hash ${hash}, ${Buffer.byteLength(body)} bytes`)
    return
  }
  const dirIndex = args.indexOf('--dir')
  if (dirIndex >= 0) {
    const dir = args[dirIndex + 1]
    if (!dir || !existsSync(dir)) throw new Error('--dir needs an existing folder of SVG files')
    const r = buildFromDir(manifest, entries, dir)
    if (r.built.length === 0) throw new Error('no manifest key matched a file in ' + dir)
    const body = `<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" style="display:none" data-platform="${r.platform}">${r.symbols.join('')}</svg>\n`
    const hash = createHash('sha256').update(body).digest('hex').slice(0, 12)
    mkdirSync(dirname(SPRITE), { recursive: true })
    writeFileSync(SPRITE, body)
    writeFileSync(BUILT, JSON.stringify({
      _comment: 'Written by web/scripts/fetch_icons8.mjs --dir: icons8 SVGs downloaded with the icon subscription, keyed to the manifest.',
      platform: r.platform, hash, fetchedAt: new Date().toISOString(), ids: r.built.sort(),
    }, null, 2) + '\n')
    console.log(`sprite from ${dir}: ${r.built.length} icons, hash ${hash}, ${Buffer.byteLength(body)} bytes; failures: ${r.failures.length}${r.failures.length ? ' (' + r.failures.join('; ') + ')' : ''}`)
    if (r.failures.length) process.exit(1)
    return
  }
  const token = readToken()
  if (!token) {
    console.error('no ICONS8_API_TOKEN in the credential store or environment; nothing fetched')
    process.exit(2)
  }
  let symbols = []
  let built = []
  let failures = []
  if (flag('--mcp')) {
    ({ symbols, built, failures } = await fetchViaMcp(manifest, entries, platform, token))
  } else for (const [key, spec] of entries) {
    const term = String(spec.term || key)
    try {
      let id = spec.id
      let picked = null
      if (!id) {
        const url = `${API}/search?term=${encodeURIComponent(term)}&platform=${encodeURIComponent(platform)}&amount=8&language=en-US`
        const data = await getJson(url, token)
        const list = Array.isArray(data?.icons) ? data.icons : []
        picked = list.map((i) => ({ i, s: scoreMatch(i, term, platform) })).sort((a, b) => b.s - a.s)[0]?.i || null
        if (!picked || picked.platform !== platform) throw new Error(`no ${platform} icon for "${term}"`)
        id = picked.id
      }
      if (flag('--dry-run')) {
        console.log(`${key.padEnd(14)} ${term.padEnd(18)} -> ${id}${picked ? ` (${picked.commonName || picked.name})` : ''}`)
        continue
      }
      const icon = (await getJson(`${API}/icon?id=${encodeURIComponent(id)}`, token))?.icon
      const svg = decodeSvg(icon)
      if (!svg) throw new Error(`icon ${id} returned no SVG (plan entitlement?)`)
      const symbol = toSymbol(key, svg)
      if (!symbol) throw new Error(`icon ${id} could not be normalized`)
      symbols.push(symbol)
      built.push(key)
      if (flag('--pin')) manifest.icons[key] = { ...spec, id, name: picked?.commonName || picked?.name || spec.name }
      console.log(`${key.padEnd(14)} ok (${id})`)
    } catch (err) {
      failures.push(`${key}: ${err?.message || err}`)
      console.error(`${key.padEnd(14)} FAILED ${err?.message || err}`)
    }
  }
  if (flag('--dry-run')) return
  if (built.length === 0) throw new Error('no icons resolved; sprite not written')
  const body = `<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" style="display:none" data-platform="${platform}">${symbols.join('')}</svg>\n`
  const hash = createHash('sha256').update(body).digest('hex').slice(0, 12)
  mkdirSync(dirname(SPRITE), { recursive: true })
  writeFileSync(SPRITE, body)
  writeFileSync(BUILT, JSON.stringify({
    _comment: 'Written by web/scripts/fetch_icons8.mjs: the keys actually present in public/icons8-sprite.svg, the sprite content hash (cache key), and the platform.',
    platform, hash, fetchedAt: new Date().toISOString(), ids: built.sort(),
  }, null, 2) + '\n')
  if (flag('--pin')) writeFileSync(MANIFEST, JSON.stringify(manifest, null, 2) + '\n')
  console.log(`sprite: ${built.length} icons, hash ${hash}, ${Buffer.byteLength(body)} bytes; failures: ${failures.length}`)
  if (failures.length) process.exit(1)
}

// Run only as a script; importable for its helpers (tests, the interim builder).
if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  main().catch((err) => { console.error(err?.message || err); process.exit(1) })
}
