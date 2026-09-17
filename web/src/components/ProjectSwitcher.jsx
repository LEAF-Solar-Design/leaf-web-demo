import { useEffect, useRef, useState } from 'react'
import './panels.css'
import useExit from '../useExit.js'
import { EMPTY_WORKSPACE_PROJECT, formatProjectsUnavailable } from '../site/workspaceProjectState.js'

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
  onCreateOrg, onCreateProject, onOpenProject, onLoadProjects,
}) {
  const [open, setOpen] = useState(false)
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

  // On open, start the highlight on the currently open project.
  useEffect(() => {
    if (!open) return
    const idx = (projects || []).findIndex((p) => (p.project_id || p.id) === openProjectId)
    setHi(idx >= 0 ? idx : 0)
  }, [open, projects, openProjectId])

  useEffect(() => {
    if (!open) return
    const onDoc = (e) => { if (rootRef.current && !rootRef.current.contains(e.target)) setOpen(false) }
    const onKey = (e) => {
      if (e.key === 'Escape' && !document.querySelector('.drawer-layer .drawer')) { setOpen(false); return } // an open drawer owns Esc
      if (e.target?.closest?.('input, textarea, select, [contenteditable="true"]')) {
        if (e.key === 'Enter' && !e.isComposing && e.target.tagName === 'INPUT' && rootRef.current?.contains(e.target)) {
          e.preventDefault()
          e.target.form?.requestSubmit()
        }
        return
      }
      if (e.target?.closest?.('button') && e.key === 'Enter') return
      const n = (projects || []).length
      if (unavailable || !bound || n === 0) return
      if (e.key === 'ArrowDown') { e.preventDefault(); setHi((h) => (h + 1) % n) }
      else if (e.key === 'ArrowUp') { e.preventDefault(); setHi((h) => (h - 1 + n) % n) }
      else if (e.key === 'Enter') {
        e.preventDefault()
        const p = projects[hi]
        if (p) { onOpenProject(p.project_id || p.id); setOpen(false) }
      }
    }
    document.addEventListener('mousedown', onDoc)
    document.addEventListener('keydown', onKey)
    return () => { document.removeEventListener('mousedown', onDoc); document.removeEventListener('keydown', onKey) }
  }, [open, projects, hi, bound, unavailable, openProjectId, onOpenProject])

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

  const pick = (pid) => { onOpenProject(pid); setOpen(false) }
  const count = (projects || []).length

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
                  <input value={orgName} onChange={(event) => setOrgName(event.target.value)} disabled={orgBusy} />
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
                <ul className="proj-list">
                  {projects.map((p, i) => {
                    const pid = p.project_id || p.id
                    const isOpen = pid === openProjectId
                    const isHi = i === hi
                    return (
                      <li key={pid}>
                        <button
                          className={`resolver-row ${isHi ? 'active' : ''}`}
                          onClick={() => pick(pid)}
                          onMouseEnter={() => setHi(i)}
                          role="menuitem"
                        >
                          <span className="lbar" aria-hidden="true" />
                          <span className="label">{p.name}</span>
                          {isOpen && <span className="proj-mark">Open</span>}
                          {isHi && <span className="key hot">Enter</span>}
                        </button>
                      </li>
                    )
                  })}
                  {projects.length === 0 && !loading && (!hasBootstrapState || projectsLoaded) && (
                    <li className="proj-note-li">No projects yet.</li>
                  )}
                </ul>
              )}
              <form className="proj-create" onSubmit={(event) => {
                event.preventDefault()
                const name = projectDraft.trim()
                if (!name) return
                submit('project', name)
              }}>
                <label>
                  New project
                  <input value={projectDraft} onChange={(event) => setProjectDraft(event.target.value)} disabled={projectBusy} />
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
