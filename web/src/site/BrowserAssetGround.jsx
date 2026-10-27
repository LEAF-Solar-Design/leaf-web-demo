import { createContext, useEffect, useRef, useState } from 'react'
import { ProjectBoardGround } from './ProjectBoardGround.jsx'
import WorkspaceEnvironment from './WorkspaceEnvironment.jsx'
import ProjectPageEditor, { PagePreview } from './ProjectPageEditor.jsx'
import { subscribeBeforeLogout, subscribeTokenStored } from '../auth.js'
import { composerDraftAccountScope } from '../lib/composerDraft.js'
import { readActiveScope, subscribeActiveScope } from '../auth/activeScope.js'
import './browserAssetGround.css'

// Only ReleaseOutputs supplies decoded HTML after the trusted artifact retrieval.
export const BrowserArtifactContext = createContext(null)

export function BrowserAssetGround({ browserActive = false, browserPanel: controlledPanel, onBrowserPanel: setControlledPanel,
  projectId, accountScope, signedIn, profile = 'browser', profiles, onSelect, onDescribe, onEnterWorkspace, ...boardProps }) {
  const [localPanel, setLocalPanel] = useState('none')
  const browserPanel = controlledPanel ?? localPanel
  const onBrowserPanel = setControlledPanel || setLocalPanel
  const [authEpoch, setAuthEpoch] = useState(0)
  const scopeKey = JSON.stringify([signedIn === true, accountScope || null, projectId || null, authEpoch])
  const scope = useRef(null)
  const [asset, setAsset] = useState(null)
  const [activation, setActivation] = useState(0)
  const [selectedField, setSelectedField] = useState(null)
  if (scope.current?.key !== scopeKey) scope.current = { key: scopeKey }
  const currentScope = scope.current
  if (asset && asset.scope !== currentScope) setAsset(null)
  const shown = asset?.scope === currentScope ? asset : null
  const live = useRef(false)
  const accountRef = useRef(accountScope)
  accountRef.current = accountScope
  useEffect(() => {
    live.current = true
    // Auth notifications also fence an in-flight callback before App rerenders.
    const invalidate = () => { scope.current = null; setAsset(null); setAuthEpoch(value => value + 1) }
    const logout = subscribeBeforeLogout(invalidate)
    const checkAccount = () => {
      if (composerDraftAccountScope() !== accountRef.current) invalidate()
    }
    const token = subscribeTokenStored(checkAccount)
    let photoScope = readActiveScope()
    const activeScope = subscribeActiveScope(() => {
      const nextScope = readActiveScope()
      if (nextScope !== photoScope) { photoScope = nextScope; invalidate() }
    })
    const storage = event => { if (event.key === 'leaf.jwt' || event.key === null) checkAccount() }
    window.addEventListener('storage', storage)
    return () => { logout(); token(); activeScope(); window.removeEventListener('storage', storage); live.current = false }
  }, [])
  const acceptArtifact = ({ html, name }) => {
    if (!live.current || scope.current !== currentScope || !signedIn || !accountScope || accountScope === 'guest' || !projectId
      || typeof html !== 'string' || typeof name !== 'string' || !/\.html$/i.test(name)) return false
    setAsset({ html, name, scope: currentScope })
    onEnterWorkspace?.()
    setActivation(value => value + 1)
    onBrowserPanel('none')
    onSelect?.('browser')
    return true
  }
  const previewPage = ({ document, name }) => {
    if (!live.current || scope.current !== currentScope || !signedIn || !accountScope || accountScope === 'guest' || !projectId) return
    const opening = shown?.kind !== 'page'
    setAsset({ kind: 'page', document, name, scope: currentScope })
    if (opening) {
      onEnterWorkspace?.()
      setActivation(value => value + 1)
      onSelect?.('browser')
    }
  }
  return <BrowserArtifactContext.Provider value={acceptArtifact}>
    <section className="browser-asset-ground" data-ground="browser-asset" hidden={!browserActive} aria-label="Browser asset">
      <div className="browser-asset-viewport">
        {shown?.kind === 'page' ? <>
          <span className="browser-preview-label">Page draft · {shown.name}</span>
          <PagePreview document={shown.document} projectId={projectId} onSelectField={field => {
            setSelectedField({ field, request: Date.now() })
            onBrowserPanel('project')
          }} />
        </> : shown ? <>
          <span className="browser-preview-label">Verified preview · {shown.name}</span>
          <iframe title={`Verified preview: ${shown.name}`} srcDoc={shown.html} sandbox="allow-scripts allow-downloads" />
        </> : <div className="browser-asset-empty">
          <p>{boardProps.workspaceProject?.label || 'No project open'}</p>
          <h1>No app to show yet</h1>
          <p>Open Project to build or select a verified output.</p>
        </div>}
      </div>
    </section>
    <WorkspaceEnvironment scopeKey={currentScope} profile={profile} profiles={profiles} onSelect={onSelect} onDescribe={onDescribe}
      panel={browserPanel} onPanel={onBrowserPanel} activation={activation}
      chooseRequest={boardProps.startFocusRequest} onEnter={onEnterWorkspace}
      hasContent={profile === 'browser' ? Boolean(shown) : Boolean(boardProps.drawing
        || boardProps.workspace?.drawing_versions?.length || boardProps.workspace?.jobs?.length || boardProps.workspace?.built_tools?.length)}>
      {signedIn && projectId && <ProjectPageEditor key={scopeKey} projectId={projectId} accountScope={accountScope}
        selectedField={selectedField} onPreview={previewPage} />}
      <ProjectBoardGround {...boardProps} active={browserPanel === 'project'} occluders={undefined} />
    </WorkspaceEnvironment>
  </BrowserArtifactContext.Provider>
}
