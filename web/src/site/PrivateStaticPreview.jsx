import { useEffect, useRef, useState } from 'react'
import { authHeaders, getStoredOrgId, noteUnauthorized } from '../api.js'
import { getStoredActorBindingId } from '../projects/api.js'
import { loadPrivateStaticAssets } from './privateStaticAssets.js'
import { buildOpaqueStaticDocument, PRIVATE_FRAME_SANDBOX } from './opaqueStaticDocument.js'
import { attachChartStateBridge, copyChartState } from './privatePreviewState.js'

async function envelope(response) {
  if (!response.ok || !response.headers.get('Content-Type')?.startsWith('application/json') || !response.body) throw new Error('private_preview_unavailable')
  const reader = response.body.getReader(), chunks = []
  let total = 0
  try { for (;;) { const part = await reader.read(); if (part.done) break; total += part.value.length; if (total > 3 * 1024 * 1024) throw new Error('private_preview_unavailable'); chunks.push(part.value) } }
  finally { await reader.cancel().catch(() => {}); reader.releaseLock() }
  const bytes = new Uint8Array(total); let offset = 0
  for (const part of chunks) { bytes.set(part, offset); offset += part.length }
  return JSON.parse(new TextDecoder('utf-8', { fatal:true }).decode(bytes))
}
export default function PrivateStaticPreview({ projectId, result, isCurrent, initialState, onStateChange }) {
  const [display, setDisplay] = useState(null), frame = useRef(null), identity = useRef(null), lastState = useRef(null)
  const stateCallback = useRef(onStateChange), initial = useRef(initialState)
  stateCallback.current = onStateChange; initial.current = initialState
  useEffect(() => {
    const controller = new AbortController(), org = getStoredOrgId(), actor = getStoredActorBindingId()
    const held = { result, projectId }, nonce = crypto.randomUUID()
    identity.current = null; setDisplay(null)
    let active = true
    const owned = () => active && isCurrent() && org === getStoredOrgId() && actor === getStoredActorBindingId()
    const current = () => !controller.signal.aborted && owned()
    const receive = event => {
      const id = identity.current, m = event.data
      if (!current() || !id || event.source !== frame.current?.contentWindow || event.origin !== 'null' ||
          !m || m.protocol !== 'leaf.chart-state.v1' || m.type !== 'change' || m.nonce !== id.nonce || m.jobId !== id.jobId || m.artifactId !== id.artifactId) return
      try { const state = copyChartState(m.state); lastState.current = JSON.stringify(state); void Promise.resolve(stateCallback.current?.(state)).catch(() => {}) } catch { /* Invalid messages cannot update parent state. */ }
    }
    addEventListener('message', receive)
    const timeout = setTimeout(() => { controller.abort(); if (owned()) setDisplay({ held,error:true }) }, 120000)
    void (async () => {
      const c = result?.candidate, p = result?.preview
      if (result?.schema !== 'leaf.browser-candidate-preview.v1' || !c || !p ||
          !/^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/.test(c.sourceRequestId) ||
          !Number.isSafeInteger(c.version) || c.version < 1 || !/^[0-9a-f]{64}$/.test(c.receiptDigest) ||
          c.sourceCommit !== p.sourceCommit || c.sourceTree !== p.sourceTree || !current()) throw new Error('private_preview_unavailable')
      const loaded = await loadPrivateStaticAssets({ preview:p, signal:controller.signal, isCurrent:current,
        requestAsset:async witness => {
          if (!current()) throw new Error('private_preview_unavailable')
          const headers = { ...authHeaders() }; if (!headers.Authorization) throw new Error('private_preview_unavailable')
          if (org) headers['X-Org-Id'] = org
          if (actor) headers['X-Actor-Binding-Id'] = actor
          const query = new URLSearchParams({ source_commit:c.sourceCommit,source_tree:c.sourceTree,receipt_digest:c.receiptDigest,version:String(c.version),artifact_id:witness.artifactId,manifest_sha256:witness.manifestSha256,path:witness.path })
          const path = `/api/projects/${encodeURIComponent(projectId)}/browser/source/requests/${encodeURIComponent(c.sourceRequestId)}/preview/${encodeURIComponent(witness.jobId)}/asset?${query}`
          const response = noteUnauthorized(await fetch(path,{ method:'GET',headers,credentials:'same-origin',redirect:'error',cache:'no-store',signal:controller.signal }),path,headers.Authorization)
          if (!current()) throw new Error('private_preview_unavailable')
          return envelope(response)
        } })
      if (!current()) return
      const id = Object.freeze({ nonce,jobId:loaded.jobId,artifactId:loaded.artifactId })
      const state = initial.current == null ? null : copyChartState(initial.current)
      const html = attachChartStateBridge(buildOpaqueStaticDocument(loaded), id, state)
      if (!current()) return
      identity.current = id; lastState.current = JSON.stringify(state); setDisplay({ held, html })
    })().catch(() => { if (current()) setDisplay({ held, error:true }) }).finally(() => clearTimeout(timeout))
    return () => { active = false; controller.abort(); clearTimeout(timeout); removeEventListener('message',receive); identity.current = null }
  }, [projectId,result,isCurrent])
  useEffect(() => {
    if (!identity.current || !isCurrent() || initialState == null) return
    try { const state = copyChartState(initialState), raw = JSON.stringify(state); if (raw === lastState.current) return; lastState.current = raw; frame.current?.contentWindow?.postMessage({ ...identity.current,protocol:'leaf.chart-state.v1',type:'restore',state }, '*') } catch { /* Invalid restoration is ignored. */ }
  }, [initialState,isCurrent])
  const shown = isCurrent() && display?.held.result === result && display?.held.projectId === projectId ? display : null
  if (shown?.error) return <p role="alert">Private preview is unavailable. Reopen the saved source to try again.</p>
  if (!shown) return <p role="status">Loading private preview...</p>
  return <iframe ref={frame} title="Private app preview" sandbox={PRIVATE_FRAME_SANDBOX} referrerPolicy="no-referrer" credentialless="" srcDoc={shown.html} style={{ border:0,minWidth:1200,width:'100%',minHeight:500,height:'100%' }} />
}
