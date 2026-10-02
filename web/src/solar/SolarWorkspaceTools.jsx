import { useLayoutEffect, useMemo, useRef, useState } from 'react'
import { authHeaders, config, noteUnauthorized } from '../api.js'
import { createSolarLandxmlClient } from './solarLandxmlClient.js'
import SolarLandxmlUpload from './SolarLandxmlUpload.jsx'
import './solarFlow.css'

const FLOW_PANELS = Object.freeze({
  rooftop: Object.freeze([]),
  'ground-electrical': Object.freeze([]),
  'ground-physical': Object.freeze(['landxml']),
  'solaredge-import': Object.freeze([]),
  'pvcase-tutorial': Object.freeze([]),
})

// drawingVersion, getCheckoutCapability and onDrawingVersionChanged are reserved for
// drawing-writing workspace tools. A terrain import changes only the physical head.
export default function SolarWorkspaceTools({
  drawingId, projectId = null, drawingVersion, flow, checkoutHeld, busy,
  getCheckoutCapability, onPhysicalHeadChanged, onDrawingVersionChanged, transport,
}) {
  const client = useMemo(() => createSolarLandxmlClient({
    fetchImpl: transport?.fetchImpl ?? ((...args) => fetch(...args)),
    apiBase: config.apiBase,
    headers: transport?.headers ?? (() => ({ 'X-Tenant-Id': config.tenant, ...authHeaders() })),
    onResponse: transport?.onResponse ?? ((response, url, sentAuth) => noteUnauthorized(response, url, sentAuth)),
  }), [transport])

  return (
    <ToolsForScope
      key={JSON.stringify([drawingId ?? null, projectId ?? null])}
      drawingId={drawingId}
      projectId={projectId}
      flow={flow}
      checkoutHeld={checkoutHeld}
      busy={busy}
      client={client}
      onPhysicalHeadChanged={onPhysicalHeadChanged}
    />
  )
}

function ToolsForScope({ drawingId, projectId, flow, checkoutHeld, busy, client, onPhysicalHeadChanged }) {
  const [open, setOpen] = useState(false)
  const [physicalHead, setPhysicalHead] = useState(null)
  const [announcement, setAnnouncement] = useState('')
  const trigger = useRef(null)
  const panel = useRef(null)
  const activeScope = useRef(false)
  const pendingImport = useRef(null)
  const hasLandxml = !!drawingId && FLOW_PANELS[flow]?.includes('landxml') === true

  useLayoutEffect(() => {
    activeScope.current = true
    return () => {
      activeScope.current = false
      pendingImport.current = null
    }
  }, [])

  useLayoutEffect(() => {
    if (open && hasLandxml) panel.current?.querySelector('input[type="file"]')?.focus()
  }, [open, hasLandxml])

  const upload = useMemo(() => (request) => {
    const started = { drawingId, projectId }
    pendingImport.current = started
    return client.uploadLandxml(request)
  }, [client, drawingId, projectId])

  function onImported(value) {
    const started = pendingImport.current
    if (!activeScope.current || !started || started.drawingId !== drawingId
      || started.projectId !== projectId || value?.drawing_id !== drawingId) return
    pendingImport.current = null
    setPhysicalHead(value.head)
    setAnnouncement(value.created
      ? 'Terrain imported for this drawing.'
      : 'This terrain was already imported, so nothing changed.')
    onPhysicalHeadChanged?.(value)
  }

  if (!hasLandxml) return null
  return (
    <section className="solar-workspace-tools" aria-label="Solar workspace tools"
      data-physical-head-index={physicalHead?.index ?? undefined}>
      <button type="button" ref={trigger} aria-expanded={open} onClick={() => setOpen(true)}>
        Import LandXML terrain
      </button>
      {open && (
        <div className="solar-workspace-panel" ref={panel}>
          <SolarLandxmlUpload drawingId={drawingId} projectId={projectId} upload={upload}
            checkoutHeld={checkoutHeld} busy={busy} onImported={onImported}
            onClose={() => {
              pendingImport.current = null
              setOpen(false)
              trigger.current?.focus()
            }} />
        </div>
      )}
      <p className="solar-workspace-announce" aria-live="polite" aria-atomic="true">{announcement}</p>
    </section>
  )
}
