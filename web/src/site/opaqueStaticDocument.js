// Consumes already verified loader output in the trusted parent. The returned
// srcDoc requires an iframe sandbox="allow-scripts" (never allow-same-origin).
const POLICY = "default-src 'none'; script-src 'unsafe-inline' data:; style-src 'unsafe-inline' data:; img-src data:; font-src data:; connect-src 'none'; frame-src 'none'; worker-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'"
const fail = () => { throw new Error('private_frame_unavailable') }
const base = 'https://leaf-static.invalid/'
function encode(bytes) {
  let binary = ''
  for (let i = 0; i < bytes.length; i += 16384) binary += String.fromCharCode(...bytes.subarray(i, i + 16384))
  return btoa(binary)
}
// Runs only inside the opaque frame. Its registry contains verified bytes, no
// authority, headers, tokens, network origin or parent API callable by content.
function bootstrap(registry) {
  const root = 'https://leaf-static.invalid/'
  // An opaque srcDoc has no writable same-origin history URL. Keep routing
  // state inside this frame instead of relaxing its browser origin boundary.
  let state = null
  const historyState = (value, title, target) => {
    if (target != null) { const url = new URL(String(target), root); if (url.origin !== new URL(root).origin) throw new Error('private_navigation_denied') }
    state = structuredClone(value)
  }
  Object.defineProperty(History.prototype, 'state', { configurable: true, get() { return state } })
  History.prototype.replaceState = historyState
  History.prototype.pushState = historyState
  const known = new Set(Object.values(registry))
  const resolve = value => {
    const text = String(value)
    if (known.has(text)) return text
    const url = new URL(text, root)
    if (url.origin !== new URL(root).origin || !Object.hasOwn(registry, url.pathname)) throw new Error('private_asset_missing')
    return registry[url.pathname]
  }
  for (const [prototype, property] of [[HTMLScriptElement.prototype, 'src'], [HTMLLinkElement.prototype, 'href'], [HTMLImageElement.prototype, 'src'], [HTMLSourceElement.prototype, 'src']]) {
    const descriptor = Object.getOwnPropertyDescriptor(prototype, property)
    if (descriptor?.set) Object.defineProperty(prototype, property, { ...descriptor, set(value) { descriptor.set.call(this, resolve(value)) } })
  }
  const attribute = Element.prototype.setAttribute
  Element.prototype.setAttribute = function (name, value) {
    const field = String(name).toLowerCase()
    if ((['SCRIPT','IMG','SOURCE'].includes(this.tagName) && field === 'src') || (this.tagName === 'LINK' && field === 'href')) value = resolve(value)
    return attribute.call(this, name, value)
  }
  // Static Next data fetches are answered from the immutable registry, never
  // forwarded to Studio. No network fallback for absent paths or POST writes.
  window.fetch = async (input, options = {}) => {
    const method = options.method || (typeof input === 'object' && input.method) || 'GET'
    if (method !== 'GET') throw new Error('private_fetch_denied')
    const uri = resolve(typeof input === 'string' ? input : input.url)
    const comma = uri.indexOf(',')
    const body = Uint8Array.from(atob(uri.slice(comma + 1)), c => c.charCodeAt(0))
    return new Response(body, { headers: { 'Content-Type': uri.slice(5, uri.indexOf(';base64,')) } })
  }
}

export function buildOpaqueStaticDocument(loaded) {
  if (!loaded || !Array.isArray(loaded.files) || !loaded.files.length || loaded.files.length > 4096) fail()
  const registry = Object.create(null)
  let total = 0
  for (const file of loaded.files) {
    if (typeof file.path !== 'string' || !/^\/(?:[A-Za-z0-9_.-]+\/)*[A-Za-z0-9_.-]+$/.test(file.path) || /[\u0000-\u001f\u007f]/.test(file.path) || file.path.split('/').some(p => p === '.' || p === '..') || Object.hasOwn(registry, file.path) ||
        (!ArrayBuffer.isView(file.bytes) || Object.prototype.toString.call(file.bytes) !== "[object Uint8Array]") || file.bytes.length > 2 * 1024 * 1024 || typeof file.contentType !== 'string' || !/^[a-z0-9.+-]+\/[a-z0-9.+-]+(?:; charset=utf-8)?$/i.test(file.contentType) || /[\r\n]/.test(file.contentType)) fail()
    total += file.bytes.length
    if (total > 32 * 1024 * 1024) fail()
    registry[file.path] = `data:${file.contentType};base64,${encode(file.bytes)}`
  }
  // Rewrite CSS references only to verified supplied assets; no remote URLs.
  for (const file of loaded.files.filter(f => f.contentType.startsWith('text/css'))) {
    const css = new TextDecoder('utf-8', { fatal: true }).decode(file.bytes).replace(/url\(\s*(["']?)([^"')]+)\1\s*\)/g, (_, quote, value) => {
      const url = new URL(value.trim(), new URL(file.path, base))
      if (url.origin !== new URL(base).origin || !Object.hasOwn(registry, url.pathname)) fail()
      return `url("${registry[url.pathname]}")`
    })
    if (/@import/i.test(css)) fail()
    registry[file.path] = `data:text/css;base64,${encode(new TextEncoder().encode(css))}`
  }
  const index = loaded.files.find(f => f.path === '/index.html')
  if (!index || !index.contentType.startsWith('text/html')) fail()
  const doc = new DOMParser().parseFromString(new TextDecoder('utf-8', { fatal: true }).decode(index.bytes), 'text/html')
  for (const node of doc.querySelectorAll('base,meta[http-equiv],iframe,object,embed')) node.remove()
  for (const node of doc.querySelectorAll('script[src],link[href],img[src],source[src]')) {
    const key = node.tagName === 'LINK' ? 'href' : 'src'
    if (node.tagName === 'LINK' && !['stylesheet','preload','modulepreload'].includes(node.rel)) { node.remove(); continue }
    const url = new URL(node.getAttribute(key), base)
    if (url.origin !== new URL(base).origin || !Object.hasOwn(registry, url.pathname)) fail()
    node.setAttribute(key, registry[url.pathname])
    node.removeAttribute('integrity'); node.removeAttribute('crossorigin')
  }
  const policy = doc.createElement('meta'); policy.httpEquiv = 'Content-Security-Policy'; policy.content = POLICY
  const script = doc.createElement('script'); script.textContent = `(${bootstrap.toString()})(${JSON.stringify(registry)});`
  doc.head.prepend(script); doc.head.prepend(policy)
  return '<!doctype html>' + doc.documentElement.outerHTML
}
export const PRIVATE_FRAME_SANDBOX = 'allow-scripts'
