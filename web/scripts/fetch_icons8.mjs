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
import { readFileSync, writeFileSync, mkdirSync, existsSync } from 'node:fs'
import { createHash } from 'node:crypto'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'
import { homedir } from 'node:os'

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..')
const MANIFEST = join(ROOT, 'src', 'assets', 'icons8', 'manifest.json')
const BUILT = join(ROOT, 'src', 'assets', 'icons8', 'built.json')
const SPRITE = join(ROOT, 'public', 'icons8-sprite.svg')
const API = 'https://api-icons.icons8.com/publicApi/icons'
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

// Normalize to a <symbol>: keep the viewBox, drop the root's width/height and
// every hard-coded fill (currentColor tints the chrome), strip metadata.
function toSymbol(key, svgText) {
  const open = svgText.match(/<svg\b[^>]*>/i)
  if (!open) return ''
  const attrs = open[0]
  const viewBox = (attrs.match(/viewBox="([^"]+)"/i) || [])[1] || '0 0 24 24'
  let inner = svgText.slice(open.index + attrs.length).replace(/<\/svg>\s*$/i, '')
  inner = inner
    .replace(/<\?xml[^>]*>/g, '')
    .replace(/<!DOCTYPE[^>]*>/g, '')
    .replace(/<(title|desc|metadata)\b[\s\S]*?<\/\1>/gi, '')
    .replace(/<script\b[\s\S]*?<\/script>/gi, '')
    .replace(/\son[a-z]+="[^"]*"/gi, '')
    .replace(/\sfill="(?!none)[^"]*"/gi, '')
    .replace(/fill:\s*(?!none)[^;"']+;?/gi, '')
    .replace(/\s{2,}/g, ' ')
    .trim()
  if (!inner || /<(script|foreignObject|image)\b/i.test(inner)) return ''
  return `<symbol id="i8-${key}" viewBox="${viewBox}">${inner}</symbol>`
}

async function main() {
  const manifest = JSON.parse(readFileSync(MANIFEST, 'utf8'))
  const platform = String(manifest.platform || '')
  const entries = Object.entries(manifest.icons || {}).filter(([k]) => !only || only.has(k))
  if (!platform || entries.length === 0) throw new Error('manifest has no platform or no icons')
  if (entries.length > MAX_ICONS) throw new Error(`manifest lists ${entries.length} icons, cap is ${MAX_ICONS}`)
  const token = readToken()
  if (!token) {
    console.error('no ICONS8_API_TOKEN in the credential store or environment; nothing fetched')
    process.exit(2)
  }
  const symbols = []
  const built = []
  const failures = []
  for (const [key, spec] of entries) {
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

main().catch((err) => { console.error(err?.message || err); process.exit(1) })
