import { estimatorOriginAllowed } from './estimatorOrigins.js'

const full = (regex, value) => typeof value === 'string' && regex.exec(value)?.[0] === value
const channelShape = value => full(/^[A-Za-z0-9_-]{43}$/, value)
const subjectShape = value => full(/^sh_[0-9a-f]{32}$/, value)
const sameChannel = (left, right) => {
  if (!channelShape(left) || !channelShape(right)) return false
  let difference = 0
  for (let index = 0; index < 43; index += 1) difference |= left.charCodeAt(index) ^ right.charCodeAt(index)
  return difference === 0
}
const tokenShape = value => typeof value === 'string' && value.length <= 2048
  && full(/^[A-Za-z0-9_-]{1,512}\.[A-Za-z0-9_-]{1,1024}\.[A-Za-z0-9_-]{300,700}$/, value)

// Only this ephemeral closure holds a channel. Tokens exist only in the mint
// continuation's local variable and are posted once; neither reaches React.
export function createEstimatorBridge({ frame, origin, mint, win = globalThis.window,
  now = Date.now, createdAt = now(), isCurrent = () => true,
  onState = () => {}, onRecreate = () => {} }) {
  if (!estimatorOriginAllowed(origin, win.location.origin)) throw new Error('Estimator origin refused')
  const peer = frame.contentWindow
  const request = new AbortController()
  let chan = null, incoming = -1, outgoing = 0, subh = null
  let ready = false, sent = false, stopped = false, ending = false, dropped = 0
  let endPromise, finishEnd, endTimer
  const drop = () => { dropped += 1 }
  const send = (type, fields = {}) => {
    peer.postMessage({ v: 1, type, chan: chan || '', seq: ++outgoing, ts: now(), ...fields }, origin)
  }
  const readyTimer = setTimeout(() => {
    if (!stopped && !ending && !ready) { stopped = true; request.abort(); onState('failed') }
  }, Math.max(0, createdAt + 15000 - now()))

  function recreate() {
    stopped = true
    clearTimeout(readyTimer)
    request.abort()
    onRecreate()
  }

  async function receive(event) {
    if (event.origin !== origin || event.source !== peer || (stopped && !ending)) { drop(); return }
    const data = event.data
    try {
      if (!data || typeof data !== 'object' || Array.isArray(data)
        || new TextEncoder().encode(JSON.stringify(data)).length > 8192) { drop(); return }
    } catch { drop(); return }
    const keys = data.type === 'estimator.ready' ? ['v', 'type', 'chan', 'seq', 'ts']
      : ['v', 'type', 'chan', 'seq', 'ts', 'state', 'ref', 'subh']
    if (Object.keys(data).some(key => !keys.includes(key)) || data.v !== 1 || !channelShape(data.chan)
      || !Number.isSafeInteger(data.seq) || data.seq < 0 || !Number.isSafeInteger(data.ts)
      || Math.abs(now() - data.ts) > 30000) { drop(); return }
    if (data.type === 'estimator.ready') {
      if (ready) {
        // A fresh bootstrap has a fresh channel/sequence. It can only cause
        // recreation, never another token from this instance.
        if (sameChannel(data.chan, chan) && data.seq <= incoming) { drop(); return }
        if (ending) { drop(); return }
        recreate(); return
      }
      if (now() - createdAt >= 15000 || (!ending && !isCurrent())) { drop(); return }
      ready = true
      chan = data.chan
      incoming = data.seq
      clearTimeout(readyTimer)
      if (ending) {
        try { send('studio.end') } catch { /* the end timeout still releases the frame */ }
        return
      }
      try {
        const result = await mint(chan, request.signal)
        if (stopped || ending || !isCurrent() || sent) return
        if (!result || result.estimator_origin !== origin || result.expires_in !== 60
          || !subjectShape(result.subh) || !tokenShape(result.token)) throw new Error()
        subh = result.subh
        sent = true
        send('studio.token', { token: result.token })
      } catch (error) {
        if (!stopped && !ending && isCurrent()) {
          stopped = true
          onState(error?.status === 403 || error?.status === 404 ? 'denied'
            : error?.status === 429 ? 'throttled' : 'failed')
        }
      }
      return
    }
    if (data.type !== 'estimator.session' || !sameChannel(data.chan, chan) || data.seq <= incoming
      || !['active', 'ended', 'failed'].includes(data.state)
      || (data.ref !== undefined && !full(/^[0-9a-f]{8}$/, data.ref))
      || (data.subh !== undefined && !subjectShape(data.subh))
      || (data.state === 'active' && !subjectShape(data.subh))) { drop(); return }
    incoming = data.seq
    if (ending) {
      if (data.state === 'ended') finishEnd?.()
      return
    }
    if (!isCurrent()) { drop(); return }
    if (data.state !== 'active' || !sent || data.subh !== subh) { recreate(); return }
    onState('ready')
  }
  win.addEventListener('message', receive)
  return {
    end() {
      if (endPromise) return endPromise
      ending = true
      request.abort()
      clearTimeout(readyTimer)
      endPromise = new Promise(resolve => {
        finishEnd = () => { clearTimeout(endTimer); ending = false; stopped = true; resolve() }
        endTimer = setTimeout(finishEnd, 1500)
        try { send('studio.end') } catch { /* the end timeout still releases the frame */ }
      })
      return endPromise
    },
    dispose() {
      stopped = true
      request.abort()
      clearTimeout(readyTimer)
      finishEnd?.()
      win.removeEventListener('message', receive)
      chan = null
      subh = null
    },
    dropped: () => dropped,
  }
}
