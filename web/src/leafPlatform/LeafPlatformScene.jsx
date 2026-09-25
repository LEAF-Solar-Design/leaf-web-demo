import React, { useEffect, useRef, useState } from 'react'
import { getStoredOrgId, listProjects, openProject } from '../api.js'
import { isSignedIn, login } from '../auth.js'
import { getLeafHostBridge } from './hostBridge.js'
import './leafPlatform.css'

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i
const isUuid = (value) => typeof value === 'string' && UUID.test(value)
const sameId = (a, b) => typeof a === 'string' && typeof b === 'string' && a.toLowerCase() === b.toLowerCase()
const initial = { status: 'unavailable', ready: null, selectedObjectId: null, selectedHandles: null }

export default function LeafPlatformScene() {
  const [bridge] = useState(getLeafHostBridge)
  const [state, setState] = useState(initial)
  const [projects, setProjects] = useState([])
  const [projectId, setProjectId] = useState('')
  const [versions, setVersions] = useState([])
  const [versionId, setVersionId] = useState('')
  const [catalogMessage, setCatalogMessage] = useState('')
  const [versionMessage, setVersionMessage] = useState('')
  const [actionMessage, setActionMessage] = useState('')
  const [binding, setBinding] = useState(false)
  const [working, setWorking] = useState(false)
  const actionInFlight = useRef(false)
  const signedIn = isSignedIn()
  const orgId = getStoredOrgId()

  useEffect(() => {
    const unsubscribe = bridge.subscribe(setState)
    bridge.start()
    return () => { unsubscribe(); bridge.stop() }
  }, [bridge])

  useEffect(() => {
    if (!signedIn || !['unbound', 'connected'].includes(state.status)) return undefined
    if (state.status === 'connected' && (!isUuid(orgId) || !sameId(state.ready.platformTenantId, orgId))) return undefined
    let live = true
    setProjects([])
    setProjectId('')
    setVersions([])
    setVersionId('')
    if (!isUuid(orgId)) {
      setCatalogMessage('Choose a workspace in Studio, then reopen this palette.')
      return undefined
    }
    setCatalogMessage('Loading projects.')
    Promise.resolve().then(() => listProjects(orgId)).then((items) => {
      if (!live) return
      const available = Array.isArray(items) ? items.filter((item) => isUuid(item?.project_id)) : []
      setProjects(available)
      setCatalogMessage(available.length ? '' : 'This workspace has no projects yet.')
    }).catch(() => {
      if (live) setCatalogMessage('Projects could not be loaded. Reopen this palette to try again.')
    })
    return () => { live = false }
  }, [state.status, state.ready?.platformTenantId, signedIn, orgId])

  useEffect(() => {
    setVersions([])
    setVersionId('')
    setVersionMessage('')
    if (state.status !== 'unbound' || !signedIn || !isUuid(orgId) || !projectId) return undefined
    let live = true
    setVersionMessage('Loading drawing versions.')
    Promise.resolve().then(() => openProject(projectId, orgId)).then((project) => {
      if (!live) return
      const items = project?.drawing_versions
      const available = Array.isArray(items) ? items.filter((item) =>
        [item?.version_id, item?.drawing_id, item?.project_id, item?.org_id].every(isUuid) &&
        sameId(item.org_id, orgId) && sameId(item.project_id, projectId)) : []
      setVersions(available)
      setVersionMessage(available.length ? '' : 'This project has no drawing versions available to connect.')
    }).catch(() => {
      if (live) setVersionMessage('Drawing versions could not be loaded. Choose the project again to try.')
    })
    return () => { live = false }
  }, [projectId, orgId, signedIn, state.status])

  useEffect(() => {
    setBinding(false)
    setActionMessage('')
  }, [state.ready])

  useEffect(() => {
    if (state.status !== 'unbound' || (state.bindingResult && state.bindingResult !== 'Waiting for confirmation in AutoCAD.')) {
      setBinding(false)
    }
  }, [state.status, state.bindingResult])

  async function connect(chosenVersionId) {
    const version = versions.find((item) => item.version_id === chosenVersionId)
    if (!version || binding || actionInFlight.current) return
    actionInFlight.current = true
    setBinding(true)
    setActionMessage('')
    try {
      await bridge.bindDrawing({
        platformTenantId: version.org_id, projectId: version.project_id,
        drawingId: version.drawing_id, drawingVersionId: version.version_id,
      })
    } catch {
      setBinding(false)
      setActionMessage('The drawing could not be connected. Please try again.')
    } finally { actionInFlight.current = false }
  }

  async function runAction(action) {
    if (actionInFlight.current) return
    actionInFlight.current = true
    setWorking(true)
    setActionMessage('')
    try {
      await action()
    } catch {
      setActionMessage('The request could not be completed. Please try again.')
    } finally {
      actionInFlight.current = false
      setWorking(false)
    }
  }

  const otherWorkspace = state.status === 'connected' && signedIn && isUuid(orgId) && !sameId(state.ready.platformTenantId, orgId)
  const boundProject = projects.find((project) => sameId(project.project_id, state.ready?.projectId))
  const selectedTarget = state.selectedObjectId || (state.selectedHandles?.length ? { objectHandles: state.selectedHandles } : null)
  const selectedLabel = state.selectedObjectId || (state.selectedHandles?.length
    ? `${state.selectedHandles[0]}${state.selectedHandles.length > 1 ? ` and ${state.selectedHandles.length - 1} more` : ''}` : 'None')

  return (
    <main className="leaf-platform" aria-labelledby="leaf-platform-title">
      <header>
        <p className="leaf-platform-context">Studio in AutoCAD</p>
        <h1 id="leaf-platform-title">Drawing connection</h1>
      </header>
      {state.status === 'unavailable' && <section>
        <p>Open this page from AutoCAD with the LEAFPLATFORM command.</p>
        <a href="/app">Back to Studio</a>
      </section>}
      {state.status === 'connecting' && <p role="status">Waiting for AutoCAD.</p>}
      {state.status === 'unbound' && <section>
        <p>Connect this DWG to a drawing version in your workspace.</p>
        {!signedIn ? <button type="button" disabled={working} onClick={() => runAction(() => login())}>Sign in</button> : <>
          {catalogMessage && <p role="status">{catalogMessage}</p>}
          {projects.length > 0 && <label>
            Project
            <select value={projectId} disabled={binding} onChange={(event) => { setProjectId(event.target.value); setVersionId('') }}>
              <option value="">Choose a project</option>
              {projects.map((project) => <option key={project.project_id} value={project.project_id}>{project.name || 'Untitled project'}</option>)}
            </select>
          </label>}
          {versionMessage && <p role="status">{versionMessage}</p>}
          {versions.length > 0 && <>
            <label>
              Drawing version
              <select value={versionId} disabled={binding} onChange={(event) => setVersionId(event.target.value)}>
                <option value="">Choose a drawing version</option>
                {versions.map((version) => <option key={version.version_id} value={version.version_id}>
                  {`Version ${version.seq ?? '?'} (${version.drawing_id.slice(0, 8)})`}
                </option>)}
              </select>
            </label>
            <button type="button" disabled={!versionId || binding} onClick={() => connect(versionId)}>Connect drawing</button>
          </>}
        </>}
        {state.bindingResult && <p role="status">{state.bindingResult}</p>}
      </section>}
      {state.status === 'connected' && <section>
        {!signedIn ? <>
          <p>This DWG is connected. Sign in to use this connection.</p>
          <button type="button" disabled={working} onClick={() => runAction(() => login())}>Sign in</button>
        </> : !isUuid(orgId) ? <p>Choose a workspace in Studio, then reopen this palette.</p>
          : otherWorkspace ? <p>This DWG belongs to another workspace. Open that workspace in Studio to use this connection.</p> : <>
          <p>Connected to {boundProject?.name || state.ready.drawingId.slice(0, 8)}.</p>
          <p>Selected object: <span>{selectedLabel}</span></p>
          <div className="leaf-platform-actions">
            <button type="button" disabled={!selectedTarget || working} onClick={() => runAction(() => bridge.focusObject(selectedTarget, 'select'))}>Select</button>
            <button type="button" disabled={!selectedTarget || working} onClick={() => runAction(() => bridge.focusObject(selectedTarget, 'focus'))}>Zoom to</button>
          </div>
        </>}
      </section>}
      {actionMessage && <p role="status">{actionMessage}</p>}
    </main>
  )
}
