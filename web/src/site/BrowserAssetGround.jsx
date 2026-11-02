import { createContext, useCallback, useEffect, useRef, useState } from 'react'
import { ProjectBoardGround } from './ProjectBoardGround.jsx'
import WorkspaceEnvironment from './WorkspaceEnvironment.jsx'
import ProjectPageEditor, { PagePreview } from './ProjectPageEditor.jsx'
import { authConfigured, isSignedIn, login, subscribeBeforeLogout, subscribeTokenStored } from '../auth.js'
import { composerDraftAccountScope } from '../lib/composerDraft.js'
import { readActiveScope, subscribeActiveScope } from '../auth/activeScope.js'
import { subscribeProjectPageProposals } from './projectPageProposalDelivery.js'
import { readEditingStyle, writeEditingStyle } from '../lib/editingStylePreference.js'
import CanvasToolbar from './CanvasToolbar.jsx'
import ElementInspector from './ElementInspector.jsx'
import PortfolioEditor from './PortfolioEditor.jsx'
import './browserAssetGround.css'

// Only ReleaseOutputs supplies decoded HTML after the trusted artifact retrieval.
export const BrowserArtifactContext = createContext(null)

export function BrowserAssetGround({ browserActive = false, browserPanel: controlledPanel, onBrowserPanel: setControlledPanel,
  projectId, accountScope, signedIn, profile = 'browser', profiles, onSelect, onDescribe, onEnterWorkspace, sampleEntry, ...boardProps }) {
  const [localPanel, setLocalPanel] = useState('none')
  const browserPanel = controlledPanel ?? localPanel
  const onBrowserPanel = setControlledPanel || setLocalPanel
  const [authEpoch, setAuthEpoch] = useState(0)
  const [showSignIn, setShowSignIn] = useState(() => authConfigured && !isSignedIn())
  const scopeKey = JSON.stringify([signedIn === true, accountScope || null, projectId || null, authEpoch])
  const scope = useRef(null)
  const [asset, setAsset] = useState(null)
  const [activation, setActivation] = useState(0)
  const [selectedField, setSelectedField] = useState(null)
  const [incomingProposal, setIncomingProposal] = useState(null)
  const [editorSession, setEditorSession] = useState(null)
  const preferenceScope = signedIn && (!authConfigured || isSignedIn()) ? accountScope : null
  const [stylePreference, setStylePreference] = useState(() => ({ accountScope: preferenceScope, value: readEditingStyle(preferenceScope) }))
  const editingStyle = stylePreference.accountScope === preferenceScope ? stylePreference.value : readEditingStyle(preferenceScope)
  const [editing, setEditing] = useState(false), [outline, setOutline] = useState(false), [settings, setSettings] = useState(false)
  const [selectedElement, setSelectedElement] = useState(null)
  const viewport = useRef(null)
  const pendingProposal = useRef(null)
  if (scope.current?.key !== scopeKey) scope.current = { key: scopeKey }
  const currentScope = scope.current
  const acceptSession = useCallback(value => {
    if (scope.current === currentScope) setEditorSession({ ...value, scope: currentScope })
  }, [currentScope])
  const pageEditor = editorSession?.scope === currentScope ? editorSession : null
  useEffect(() => {
    setStylePreference({ accountScope: preferenceScope, value: readEditingStyle(preferenceScope) })
  }, [preferenceScope])
  useEffect(() => { setSelectedElement(null); setSelectedField(null); setOutline(false); setSettings(false); setEditing(false) }, [currentScope])
  useEffect(() => { if (browserPanel === 'project') setOutline(false) }, [browserPanel])
  if (asset && asset.scope !== currentScope) setAsset(null)
  const shown = asset?.scope === currentScope ? asset : null
  useEffect(() => {
    if (!editing || !selectedElement) return
    const target = [...(viewport.current?.querySelectorAll('[data-element]') || [])].find(node => node.dataset.element === selectedElement)
    target?.scrollIntoView?.({ block: 'start' })
  }, [selectedElement, outline, editing, shown?.document])
  const live = useRef(false)
  const accountRef = useRef(accountScope)
  accountRef.current = accountScope
  useEffect(() => {
    live.current = true
    // Auth notifications also fence an in-flight callback before App rerenders.
    const invalidate = () => {
      scope.current = null; pendingProposal.current = null; setIncomingProposal(null)
      setAsset(null); setAuthEpoch(value => value + 1)
    }
    const logout = subscribeBeforeLogout(invalidate)
    const checkAccount = () => {
      if (composerDraftAccountScope() !== accountRef.current) invalidate()
    }
    const token = subscribeTokenStored(() => {
      setShowSignIn(authConfigured && !isSignedIn())
      checkAccount()
    })
    let photoScope = readActiveScope()
    const activeScope = subscribeActiveScope(() => {
      const nextScope = readActiveScope()
      if (nextScope !== photoScope) { photoScope = nextScope; invalidate() }
    })
    const storage = event => { if (event.key === 'leaf.jwt' || event.key === null) checkAccount() }
    window.addEventListener('storage', storage)
    return () => { logout(); token(); activeScope(); window.removeEventListener('storage', storage); live.current = false }
  }, [])
  useEffect(() => {
    pendingProposal.current = null
    setIncomingProposal(null)
    const activeScope = readActiveScope()
    if (!signedIn || !accountScope || accountScope === 'guest' || !projectId || !activeScope) return
    const seen = new Set()
    return subscribeProjectPageProposals(request => {
      const current = () => live.current && scope.current === currentScope && request.isCurrent()
        && request.accountScope === accountScope && composerDraftAccountScope() === accountScope
        && request.activeScope === activeScope && readActiveScope() === activeScope
      if (!current() || request.result?.source?.project_id !== projectId || seen.has(request.key) || pendingProposal.current) return false
      const incoming = { ...request, scope: currentScope, isCurrent: current, consumed: accepted => {
        if (pendingProposal.current !== incoming) return
        pendingProposal.current = null
        setIncomingProposal(value => value === incoming ? null : value)
        if (accepted === true && current()) {
          seen.add(request.key)
          if (seen.size > 128) seen.delete(seen.values().next().value)
        }
      } }
      pendingProposal.current = incoming
      setIncomingProposal(incoming)
      onEnterWorkspace?.()
      setActivation(value => value + 1)
      onSelect?.('browser')
      onBrowserPanel('project')
      return true
    })
  }, [currentScope]) // callbacks are captured with this editor lifetime
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
      <div ref={viewport} className="browser-asset-viewport" data-inspector={editingStyle === 'environment' && editing && outline || undefined}>
        {pageEditor && <CanvasToolbar editor={pageEditor} editing={editing} onEditing={value => { setEditing(value); if (!value) setOutline(false) }} outline={outline} onOutline={value => { setOutline(value); if (value) setEditing(true) }} settings={settings} onSettings={setSettings} />}
        {settings && <section className="canvas-settings" aria-label="Settings"><label>Editing style <select value={editingStyle} onChange={event => {
          if (!live.current || scope.current !== currentScope) return
          const value = event.target.value
          writeEditingStyle(preferenceScope, value)
          setStylePreference({ accountScope: preferenceScope, value })
          onBrowserPanel(value === 'sidebar' ? 'project' : 'none')
        }}><option value="environment">In environment</option><option value="sidebar">Sidebar</option></select></label><button type="button" onClick={() => setSettings(false)}>Close settings</button></section>}
        {shown?.kind === 'page' ? <>
          <span className="browser-preview-label">Page draft · {shown.name}</span>
          <PagePreview document={shown.document} projectId={projectId} selectedElement={selectedElement} onSelectElement={(value, inspect = false) => { setSelectedElement(value); if (inspect) setOutline(true) }} onInlineChange={editingStyle === 'environment' && editing && pageEditor?.state.enabled && !pageEditor.state.busy ? pageEditor.session.change : undefined} onSelectField={editingStyle === 'sidebar' && editing ? field => {
            setSelectedField({ field, request: Date.now() })
            onBrowserPanel('project')
          } : undefined} />
          {editingStyle === 'environment' && editing && outline && pageEditor && <aside className="canvas-properties">
            <fieldset disabled={!pageEditor.state.enabled || Boolean(pageEditor.state.busy)}><ElementInspector document={shown.document} selected={selectedElement} onSelect={setSelectedElement} onChange={pageEditor.session.change} onClose={() => setOutline(false)}>
              {shown.document.categories && <details><summary>Structure and project photos</summary><PortfolioEditor document={shown.document} onChange={pageEditor.session.change} projectId={projectId} onSelectElement={setSelectedElement} /></details>}
            </ElementInspector>
            </fieldset>
          </aside>}
        </> : shown ? <>
          <span className="browser-preview-label">Verified preview · {shown.name}</span>
          <iframe title={`Verified preview: ${shown.name}`} srcDoc={shown.html} sandbox="allow-scripts allow-downloads" />
        </> : <div className="browser-asset-empty">
          <p>{boardProps.workspaceProject?.label || 'No project open'}</p>
          <h1>{signedIn && projectId ? 'No page open' : 'No app to show yet'}</h1>
          <p>{signedIn && projectId ? 'Open page loads your saved page or portfolio.' : 'Open Project to build or select a verified output.'}</p>
          {pageEditor && <button type="button" disabled={!pageEditor.state.enabled || Boolean(pageEditor.state.busy)} onClick={pageEditor.session.open}>Open page</button>}
        </div>}
      </div>
    </section>
    <WorkspaceEnvironment scopeKey={currentScope} profile={profile} profiles={profiles} onSelect={onSelect} onDescribe={onDescribe}
      panel={browserPanel} onPanel={onBrowserPanel} activation={activation} sampleEntry={sampleEntry}
      chooseRequest={boardProps.startFocusRequest} onEnter={onEnterWorkspace}
      onSignIn={showSignIn ? () => { void login() } : undefined}
      hasContent={profile === 'browser' ? Boolean(shown) : Boolean(boardProps.drawing
        || boardProps.workspace?.drawing_versions?.length || boardProps.workspace?.jobs?.length || boardProps.workspace?.built_tools?.length)}>
      {signedIn && projectId && <ProjectPageEditor key={scopeKey} projectId={projectId} accountScope={accountScope}
        selectedField={selectedField} onPreview={previewPage} onSession={acceptSession} selectedElement={selectedElement} onSelectElement={setSelectedElement}
        incomingProposal={incomingProposal?.scope === currentScope ? incomingProposal : null} />}
      <ProjectBoardGround {...boardProps} active={browserPanel === 'project'} occluders={undefined} />
    </WorkspaceEnvironment>
  </BrowserArtifactContext.Provider>
}
