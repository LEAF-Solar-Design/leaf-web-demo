import { execFileSync } from 'node:child_process'
import { existsSync, readdirSync, readFileSync, statSync } from 'node:fs'
import { createServer } from 'node:http'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

export const STUDIO_PATHS = Object.freeze([
  '/', '/try', '/app', '/app/leaf-platform', '/ty', '/sheets',
  ...['l-000', 'g-000', 'a-101', 'a-102', 'e-401', 'c-201', 's-501'].map((code) => `/sheets/${code}`),
])

// These are browser configuration, never an open-ended VITE_* prefix copy.
// Keep in sync with consumers in src, including the proof bridge's computed key.
export const PUBLIC_ENV_KEYS = Object.freeze([
  'VITE_API_BASE', 'VITE_AUTH0_AUDIENCE', 'VITE_AUTH0_CLIENT_ID', 'VITE_AUTH0_DOMAIN',
  'VITE_CAD_EDIT', 'VITE_CAT_PROOF', 'VITE_INTEGRATIONS_API_ORIGIN',
  'VITE_IOS_SURFACE', 'VITE_LAB_HOST_ORIGINS', 'VITE_LAB_PREVIEW',
  'VITE_LEAF_PROOF_BRIDGE', 'VITE_LIFECYCLE_UI', 'VITE_MOCK',
  'VITE_SOLAR_FLOW_RAIL', 'VITE_SOLAR_SETTINGS_FORM', 'VITE_STARTUP_FETCH_TIMEOUT_MS',
  'VITE_TELEMETRY_DISABLED', 'VITE_TENANT_ID', 'VITE_WORLD_SPACE_BOARD',
])

export function publicEnvironment(env, development) {
  return {
    ...Object.fromEntries(PUBLIC_ENV_KEYS.filter((key) => env[key] !== undefined).map((key) => [key, String(env[key])])),
    DEV: development, PROD: !development, MODE: development ? 'development' : 'production', BASE_URL: '/',
  }
}

export function environmentDefinitions(env, development) {
  const exposed = publicEnvironment(env, development)
  // Exact properties retain literal flag folding; the whole object supports
  // existing consumers that pass import.meta.env to another function.
  return {
    'import.meta.env': JSON.stringify(exposed),
    ...Object.fromEntries([...PUBLIC_ENV_KEYS, 'DEV', 'PROD', 'MODE', 'BASE_URL'].map((key) => [
      `import.meta.env.${key}`, exposed[key] === undefined ? 'undefined' : JSON.stringify(exposed[key]),
    ])),
  }
}

export function sourceIdentity(webRoot, env = process.env) {
  if (env.LEAF_SOURCE_SHA?.trim()) return env.LEAF_SOURCE_SHA.trim()
  try {
    return execFileSync('git', ['rev-parse', 'HEAD'], { cwd: webRoot, encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'] }).trim()
  } catch {
    return 'unknown'
  }
}

export function elementRefEnabled(env = process.env) {
  const raw = env.LEAF_ELEMENT_REF_STAMP
  if (raw === undefined || raw === '' || raw === '1') return true
  if (raw === '0') return false
  throw new Error('LEAF_ELEMENT_REF_STAMP must be 0 or 1')
}

export function emittedScripts(distDir) {
  const scripts = []
  function visit(directory) {
    for (const entry of readdirSync(directory, { withFileTypes: true })) {
      const filename = path.join(directory, entry.name)
      if (entry.isDirectory()) visit(filename)
      else if (entry.isFile() && entry.name.endsWith('.js')) scripts.push(path.relative(distDir, filename).split(path.sep).join('/'))
    }
  }
  visit(distDir)
  return scripts.sort()
}

export function bundleManifest(distDir, sourceSha) {
  const scripts = emittedScripts(distDir)
  if (!existsSync(path.join(distDir, 'index.html')) || !scripts.some((asset) => asset.startsWith('_next/'))) {
    throw new Error('Next export must contain index.html and emitted Next scripts')
  }
  const workers = scripts.filter((asset) => {
    const code = readFileSync(path.join(distDir, asset), 'utf8')
    return code.includes('engine_unavailable:') && code.includes('documentLoaded')
  })
  return {
    schema: 'leaf.web-bundle.v1', framework: 'next', source_sha: sourceSha,
    scripts, ...(workers.length ? { worker_assets: workers } : {}),
  }
}

// Static exports cannot use `next start`. Preview the emitted Next bundle
// with the same root-shell fallback as nginx, without a Vite runtime.
if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url) && process.argv.includes('--preview')) {
  const distDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../dist')
  if (!existsSync(path.join(distDir, 'index.html'))) throw new Error('Run npm run build before preview')
  const portIndex = process.argv.indexOf('--port')
  const port = Number(portIndex === -1 ? 5175 : process.argv[portIndex + 1])
  if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error('Invalid preview port')
  const types = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8', '.json': 'application/json', '.wasm': 'application/wasm', '.woff2': 'font/woff2', '.svg': 'image/svg+xml', '.png': 'image/png', '.jpg': 'image/jpeg', '.webp': 'image/webp', '.ico': 'image/x-icon' }
  createServer((request, response) => {
    try {
      const pathname = decodeURIComponent(new URL(request.url, 'http://localhost').pathname)
      const requested = path.resolve(distDir, `.${pathname}`)
      if (!requested.startsWith(distDir + path.sep) && requested !== distDir) {
        response.writeHead(403).end()
        return
      }
      const candidates = [requested, `${requested}.html`, path.join(requested, 'index.html')]
      let filename = candidates.find((candidate) => existsSync(candidate) && statSync(candidate).isFile())
      if (!filename && !path.extname(pathname)) filename = path.join(distDir, 'index.html')
      if (!filename) { response.writeHead(404).end(); return }
      response.setHeader('Content-Type', types[path.extname(filename)] || 'application/octet-stream')
      if (request.method === 'HEAD') response.end()
      else response.end(readFileSync(filename))
    } catch {
      response.writeHead(400).end()
    }
  }).listen(port, '127.0.0.1', () => console.log(`Next static export preview: http://127.0.0.1:${port}`))
}
