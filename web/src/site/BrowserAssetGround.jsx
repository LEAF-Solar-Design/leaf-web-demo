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
import { authHeaders, getStoredOrgId, noteUnauthorized } from '../api.js'
import { getStoredActorBindingId } from '../projects/api.js'
import ElementInspector from './ElementInspector.jsx'
import PortfolioEditor from './PortfolioEditor.jsx'
import './browserAssetGround.css'

// Verified release outputs and registered views share this scope-fenced renderer.
export const BrowserArtifactContext = createContext(null)

async function retrieveAuthoredView(request, projectId, current, signal) {
  const source = request.result
  const valid = (value, pattern) => typeof value === 'string' && pattern.test(value)
  const authority = value => typeof value === 'string' && value.trim() === value && value.length > 0 && value.length <= 200 && !/[\p{C}]/u.test(value)
  if (!current() || request.projectId !== projectId || source.kind !== 'view' || source.route !== 'view'
    || !authority(source.session_id) || !authority(source.turn_id)
    || source.session_id !== request.provenance?.sessionId || source.turn_id !== request.provenance?.turnId
    || !Number.isSafeInteger(request.provenance?.seq) || request.provenance.seq < 1
    || source.tenant_id !== getStoredOrgId()
    || !valid(source.tool, /^[a-z0-9]+(?:-[a-z0-9]+)*(?![\s\S])/) || source.tool.length > 200
    || !valid(source.catalog_commit, /^[0-9a-f]{40}(?![\s\S])/)
    || !valid(source.effective_catalog_digest, /^[0-9a-f]{64}(?![\s\S])/)
    || !valid(source.tool_manifest_sha256, /^sha256:[0-9a-f]{64}(?![\s\S])/)
    || source.catalog_digest !== source.tool_manifest_sha256) return null
  const query = new URLSearchParams({ project_id: projectId, session_id: source.session_id,
    catalog_commit: source.catalog_commit, effective_catalog_digest: source.effective_catalog_digest,
    tool_manifest_sha256: source.tool_manifest_sha256, catalog_digest: source.catalog_digest })
  const path = `/api/tools/${encodeURIComponent(source.tool)}/view-source?${query}`
  const headers = { ...authHeaders() }
  if (!headers.Authorization) return null
  const org = getStoredOrgId(), actor = getStoredActorBindingId()
  if (org) headers['X-Org-Id'] = org
  if (actor) headers['X-Actor-Binding-Id'] = actor
  const response = noteUnauthorized(await fetch(path, { headers, method: 'GET', cache: 'no-store',
    credentials: 'same-origin', redirect: 'error', signal }), path, headers.Authorization)
  if (!current() || signal.aborted || !response.ok || response.redirected
    || !response.headers.get('Content-Type')?.startsWith('application/json')
    || response.headers.get('Cache-Control') !== 'no-store') return null
  // JSON may escape each source byte; bound the transport too, before parsing.
  const reader = response.body?.getReader()
  if (!reader) return null
  const chunks = []
  let length = 0
  try {
    for (;;) {
      const { value, done } = await reader.read()
      if (done) break
      length += value.byteLength
      if (!current() || signal.aborted || length > 8 * 1024 * 1024) return null
      chunks.push(value)
    }
  } finally { await reader.cancel().catch(() => {}) }
  const bytes = new Uint8Array(length)
  let offset = 0
  for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength }
  const result = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(bytes))
  for (const key of ['contract', 'route', 'kind', 'tool', 'tenant_id', 'session_id', 'catalog_commit',
    'effective_catalog_digest', 'tool_manifest_sha256', 'catalog_digest']) {
    if (result[key] !== source[key]) return null
  }
  if (result.project_id !== projectId || result.media_type !== 'text/html' || typeof result.html !== 'string'
    || !Number.isSafeInteger(result.size_bytes) || result.size_bytes < 0 || result.size_bytes > 1024 * 1024
    || !valid(result.sha256, /^[0-9a-f]{64}(?![\s\S])/)) return null
  const htmlBytes = new TextEncoder().encode(result.html)
  if (htmlBytes.length !== result.size_bytes
    || new TextDecoder('utf-8', { fatal: true, ignoreBOM: true }).decode(htmlBytes) !== result.html) return null
  const digest = await crypto.subtle.digest('SHA-256', htmlBytes)
  const sha = Array.from(new Uint8Array(digest), value => value.toString(16).padStart(2, '0')).join('')
  return current() && !signal.aborted && sha === result.sha256 ? { html: result.html, name: `${source.tool}.html` } : null
}

export function BrowserAssetGround({ browserActive = false, browserPanel: controlledPanel, onBrowserPanel: setControlledPanel,
  projectId, accountScope, signedIn, profile = 'browser', profiles, onSelect, onDescribe, onEnterWorkspace, sampleEntry, canApplyLayout = false, ...boardProps }) {
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
    let pendingView = null
    const unsubscribe = subscribeProjectPageProposals(request => {
      const current = () => live.current && scope.current === currentScope && request.isCurrent()
        && request.accountScope === accountScope && composerDraftAccountScope() === accountScope
        && request.activeScope === activeScope && readActiveScope() === activeScope
      if (request.result?.contract === 'leaf.authored-view.v1') {
        if (pendingView && !pendingView.current()) { pendingView.controller.abort(); pendingView = null }
        if (!current() || request.projectId !== projectId || seen.has(request.key) || pendingView) return false
        const token = { current, controller: new AbortController() }
        pendingView = token
        void retrieveAuthoredView(request, projectId, current, token.controller.signal).then(artifact => {
          if (artifact && pendingView === token && current() && acceptArtifact(artifact)) {
            seen.add(request.key)
            if (seen.size > 128) seen.delete(seen.values().next().value)
          }
        }).catch(() => {}).finally(() => { if (pendingView === token) pendingView = null })
        return true
      }
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
    return () => { pendingView?.controller.abort(); unsubscribe() }
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
      mock={boardProps.mock} canApplyLayout={signedIn === true && canApplyLayout}
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
