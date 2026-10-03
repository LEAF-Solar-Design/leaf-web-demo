import { execFile, spawn } from 'node:child_process'
import { createReadStream } from 'node:fs'
import { readFile, readdir, stat } from 'node:fs/promises'
import http from 'node:http'
import { dirname, extname, join, resolve, sep } from 'node:path'
import { fileURLToPath } from 'node:url'
import { promisify } from 'node:util'
import { shippedViteFlags, productionBundleCacheKey } from './buildFlags.mjs'
import { ensureEngineArtifacts } from './engineArtifacts.mjs'

const web = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const dist = join(web, 'dist')
const headerLimit = 16 * 1024
const bodyLimit = 32 * 1024 * 1024
const hopHeaders = new Set(['connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization', 'te', 'trailer', 'transfer-encoding', 'upgrade'])
const mime = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8', '.json': 'application/json', '.svg': 'image/svg+xml', '.png': 'image/png', '.ico': 'image/x-icon', '.wasm': 'application/wasm', '.woff2': 'font/woff2', '.dxf': 'application/dxf' }
let building

async function exists(path) {
  try { return await stat(path) } catch (error) {
    if (error.code === 'ENOENT') return null
    throw error
  }
}

async function newest(path) {
  const info = await exists(path)
  if (!info) return 0
  if (!info.isDirectory()) return info.mtimeMs
  const entries = await readdir(path, { withFileTypes: true })
  const times = await Promise.all(entries.filter((entry) => !entry.isSymbolicLink()).map((entry) => newest(join(path, entry.name))))
  return Math.max(info.mtimeMs, ...times)
}

// Call in global setup, before stack boot timing. Engine preparation also
// creates the deployment's pkg-web artifacts when the toolchain is available.
export async function prepareProductionBundle() {
  if (building) return building
  building = (async () => {
    const repo = resolve(web, '..')
    const flags = shippedViteFlags(await readFile(join(repo, 'deploy', 'Dockerfile.web'), 'utf8'))
    let engine_unavailable_reason = null
    if (flags.VITE_CAD_EDIT === '1') {
      const engine = await ensureEngineArtifacts(repo)
      if (!engine.ready) {
        flags.VITE_CAD_EDIT = '0'
        engine_unavailable_reason = engine.reason
      }
    }
    const cacheKey = productionBundleCacheKey(flags, engine_unavailable_reason)
    const vendorWorker = join(repo, 'vendor', 'acad' + 'rust-worker')
    const sourceMtime = Math.max(...await Promise.all([
      'src', 'public', 'vite-plugins', 'vite.config.js', 'index.html', 'package.json', 'package-lock.json',
    ].map((path) => newest(join(web, path)))), ...await Promise.all([
      newest(join(vendorWorker, 'worker-browser.mjs')), newest(join(vendorWorker, 'pkg-web')),
      newest(join(repo, 'scripts', 'stage_cad_engine.mjs')),
    ]))
    const marker = join(dist, '.leaf-walk-build.json')
    let cached
    try { cached = JSON.parse(await readFile(marker, 'utf8')) } catch (error) {
      if (error.code !== 'ENOENT' && !(error instanceof SyntaxError)) throw error
    }
    if (cached?.sourceMtime === sourceMtime && cached?.cacheKey === cacheKey && cached?.apiBase === '' && cached?.mock === false && cached?.authLive === false && await exists(join(dist, 'index.html')) && (flags.VITE_CAD_EDIT !== '1' || (await exists(join(dist, 'engine', 'engine.js')) && await exists(join(dist, 'engine', 'engine_bg.wasm'))))) return dist
    if (!await exists(join(web, 'node_modules'))) throw new Error('Production bundle needs web/node_modules; install the web prerequisites first')
    // npm's Windows shim needs cmd.exe; no user-controlled command is interpolated.
    const command = process.platform === 'win32' ? (process.env.ComSpec || 'cmd.exe') : 'npm'
    const args = process.platform === 'win32' ? ['/d', '/s', '/c', 'npm run build'] : ['run', 'build']
    const child = spawn(command, args, {
      cwd: web, detached: process.platform !== 'win32', windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'],
      env: { ...process.env, ...flags, VITE_API_BASE: '', VITE_MOCK: '0', VITE_TENANT_ID: 'demo-tenant', VITE_AUTH0_DOMAIN: '', VITE_AUTH0_CLIENT_ID: '', VITE_AUTH0_AUDIENCE: '' },
    })
    let output = ''
    for (const stream of [child.stdout, child.stderr]) stream.on('data', (chunk) => { output = (output + chunk).slice(-16000) })
    const timer = setTimeout(() => {
      if (!child.pid) return
      if (process.platform === 'win32') {
        const killer = spawn('taskkill', ['/PID', String(child.pid), '/T', '/F'], { windowsHide: true, stdio: 'ignore' })
        killer.on('error', () => {})
      } else {
        try { process.kill(-child.pid, 'SIGKILL') } catch (error) { if (error.code !== 'ESRCH') child.kill('SIGKILL') }
      }
    }, 300000)
    try {
      await new Promise((accept, reject) => {
        child.once('error', reject)
        child.once('close', (code) => code === 0 ? accept() : reject(new Error(`Production web build failed (${code}):\n${output}`)))
      })
    } finally { clearTimeout(timer) }
    if (!await exists(join(dist, 'index.html'))) throw new Error('Production build did not produce web/dist/index.html')
    if (flags.VITE_CAD_EDIT === '1') {
      await promisify(execFile)(process.execPath, [join(repo, 'scripts', 'stage_cad_engine.mjs')], { cwd: repo, timeout: 60000, windowsHide: true })
      if (!await exists(join(dist, 'engine', 'engine.js')) || !await exists(join(dist, 'engine', 'engine_bg.wasm'))) throw new Error('Production build did not stage the CAD engine')
    }
    const { writeFile } = await import('node:fs/promises')
    await writeFile(marker, JSON.stringify({ sourceMtime, apiBase: '', mock: false, authLive: false, flags, engine_unavailable_reason, cacheKey }) + '\n')
    return dist
  })()
  try { return await building } finally { building = undefined }
}

function headersWithoutHop(headers) {
  const denied = new Set([...hopHeaders, ...String(headers.connection || '').toLowerCase().split(',').map((key) => key.trim())])
  return Object.fromEntries(Object.entries(headers).filter(([key]) => !denied.has(key.toLowerCase())))
}

function reply(response, status, text) {
  if (response.headersSent) { response.destroy(); return }
  response.writeHead(status, { 'content-type': 'text/plain; charset=utf-8', 'cache-control': 'no-store' })
  response.end(text + '\n')
}

// vite.config.js currently has no server.proxy entries. These are the app's
// non-static route families; /api also carries the conversation and job SSE.
export const backendPaths = ['/api', '/internal', '/health', '/ready', '/openapi.json', '/docs', '/redoc']

export async function startSameOriginProxy({ port, appPort, bundleDir = dist, connectTimeoutMs = 5000, idleTimeoutMs = 30000, maxBodyBytes = bodyLimit }) {
  if (!Number.isInteger(port) || !Number.isInteger(appPort)) throw new TypeError('Proxy and app ports must be integers')
  if (!await exists(join(bundleDir, 'index.html'))) throw new Error(`Production bundle missing: ${bundleDir}/index.html; call prepareProductionBundle first`)
  const sockets = new Set()
  const upstreams = new Set()
  const server = http.createServer({ maxHeaderSize: headerLimit, headersTimeout: 10000, requestTimeout: 120000 }, (request, response) => {
    void handle(request, response).catch(() => reply(response, 500, 'Static bundle could not be served'))
  })
  server.maxHeadersCount = 100
  server.on('connection', (socket) => { sockets.add(socket); socket.on('close', () => sockets.delete(socket)) })
  server.on('clientError', (error, socket) => {
    if (socket.writable) socket.end(`HTTP/1.1 ${error.code === 'HPE_HEADER_OVERFLOW' ? '431 Request Header Fields Too Large' : '400 Bad Request'}\r\nConnection: close\r\nContent-Length: 0\r\n\r\n`)
  })

  async function handle(request, response) {
    let pathname
    try { pathname = decodeURIComponent(new URL(request.url, 'http://localhost').pathname) } catch {
      reply(response, 400, 'Invalid request path'); return
    }
    if (backendPaths.some((prefix) => pathname === prefix || pathname.startsWith(prefix + '/'))) {
      proxy(request, response)
      return
    }
    if (!['GET', 'HEAD'].includes(request.method)) { reply(response, 405, 'Static bundle accepts GET and HEAD'); return }
    const base = resolve(bundleDir)
    let file = resolve(base, '.' + pathname)
    if (pathname.includes('\0') || (file !== base && !file.startsWith(base + sep))) { reply(response, 400, 'Invalid static path'); return }
    let info = await exists(file)
    if (!info?.isFile()) {
      if (extname(pathname)) { reply(response, 404, 'Static asset not found'); return }
      file = join(base, 'index.html')
      info = await stat(file)
    }
    response.writeHead(200, { 'content-type': mime[extname(file)] || 'application/octet-stream', 'content-length': info.size, 'cache-control': 'no-cache' })
    if (request.method === 'HEAD') { response.end(); return }
    const stream = createReadStream(file)
    response.once('close', () => stream.destroy())
    stream.on('error', () => response.destroy())
    stream.pipe(response)
  }

  function proxy(request, response) {
    const declared = request.headers['content-length']
    if (declared !== undefined && (!/^\d+$/.test(declared) || Number(declared) > maxBodyBytes)) {
      reply(response, 413, 'Proxy request body exceeds limit'); request.resume(); return
    }
    const headers = headersWithoutHop(request.headers)
    headers.host = `127.0.0.1:${appPort}`
    headers['accept-encoding'] = 'identity'
    const upstream = http.request({ host: '127.0.0.1', port: appPort, method: request.method, path: request.url, headers, maxHeaderSize: headerLimit, agent: false })
    upstreams.add(upstream)
    let received = 0
    let upstreamResponse
    const connectTimer = setTimeout(() => upstream.destroy(new Error('connect timeout')), connectTimeoutMs)
    upstream.once('socket', (socket) => {
      socket.setNoDelay(true)
      socket.once('connect', () => clearTimeout(connectTimer))
    })
    upstream.setTimeout(idleTimeoutMs, () => upstream.destroy(new Error('upstream idle timeout')))
    upstream.once('close', () => { clearTimeout(connectTimer); upstreams.delete(upstream) })
    upstream.on('error', (error) => {
      request.unpipe(upstream)
      request.resume()
      reply(response, 502, `Stack upstream failed: ${error.code || error.message}`)
    })
    request.on('data', (chunk) => {
      received += chunk.length
      if (received > maxBodyBytes) {
        request.unpipe(upstream)
        reply(response, 413, 'Proxy request body exceeds limit')
        upstream.destroy()
        request.resume()
      }
    })
    request.on('aborted', () => upstream.destroy())
    response.on('close', () => { upstreamResponse?.destroy(); upstream.destroy() })
    upstream.on('response', (incoming) => {
      upstreamResponse = incoming
      const outgoing = headersWithoutHop(incoming.headers)
      outgoing['x-accel-buffering'] = 'no'
      response.writeHead(incoming.statusCode || 502, outgoing)
      response.socket?.setNoDelay(true)
      response.flushHeaders()
      incoming.on('error', () => response.destroy())
      // pipe forwards each write with backpressure; no collecting/compression.
      incoming.pipe(response)
    })
    request.pipe(upstream)
  }

  try {
    await new Promise((accept, reject) => { server.once('error', reject); server.listen(port, '127.0.0.1', accept) })
  } catch (error) {
    for (const socket of sockets) socket.destroy()
    throw error
  }
  let stopped
  return {
    server, baseURL: `http://127.0.0.1:${port}`,
    stop() {
      if (!stopped) stopped = new Promise((accept, reject) => {
        for (const upstream of upstreams) upstream.destroy()
        server.close((error) => error ? reject(error) : accept())
        for (const socket of sockets) socket.destroy()
      })
      return stopped
    },
  }
}
