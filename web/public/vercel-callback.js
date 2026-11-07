// Standalone: no Studio bootstrap, network requests, storage or opener dependency.
export const vercelChannelName = (state) => `studio-vercel-${state}`
export function parseVercelCallback(search) {
  if (typeof search !== 'string' || search.length > 8192) return null
  const query = new URLSearchParams(search)
  if (['state', 'code', 'configurationId', 'error'].some((key) => query.getAll(key).length > 1)) return null
  const one = (key) => query.getAll(key).length === 1 ? query.get(key) : null
  const state = one('state')
  if (!/^[A-Za-z0-9_-]{43}(?![\s\S])/.test(state || '')) return null
  if (query.has('error')) return { state, outcome: 'denied' }
  const code = one('code'), configurationId = one('configurationId')
  if (!/^[A-Za-z0-9_-]{1,2048}(?![\s\S])/.test(code || '') || !/^[A-Za-z0-9_-]{1,256}(?![\s\S])/.test(configurationId || '')) return null
  return { state, outcome: 'authorized', code, configurationId }
}
export function deliverVercelCallback(page) {
  let search = page.location.search
  // Remove callback material before parsing it or opening a channel.
  page.history.replaceState(null, '', '/vercel-callback.html')
  const result = parseVercelCallback(search)
  search = ''
  if (!result || typeof page.BroadcastChannel !== 'function') return false
  let channel
  try {
    channel = new page.BroadcastChannel(vercelChannelName(result.state))
    channel.postMessage(result)
    return true
  } catch { return false }
  finally { channel?.close(); for (const key of Object.keys(result)) delete result[key] }
}
if (typeof window !== 'undefined') deliverVercelCallback(window)
