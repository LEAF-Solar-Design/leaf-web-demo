import { useEffect, useMemo, useRef, useState } from 'react'
import './panels.css'
import useExit from '../useExit.js'
import useEscapeOwner from '../lib/useEscapeOwner.js'
import { EMPTY_WORKSPACE_PROJECT, formatProjectsUnavailable } from '../site/workspaceProjectState.js'
import { addRecentProject, readProjectPrincipal, readRecentProjects, togglePinnedProject, writeRecentProjects } from '../lib/recentProjects.js'

// The header PROJECT chip, made real: a calm switcher over the canonical
// org-scoped Project entity (platform/api.py). LIVE only — in mock mode it is a
// static label with zero /api calls (matching today's demo header exactly).
//
// HONEST TAG (fixed 2026-09-01). This chip used to render the literal tag
// "Project" over a name that fell back to `projectName`, which is the MOUNTED
// DRAWING's name, not a project's. With no workspace project open it therefore
// printed "Project rooftop_demo" — the header half of the contradiction a pilot
// user hit, with three surface cards correctly saying "No project open" below
// it. The tag now comes from workspaceProjectState.js, the one derivation every
// surface shares, so it reads "Drawing rooftop_demo" until a workspace project
// is genuinely open. The menu below is unchanged.
//
// States (live):
//   - platform unavailable (error string from the controller): static drawing
//     name + the controller's message, or a neutral service note.
//   - no org stored: a one-line "Create workspace org" affordance (POST /api/orgs).
//   - org present: the O2 resolver menu — 11px muted header with the count
//     right in muted, rows with a 2px accent left bar + tint + Enter cap on the
//     active row, arrow-key + Enter selection.
export default function ProjectSwitcher({
  mock, projectName, orgId, projects = [], openProjectId,
  bootstrapState, projectsLoaded = false, orgDraftError, projectDraftError, orgConflict,
  unavailable, loading, orgBusy, projectBusy, workspaceProject = null,
  createRequest = 0, storage, principalId = readProjectPrincipal(storage),
  onCreateOrg, onCreateProject, onOpenProject, onLoadProjects,
}) {
  const [open, setOpen] = useState(false)
  const [focusCreate, setFocusCreate] = useState(false)
  const createFieldRef = useRef(null)
  const [hi, setHi] = useState(0) // keyboard-highlighted row (resolver "active")
  const [orgName, setOrgName] = useState('My workspace')
  const [projectDraft, setProjectDraft] = useState('')
  const rootRef = useRef(null)
  const menu = useExit(open) // 180 ms M1 exit fade on close
  const hasBootstrapState = bootstrapState !== undefined
  const bound = hasBootstrapState ? bootstrapState === 'bound' : !!orgId
  const unbound = hasBootstrapState ? bootstrapState === 'unbound' : !orgId
  const draftError = hasBootstrapState ? (unbound ? orgDraftError : projectDraftError) : null
  const onOrgCreated = () => setOrgName('My workspace')
  const onProjectCreated = () => setProjectDraft('')
  const [submitError, setSubmitError] = useState(null)
  const submitting = useRef(false)
  const storedHistory = useMemo(() => readRecentProjects(principalId, storage), [principalId, storage])
  const [historyState, setHistoryState] = useState(() => ({ principalId, value: storedHistory }))
  const history = historyState.principalId === principalId ? historyState.value : storedHistory
  const groups = useMemo(() => {
    const byId = new Map(projects.map((project) => [project.project_id || project.id, project]))
    const resolve = (ids) => ids.map((id) => byId.get(id)).filter(Boolean)
    return [
      { name: 'Pinned', projects: principalId ? resolve(history.pinned) : [] },
      { name: 'Recent', projects: principalId ? resolve(history.recent) : [] },
      { name: 'Projects', projects },
    ]
  }, [projects, history, principalId])
  const menuProjects = useMemo(() => groups.flatMap((group) => group.projects), [groups])
  const currentOpen = !mock && bound && !unavailable && projects.some((project) => (project.project_id || project.id) === openProjectId)

  useEffect(() => {
    setHistoryState({ principalId, value: storedHistory })
  }, [principalId, storedHistory])

  // Also remember projects opened by creation or another workspace control.
  useEffect(() => {
    if (!principalId || !currentOpen) return
    setHistoryState((previous) => {
      const value = addRecentProject(previous.principalId === principalId ? previous.value : storedHistory, openProjectId)
      writeRecentProjects(principalId, value, storage)
      return { principalId, value }
    })
  }, [principalId, currentOpen, openProjectId, storedHistory, storage])

  const remember = (value) => {
    if (!principalId) return
    writeRecentProjects(principalId, value, storage)
    setHistoryState({ principalId, value })
  }
  const pick = (pid) => {
    onOpenProject(pid)
    remember(addRecentProject(history, pid))
    setOpen(false)
  }

  const submit = async (kind, name) => {
    if (!name.trim() || orgBusy || projectBusy || submitting.current) return
    submitting.current = true
    setSubmitError(null)
    try {
      const result = await (kind === 'org' ? onCreateOrg(name.trim()) : onCreateProject(name.trim()))
      if (result) (kind === 'org' ? onOrgCreated : onProjectCreated)()
    } catch (error) { setSubmitError(String(error?.message || error)) }
    finally { submitting.current = false }
  }

  // A caller's create request (the ribbon's Create project) opens the menu on
  // whichever inline create field the state shows. There is no native dialog
  // fallback: every creation goes through these fields.
  useEffect(() => {
    if (!createRequest) return
    setOpen(true)
    setFocusCreate(true)
  }, [createRequest])

  useEffect(() => {
    if (!focusCreate || !menu.shown) return
    createFieldRef.current?.focus()
    setFocusCreate(false)
  }, [focusCreate, menu.shown])

  // On open, start the highlight on the currently open project.
  useEffect(() => {
    if (!open) return
    const idx = menuProjects.findIndex((p) => (p.project_id || p.id) === openProjectId)
    setHi(idx >= 0 ? idx : 0)
  }, [open, menuProjects, openProjectId])

  // S27: Escape is the owner stack's (menu layer), which replaces the old "an
  // open drawer owns Esc" selector check with the section 7 order.
  useEscapeOwner('project-menu', open && !mock, () => setOpen(false), { layer: 'menu', scope: rootRef })

  useEffect(() => {
    if (!open) return
    const onDoc = (e) => { if (rootRef.current && !rootRef.current.contains(e.target)) setOpen(false) }
    const onKey = (e) => {
      if (e.target?.closest?.('input, textarea, select, [contenteditable="true"]')) {
        if (e.key === 'Enter' && !e.isComposing && e.target.tagName === 'INPUT' && rootRef.current?.contains(e.target)) {
          e.preventDefault()
          e.target.form?.requestSubmit()
        }
        return
      }
      if (e.target?.closest?.('button') && e.key === 'Enter') return
      const n = menuProjects.length
      if (unavailable || !bound || n === 0) return
      if (e.key === 'ArrowDown') { e.preventDefault(); setHi((h) => (h + 1) % n) }
      else if (e.key === 'ArrowUp') { e.preventDefault(); setHi((h) => (h - 1 + n) % n) }
      else if (e.key === 'Enter') {
        e.preventDefault()
        const p = menuProjects[hi]
        if (p) pick(p.project_id || p.id)
      }
    }
    document.addEventListener('mousedown', onDoc)
    document.addEventListener('keydown', onKey)
    return () => { document.removeEventListener('mousedown', onDoc); document.removeEventListener('keydown', onKey) }
  }, [open, menuProjects, hi, bound, unavailable, openProjectId, onOpenProject, history, principalId, storage])

  // The chip reads the shared derivation and NOTHING else. sol-critic finding
  // 1: that pre-F-9 name fallback survived here as a
  // compatibility path, and `projectName` is the mounted DRAWING's name -- so
  // that fallback could still print "Project rooftop_demo" over a drawing,
  // reproducing the exact bug this change exists to fix. Omitting the prop now
  // degrades to the honest resting state instead. (`projectName` is still used
  // below, in the platform-unavailable note, where naming the drawing IS the
  // honest thing to say.)
  const state = workspaceProject || EMPTY_WORKSPACE_PROJECT
  const tag = state.tag
  // 'None open' rather than a blank chip: the switcher is the affordance for
  // opening one, so the resting state has to read as a state, not as a
  // half-rendered label.
  const label = state.label || 'None open'

  // Mock: no platform, no switcher — the classic static chip.
  if (mock) {
    return (
      <span className="proj-chip static">
        <span className="tag">{tag}</span>
        <span className="name">{label}</span>
      </span>
    )
  }

  const count = (projects || []).length
  const projectRow = (p, i) => {
    const pid = p.project_id || p.id
    const isOpen = pid === openProjectId
    const isHi = i === hi
    const pinned = history.pinned.includes(pid)
    return (
      <li key={pid} style={principalId ? { display: 'flex', alignItems: 'center' } : undefined}>
        <button
          className={`resolver-row ${isHi ? 'active' : ''}`}
          style={principalId ? { flex: 1, minWidth: 0 } : undefined}
          onClick={() => pick(pid)}
          onMouseEnter={() => setHi(i)}
          role="menuitem"
        >
          <span className="lbar" aria-hidden="true" />
          <span className="label">{p.name}</span>
          {isOpen && <span className="proj-mark">Open</span>}
          {isHi && <span className="key hot">Enter</span>}
        </button>
        {principalId && (
          <button type="button" className="chip-act" style={{ flexShrink: 0 }} role="menuitemcheckbox" aria-checked={pinned}
            aria-label={`${pinned ? 'Unpin' : 'Pin'} ${p.name}`}
            onClick={() => remember(togglePinnedProject(history, pid))}>
            {pinned ? 'Unpin' : 'Pin'}
          </button>
        )}
      </li>
    )
  }

  return (
    <span className="proj-switch" ref={rootRef}>
      <button
        type="button"
        className="proj-chip"
        aria-label={`Projects: change project. ${tag} ${label}`}
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        aria-haspopup="menu"
      >
        <span>Projects / Change project</span>
        <span className="tag">{tag}</span>
        <span className="name">{label}</span>
        <span className="proj-caret" aria-hidden="true">▾</span>
      </button>

      {menu.shown && (
        <div className={`proj-menu resolver${menu.exiting ? ' exit' : ''}`} role="menu">
          {unavailable && (!hasBootstrapState || (!unbound && unavailable !== draftError)) ? (
            <div className="proj-empty">
              <div className="proj-note">{formatProjectsUnavailable(unavailable)}</div>
              <div className="proj-sub">
                Showing the current drawing: <b>{projectName}</b>. The demo keeps working without workspace projects.
              </div>
            </div>
          ) : unbound ? (
            <div className="proj-empty">
              <div className="proj-sub">No workspace org yet. Create one to keep projects and jobs.</div>
              <form className="proj-create" onSubmit={(event) => { event.preventDefault(); submit('org', orgName) }}>
                <label>
                  Workspace name
                  <input ref={createFieldRef} value={orgName} onChange={(event) => setOrgName(event.target.value)} disabled={orgBusy} />
                </label>
                <button className="btn primary proj-act" type="submit" disabled={orgBusy || !orgName.trim()}>
                  {orgBusy ? 'Creating…' : 'Create workspace org'}
                </button>
              </form>
              {(draftError || submitError) && <p role="alert">{draftError || submitError}</p>}
              {hasBootstrapState && orgConflict && <button type="button" onClick={onLoadProjects}>Use my existing workspace</button>}
            </div>
          ) : !bound ? (
            <div className="proj-note" role="status">Loading workspace projects…</div>
          ) : (
            <>
              <div className="resolver-header">
                Projects
                {(!loading || count > 0) && <span className="proj-count">{count}</span>}
              </div>
              {loading && count === 0 ? (
                <div className="skeleton-stack" aria-hidden="true">
                  <div className="skeleton-row" />
                  <div className="skeleton-row" />
                  <div className="skeleton-row" />
                </div>
              ) : (
                <>
                  {groups.slice(0, 2).map((group, groupIndex) => group.projects.length > 0 && (
                    <div key={group.name} role="group" aria-label={group.name}>
                      <div className="resolver-header">{group.name}</div>
                      <ul className="proj-list">
                        {group.projects.map((p, i) => projectRow(p, i + (groupIndex === 1 ? groups[0].projects.length : 0)))}
                      </ul>
                    </div>
                  ))}
                <ul className="proj-list" role="group" aria-label="All projects">
                  {projects.map((p, i) => projectRow(p, i + groups[0].projects.length + groups[1].projects.length))}
                  {projects.length === 0 && !loading && (!hasBootstrapState || projectsLoaded) && (
                    <li className="proj-note-li">No projects yet.</li>
                  )}
                </ul>
                </>
              )}
              <form className="proj-create" onSubmit={(event) => {
                event.preventDefault()
                const name = projectDraft.trim()
                if (!name) return
                submit('project', name)
              }}>
                <label>
                  New project
                  <input ref={createFieldRef} value={projectDraft} onChange={(event) => setProjectDraft(event.target.value)} disabled={projectBusy} />
                </label>
                <button className="chip-act proj-act" type="submit" disabled={projectBusy || !projectDraft.trim()}>
                  {projectBusy ? 'Creating…' : 'Create project'}
                </button>
              </form>
              {(draftError || submitError) && <p role="alert">{draftError || submitError}</p>}
            </>
          )}
        </div>
      )}
    </span>
  )
}
