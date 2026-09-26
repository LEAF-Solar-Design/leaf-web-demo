import React, { useEffect, useRef, useState } from 'react'
import { getStoredOrgId, listProjects, openProject } from '../api.js'
import { isSignedIn, login } from '../auth.js'
import { getLeafHostBridge } from './hostBridge.js'
import './leafPlatform.css'

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i
const isUuid = (value) => typeof value === 'string' && UUID.test(value)
const sameId = (a, b) => typeof a === 'string' && typeof b === 'string' && a.toLowerCase() === b.toLowerCase()
const initial = { status: 'unavailable', ready: null, selectedObjectId: null, selectedHandles: null, lastCommand: null, helloSentAt: null }

function drawingName(version, drawings) {
  const name = drawings.find((drawing) => sameId(drawing?.drawing_id, version.drawing_id))?.name
  return typeof name === 'string' && name.trim() ? name : `Drawing ${version.drawing_id.slice(0, 8)}`
}

function versionLabel(version, drawings) {
  const date = typeof version.created_at === 'string' && version.created_at.trim()
    ? new Date(version.created_at) : null
  const suffix = date && !Number.isNaN(date.getTime()) ? `, ${date.toISOString().slice(0, 10)}` : ''
  return `Version ${version.seq ?? '?'}: ${drawingName(version, drawings)}${suffix}`
}

function StudioRecovery() {
  return <p><a href="/app">Open Leaf Automation Studio</a>{' '}Then run LEAFPLATFORM again in AutoCAD to come back.</p>
}

function commandMessage(outcome) {
  if (!outcome || outcome.status === 'superseded') return null
  if (outcome.status === 'applied') return outcome.action === 'select'
    ? 'Selected in AutoCAD.' : 'Zoomed to the selection in AutoCAD.'
  if (outcome.status === 'stale') return 'This drawing changed in AutoCAD. Select the objects again.'
  if (outcome.status === 'unknown') return 'AutoCAD did not answer. Check AutoCAD, then try again.'
  if (outcome.status === 'rejected') return outcome.reason === 'selection_apply_failed'
    ? 'AutoCAD could not find those objects. Select them again in AutoCAD.'
    : `AutoCAD did not run that request (${(outcome.reason || 'unknown reason').replaceAll('_', ' ')}).`
  return null
}

export default function LeafPlatformScene() {
  const [bridge] = useState(getLeafHostBridge)
  const [state, setState] = useState(initial)
  const [projects, setProjects] = useState([])
  const [projectId, setProjectId] = useState('')
  const [versions, setVersions] = useState([])
  const [drawings, setDrawings] = useState([])
  const [boundDetail, setBoundDetail] = useState(null)
  const [versionId, setVersionId] = useState('')
  const [catalogMessage, setCatalogMessage] = useState('')
  const [versionMessage, setVersionMessage] = useState('')
  const [projectsFailed, setProjectsFailed] = useState(false)
  const [versionsFailed, setVersionsFailed] = useState(false)
  const [projectsAttempt, setProjectsAttempt] = useState(0)
  const [versionsAttempt, setVersionsAttempt] = useState(0)
  const [helloAttempt, setHelloAttempt] = useState(0)
  const [helloTimedOut, setHelloTimedOut] = useState(false)
  const [actionMessage, setActionMessage] = useState('')
  const [binding, setBinding] = useState(false)
  const [working, setWorking] = useState(false)
  const [focusConnect, setFocusConnect] = useState(false)
  const headingRef = useRef(null)
  const connectRef = useRef(null)
  const bindingPending = useRef(false)
  const boundRequests = useRef(new Map())
  const actionInFlight = useRef(false)
  const previousConnection = useRef(initial)
  const signedIn = isSignedIn()
  const orgId = getStoredOrgId()
  const otherWorkspace = state.status === 'connected' && signedIn && isUuid(orgId) && !sameId(state.ready.platformTenantId, orgId)
  const boundProject = projects.find((project) => sameId(project.project_id, state.ready?.projectId))
  const chosenProject = projects.find((project) => sameId(project.project_id, projectId))
  const chosenVersion = versions.find((version) => version.version_id === versionId)
  const boundIdentity = state.status === 'connected' && signedIn && isUuid(orgId) && !otherWorkspace
    ? JSON.stringify([orgId, state.ready.projectId, state.ready.drawingId, state.ready.drawingVersionId].map((id) => id.toLowerCase())) : null

  useEffect(() => {
    const unsubscribe = bridge.subscribe(setState)
    bridge.start()
    return () => { actionInFlight.current = false; unsubscribe(); bridge.stop() }
  }, [bridge])

  useEffect(() => {
    if (!signedIn || !['unbound', 'connected'].includes(state.status)) return undefined
    if (state.status === 'connected' && (!isUuid(orgId) || !sameId(state.ready.platformTenantId, orgId))) return undefined
    let live = true
    setProjects([])
    setProjectId('')
    setVersions([])
    setVersionId('')
    setProjectsFailed(false)
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
      if (live) {
        setCatalogMessage('Projects could not be loaded. Please try again.')
        setProjectsFailed(true)
      }
    })
    return () => { live = false }
  }, [state.status, state.ready?.platformTenantId, signedIn, orgId, projectsAttempt])

  useEffect(() => {
    setVersions([])
    setDrawings([])
    setVersionId('')
    setVersionMessage('')
    setVersionsFailed(false)
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
      setDrawings(Array.isArray(project?.drawing_artifacts) ? project.drawing_artifacts : [])
      setVersionMessage(available.length ? '' : 'This project has no drawing versions available to connect.')
    }).catch(() => {
      if (live) {
        setVersionMessage('Drawing versions could not be loaded. Please try again.')
        setVersionsFailed(true)
      }
    })
    return () => { live = false }
  }, [projectId, orgId, signedIn, state.status, versionsAttempt])

  useEffect(() => {
    if (!boundIdentity) return undefined
    let live = true
    const [tenantId, boundProjectId, boundDrawingId, boundVersionId] = JSON.parse(boundIdentity)
    if (!boundRequests.current.has(boundIdentity)) {
      boundRequests.current.set(boundIdentity, Promise.resolve().then(() => openProject(boundProjectId, tenantId)).then((project) => {
        const version = Array.isArray(project?.drawing_versions) && project.drawing_versions.find((item) =>
          sameId(item?.version_id, boundVersionId) && sameId(item?.drawing_id, boundDrawingId) &&
          sameId(item?.project_id, boundProjectId) && sameId(item?.org_id, tenantId))
        return version ? `${drawingName(version, Array.isArray(project.drawing_artifacts) ? project.drawing_artifacts : [])}, Version ${version.seq ?? '?'}` : null
      }).catch(() => null))
    }
    boundRequests.current.get(boundIdentity).then((label) => {
      if (live) setBoundDetail({ identity: boundIdentity, label })
    })
    return () => { live = false }
  }, [boundIdentity])

  useEffect(() => {
    if (bindingPending.current && state.status === 'connected') headingRef.current?.focus()
    bindingPending.current = false
    actionInFlight.current = false
    setBinding(false)
    setWorking(false)
  }, [state.ready, state.status])

  useEffect(() => {
    const previous = previousConnection.current
    previousConnection.current = { status: state.status, fingerprint: state.ready?.documentFingerprint }
    if (state.status === 'connected') {
      setActionMessage(signedIn && isUuid(orgId) && !otherWorkspace
        ? 'DWG connected.' : '')
    } else if (state.status !== previous.status || state.ready?.documentFingerprint !== previous.fingerprint) {
      setActionMessage('')
    }
  }, [state.status, state.ready, boundProject?.name, signedIn, orgId, otherWorkspace])

  useEffect(() => {
    setHelloTimedOut(false)
    if (state.status !== 'connecting') return undefined
    setActionMessage('Waiting for AutoCAD.')
    const timer = setTimeout(() => {
      setHelloTimedOut(true)
      setActionMessage('AutoCAD has not answered yet. Check that AutoCAD is open, then try again.')
    }, Math.max(0, 10_000 - (Date.now() - (state.helloSentAt ?? Date.now()))))
    return () => clearTimeout(timer)
  }, [state.status, state.helloSentAt, helloAttempt])

  useEffect(() => {
    const message = commandMessage(state.lastCommand)
    if (message) setActionMessage(message)
  }, [state.lastCommand])

  useEffect(() => {
    if (state.bindingResult) setActionMessage(state.bindingResult)
    if (state.status !== 'unbound' || (state.bindingResult && state.bindingResult !== 'Waiting for confirmation in AutoCAD.')) {
      if (state.bindingResult !== 'DWG connected. Starting the signed cross-probe session.') {
        if (bindingPending.current && state.status === 'unbound') setFocusConnect(true)
        bindingPending.current = false
      }
      setBinding(false)
    }
  }, [state.status, state.bindingResult])

  useEffect(() => {
    if (!binding) return undefined
    const timer = setTimeout(() => {
      actionInFlight.current = false
      bindingPending.current = false
      setFocusConnect(true)
      setBinding(false)
      bridge.retryHello()
      setActionMessage('AutoCAD did not answer. Look for a confirmation window in AutoCAD, then try again.')
    }, 60_000)
    return () => clearTimeout(timer)
  }, [binding, bridge])

  useEffect(() => {
    if (!focusConnect || binding) return
    if (connectRef.current && !connectRef.current.disabled) connectRef.current.focus()
    setFocusConnect(false)
  }, [focusConnect, binding])

  async function connect(chosenVersionId) {
    const version = versions.find((item) => item.version_id === chosenVersionId)
    if (!version || binding || actionInFlight.current) return
    const operation = {}
    actionInFlight.current = operation
    bindingPending.current = true
    setBinding(true)
    setActionMessage('Waiting for confirmation in AutoCAD.')
    try {
      await bridge.bindDrawing({
        platformTenantId: version.org_id, projectId: version.project_id,
        drawingId: version.drawing_id, drawingVersionId: version.version_id,
      })
    } catch {
      if (actionInFlight.current !== operation) return
      bindingPending.current = false
      setFocusConnect(true)
      setBinding(false)
      setActionMessage('The drawing could not be connected. Please try again.')
    } finally {
      if (actionInFlight.current === operation) actionInFlight.current = false
    }
  }

  async function runAction(action, name = true) {
    if (actionInFlight.current) return
    const operation = {}
    actionInFlight.current = operation
    setWorking(name)
    setActionMessage('')
    try {
      const outcome = await action()
      if (actionInFlight.current !== operation) return
      const message = commandMessage(outcome)
      if (message) setActionMessage(message)
    } catch {
      if (actionInFlight.current === operation) setActionMessage('The request could not be completed. Please try again.')
    } finally {
      if (actionInFlight.current === operation) {
        actionInFlight.current = false
        setWorking(false)
      }
    }
  }

  const selectedTarget = state.selectedObjectId || (state.selectedHandles?.length ? { objectHandles: state.selectedHandles } : null)
  const selectedLabel = state.selectedObjectId || (state.selectedHandles?.length === 1 ? state.selectedHandles[0] : null)

  return (
    <main className="leaf-platform" aria-labelledby="leaf-platform-title">
      <header>
        <p className="leaf-platform-context">Studio in AutoCAD</p>
        <h1 id="leaf-platform-title" ref={headingRef} tabIndex={-1}>Drawing connection</h1>
      </header>
      <p role="status" aria-live="polite" className="leaf-platform-status">{actionMessage}</p>
      {state.status === 'unavailable' && <section>
        <p>This page connects an AutoCAD drawing to a drawing version in your Leaf Automation Studio workspace.</p>
        <p>Open this page from AutoCAD with the LEAFPLATFORM command.</p>
        <a href="/app">Back to Studio</a>
      </section>}
      {state.status === 'connecting' && helloTimedOut && <button type="button" onClick={() => {
        bridge.retryHello()
        setHelloAttempt((attempt) => attempt + 1)
      }}>Try again</button>}
      {state.status === 'unbound' && <section>
        <p>Connect this DWG to a drawing version in your workspace.</p>
        {!signedIn ? <button type="button" disabled={working} onClick={() => runAction(() => login())}>Sign in</button> : <>
          {catalogMessage && <p>{catalogMessage}</p>}
          {(!isUuid(orgId) || catalogMessage === 'This workspace has no projects yet.') && <StudioRecovery />}
          {projectsFailed && <button type="button" disabled={binding} onClick={() => setProjectsAttempt((attempt) => attempt + 1)}>Try again</button>}
          {projects.length > 0 && <label>
            Project
            <select value={projectId} disabled={binding} onChange={(event) => { setProjectId(event.target.value); setVersionId('') }}>
              <option value="">Choose a project</option>
              {projects.map((project) => <option key={project.project_id} value={project.project_id}>{project.name || 'Untitled project'}</option>)}
            </select>
          </label>}
          {versionMessage && <p>{versionMessage}</p>}
          {versionMessage === 'This project has no drawing versions available to connect.' && <StudioRecovery />}
          {versionsFailed && <button type="button" disabled={binding} onClick={() => setVersionsAttempt((attempt) => attempt + 1)}>Try again</button>}
          {versions.length > 0 && <>
            <label>
              Drawing version
              <select value={versionId} disabled={binding} onChange={(event) => setVersionId(event.target.value)}>
                <option value="">Choose a drawing version</option>
                {versions.map((version) => <option key={version.version_id} value={version.version_id}>
                  {versionLabel(version, drawings)}
                </option>)}
              </select>
            </label>
            {chosenVersion && <p className="leaf-platform-summary">Connect this DWG to {typeof chosenProject?.name === 'string' && chosenProject.name.trim() ? chosenProject.name : 'Untitled project'} / {drawingName(chosenVersion, drawings)} / Version {chosenVersion.seq ?? '?'}. You cannot change this from the palette later.</p>}
            <button ref={connectRef} type="button" disabled={!versionId || binding} onClick={() => connect(versionId)}>Connect drawing</button>
          </>}
        </>}
      </section>}
      {state.status === 'connected' && <section>
        {!signedIn ? <>
          <p>This DWG is connected. Sign in to use this connection.</p>
          <button type="button" disabled={working} onClick={() => runAction(() => login())}>Sign in</button>
        </> : !isUuid(orgId) ? <><p>Choose a workspace in Studio, then reopen this palette.</p><StudioRecovery /></>
          : otherWorkspace ? <><p>This DWG belongs to another workspace. Open that workspace in Studio to use this connection.</p><StudioRecovery /></> : <>
          <p>Connected to {boundProject?.name || state.ready.drawingId.slice(0, 8)}.</p>
          {boundDetail?.identity === boundIdentity && boundDetail.label && <p>{boundDetail.label}</p>}
          <p>{selectedLabel ? <>Selected object: <span>{selectedLabel}</span></> : state.selectedHandles?.length
            ? `${state.selectedHandles.length} objects selected (${state.selectedHandles.slice(0, 3).join(', ')}${state.selectedHandles.length > 3 ? `, and ${state.selectedHandles.length - 3} more` : ''})`
            : 'Select objects in your AutoCAD drawing to use these buttons.'}</p>
          <div className="leaf-platform-actions">
            <button type="button" disabled={!selectedTarget || !!working} onClick={() => runAction(() => bridge.focusObject(selectedTarget, 'select'), 'select')}>{working === 'select' ? 'Selecting...' : 'Select'}</button>
            <button type="button" disabled={!selectedTarget || !!working} onClick={() => runAction(() => bridge.focusObject(selectedTarget, 'focus'), 'focus')}>{working === 'focus' ? 'Zooming...' : 'Zoom to'}</button>
          </div>
        </>}
      </section>}
    </main>
  )
}
