import { useEffect, useRef, useState } from 'react'
import { createProjectSolarClient } from '../api.js'
import { sameSolarDrawingBinding } from './solarDrawingBinding.js'
import { createProjectSolarAuthority, deriveProjectSolarAppScope, projectSolarCheckoutState,
  projectSolarScopeSnapshot } from './solarProjectAppModel.js'

const notCurrent = () => false

export function useProjectSolarScope(
  { projectMode, runContext, session },
  { createClient = createProjectSolarClient } = {},
) {
  const authority = useRef(null)
  if (authority.current === null) authority.current = createProjectSolarAuthority()
  const ownerRef = useRef(null)
  const desiredRef = useRef(null)
  const [installed, setInstalled] = useState(null)
  // Only canonical identity and the reported auth lifecycle fields enter the owner.
  const authenticated = session?.authenticated
  const actorId = session?.actorId
  const generation = session?.generation
  const orgId = runContext?.orgId
  const projectId = runContext?.projectId
  const drawingArtifactId = runContext?.drawingArtifactId
  const drawingId = runContext?.drawingId
  const drawingVersion = runContext?.drawingVersion
  const inputs = [projectMode, orgId, projectId, drawingArtifactId, drawingId, drawingVersion,
    authenticated, actorId, generation]
  const equalInputs = (a, b) => !!a && a.length === b.length && a.every((value, index) => Object.is(value, b[index]))
  desiredRef.current = { inputs, createClient }

  useEffect(() => {
    const owner = { client: createClient(), createClient, live: true, installation: null }
    ownerRef.current = owner
    const publish = () => {
      if (!owner.live || !owner.installation) return
      const installation = owner.installation
      // Only the projected seven fields enter React state, never the raw snapshot.
      const snapshot = projectSolarScopeSnapshot(owner.client.getSnapshot())
      const capture = owner.client.captureCatalogScope()
      let valid = true
      const isCurrent = () => {
        valid = valid && owner.live && ownerRef.current === owner && owner.installation === installation &&
          desiredRef.current.createClient === createClient && equalInputs(installation.inputs, desiredRef.current.inputs) &&
          capture()
        return valid
      }
      setInstalled({ owner, installation, snapshot, isCurrent })
    }
    owner.publish = publish
    const unsubscribe = owner.client.subscribe(publish)
    return () => {
      owner.live = false
      owner.installation = null
      if (ownerRef.current === owner) ownerRef.current = null
      authority.current.clear()
      unsubscribe()
      owner.client.dispose()
    }
  }, [createClient])

  useEffect(() => {
    const owner = ownerRef.current
    if (!owner?.live) return
    const key = authority.current.update({ authenticated, actorId, generation })
    const scope = deriveProjectSolarAppScope({ projectMode,
      runContext: { orgId, projectId, drawingArtifactId, drawingId, drawingVersion }, authorityKey: key })
    // Invalidate before setScope can synchronously notify its subscribers.
    owner.installation = null
    owner.client.setScope(scope.mode === 'project' ? scope.binding : null,
      scope.mode === 'project' ? scope.authorityKey : null)
    owner.installation = { inputs, scope }
    owner.publish()
  }, [createClient, projectMode, orgId, projectId, drawingArtifactId, drawingId, drawingVersion,
    authenticated, actorId, generation])

  const matches = installed?.owner.live && installed.owner === ownerRef.current &&
    installed.owner.installation === installed.installation &&
    installed.owner.createClient === createClient && equalInputs(installed.installation.inputs, inputs)
  const scope = matches ? installed.installation.scope : deriveProjectSolarAppScope({ projectMode,
    runContext: { orgId, projectId, drawingArtifactId, drawingId, drawingVersion }, authorityKey: null })
  const ready = !!(matches && scope.mode === 'project' &&
    sameSolarDrawingBinding(scope.binding, installed.snapshot.binding) &&
    scope.authorityKey === installed.snapshot.authorityKey)
  return { scope, ready, projectClient: ready ? installed.owner.client : null,
    checkout: projectSolarCheckoutState({ scope, snapshot: matches ? installed.snapshot : null }),
    isCurrent: ready ? installed.isCurrent : notCurrent }
}
