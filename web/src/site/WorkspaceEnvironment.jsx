import { useLayoutEffect, useRef, useState, version } from 'react'
import useEscapeOwner from '../lib/useEscapeOwner.js'
import { STUDIO_DRAWING_OCCLUDERS } from './drawingOccluders.js'
import './workspaceEnvironment.css'

// Next supplies React 19 while standalone consumers still use React 18.
export const INERT_VALUE = Number(version.split('.')[0]) >= 19 ? true : ''

export const ENVIRONMENT_OCCLUDERS = Object.freeze([
  ...STUDIO_DRAWING_OCCLUDERS,
  ['.browser-project-panel', 'left'], ['#project-workspace-activity', 'right'],
  ['.environment-strip', 'top'],
])

// Session memory belongs to this mounted authority scope, never to storage.
export default function WorkspaceEnvironment({ scopeKey, profile = 'browser', profiles = [], onSelect,
  panel = 'none', onPanel, hasContent = false, activation = 0, chooseRequest = 0, onEnter, onDescribe, onSignIn, children }) {
  const [memory, setMemory] = useState(() => ({ scopeKey, entered: {} }))
  if (memory.scopeKey !== scopeKey) setMemory({ scopeKey, entered: {} })
  const entered = memory.scopeKey === scopeKey && memory.entered[profile] === true
  const projectToggle = useRef(null)
  const studioToggle = useRef(null)
  const viewport = useRef(null)
  const chooser = useRef(null)
  const [focusChooser, setFocusChooser] = useState(0)
  const previousActivation = useRef(activation)
  const pendingViewportFocus = useRef(null)
  const close = () => {
    onPanel('none')
    ;(panel === 'project' ? projectToggle : studioToggle).current?.focus()
  }
  useEscapeOwner('workspace-environment', panel !== 'none', close, { layer: 'drawer' })
  useLayoutEffect(() => { onPanel('none') }, [profile, scopeKey])
  useLayoutEffect(() => {
    if (focusChooser) chooser.current?.querySelector('button')?.focus()
  }, [focusChooser])
  useLayoutEffect(() => {
    if (!chooseRequest) return
    setMemory(current => ({ ...current, entered: { ...current.entered, [profile]: false } }))
    onPanel('none')
    setFocusChooser(value => value + 1)
  }, [chooseRequest])
  useLayoutEffect(() => {
    if (panel === 'project') document.querySelector('.browser-project-panel .browser-panel-heading button')?.focus()
    if (panel === 'studio') document.getElementById('browser-studio-close')?.focus()
  }, [panel])
  const enter = (destination = 'none') => {
    onEnter?.()
    setMemory(current => ({ scopeKey, entered: { ...(current.scopeKey === scopeKey ? current.entered : {}), [profile]: true } }))
    onPanel(destination)
    if (destination === 'none') pendingViewportFocus.current = profile
  }
  useLayoutEffect(() => {
    if (activation !== previousActivation.current) {
      previousActivation.current = activation
      setMemory(current => ({ scopeKey, entered: { ...(current.scopeKey === scopeKey ? current.entered : {}), browser: true } }))
      onPanel('none')
      pendingViewportFocus.current = 'browser'
    }
  }, [activation, profile, scopeKey])
  useLayoutEffect(() => {
    if (!entered || panel !== 'none' || pendingViewportFocus.current !== profile) return
    pendingViewportFocus.current = null
    viewport.current?.focus()
  }, [entered, panel, profile])
  return <>
    <section className="workspace-environment" data-profile={profile} data-entered={entered ? 'true' : 'false'} aria-label={`${profile} environment`}>
      <div ref={viewport} tabIndex={-1} role="region" className="environment-focus" aria-label={`${profile} workspace`} />
      <div className="environment-backdrop" aria-hidden="true">{profile === 'ios' && <div className="environment-phone" />}</div>
      <div ref={chooser} className="environment-chooser" aria-hidden={entered ? 'true' : undefined} inert={entered ? INERT_VALUE : undefined}>
        <p>{profiles.find(item => item.id === profile)?.label || profile}</p>
        <h1>What would you like to work on?</h1>
        <button type="button" tabIndex={entered ? -1 : undefined} onClick={() => enter('project')}>Open project or material</button>
        <button type="button" tabIndex={entered ? -1 : undefined} onClick={() => { onDescribe?.(); enter('studio') }}>Describe what to create</button>
        {hasContent && <button type="button" tabIndex={entered ? -1 : undefined} onClick={() => enter()}>Continue workspace</button>}
        {typeof onSignIn === 'function' && <button type="button" tabIndex={entered ? -1 : undefined} onClick={() => onSignIn()}>Sign in</button>}
      </div>
      <nav className="environment-strip" aria-label="Workspace controls">
        <button ref={projectToggle} type="button" aria-expanded={panel === 'project'} aria-controls="environment-project-panel"
          onClick={() => panel === 'project' ? close() : enter('project')}>Project</button>
        <label>Profile<select aria-label="Workspace profile" value={profile} onChange={event => onSelect?.(event.target.value)}>
          {profiles.map(item => <option key={item.id} value={item.id}>{item.label}</option>)}
        </select></label>
        <button type="button" onClick={() => {
          setMemory(current => ({ ...current, entered: { ...current.entered, [profile]: false } }))
          onPanel('none')
          setFocusChooser(value => value + 1)
        }}>Choose</button>
        <button ref={studioToggle} type="button" aria-expanded={panel === 'studio'} aria-controls="project-workspace-activity"
          onClick={() => panel === 'studio' ? close() : enter('studio')}>Studio</button>
      </nav>
    </section>
    <div id="environment-project-panel" className="browser-project-panel" hidden={panel !== 'project'}
      inert={panel !== 'project' ? INERT_VALUE : undefined} role="region" aria-label="Project sidebar">
      <div className="browser-panel-heading"><strong>Project</strong><button type="button" onClick={close}>Close Project</button></div>
      {children}
    </div>
  </>
}
