import { useEffect, useRef, useState } from 'react'

// The header PROJECT chip, made real: a calm switcher over the canonical
// org-scoped Project entity (platform/api.py). LIVE only — in mock mode it is a
// static label with zero /api calls (matching today's demo header exactly).
//
// States (live):
//   - platform unavailable (no DATABASE_URL / 500): show today's static drawing
//     name + a calm "projects unavailable (no database configured)" note. Never
//     a broken surface.
//   - no org stored: a one-line "Create workspace org" affordance (POST /api/orgs).
//   - org present: list projects (click to open), create project (name prompt).
//
// Calm vocabulary: a square mono chip, a plain bordered menu, word actions —
// no emoji, no rounded status pills, no spinners.
export default function ProjectSwitcher({
  mock, projectName, orgId, projects, openProjectId, currentName,
  unavailable, loading, orgBusy, projectBusy,
  onCreateOrg, onCreateProject, onOpenProject,
}) {
  const [open, setOpen] = useState(false)
  const rootRef = useRef(null)

  useEffect(() => {
    if (!open) return
    const onDoc = (e) => { if (rootRef.current && !rootRef.current.contains(e.target)) setOpen(false) }
    const onKey = (e) => { if (e.key === 'Escape') setOpen(false) }
    document.addEventListener('mousedown', onDoc)
    document.addEventListener('keydown', onKey)
    return () => { document.removeEventListener('mousedown', onDoc); document.removeEventListener('keydown', onKey) }
  }, [open])

  // Mock: no platform, no switcher — the classic static chip.
  if (mock) {
    return (
      <span className="proj-chip static" title="Projects are a live-mode workspace surface">
        <span className="tag">Project</span>
        <span className="name">{projectName}</span>
      </span>
    )
  }

  const label = currentName || projectName
  const pick = (pid) => { onOpenProject(pid); setOpen(false) }

  return (
    <span className="proj-switch" ref={rootRef}>
      <button
        className="proj-chip"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        aria-haspopup="menu"
        title="Switch project"
      >
        <span className="tag">Project</span>
        <span className="name">{label}</span>
        <span className="proj-caret" aria-hidden="true">{open ? 'hide' : 'switch'}</span>
      </button>

      {open && (
        <div className="proj-menu" role="menu">
          {unavailable ? (
            <div className="proj-empty">
              <div className="proj-note">projects unavailable (no database configured)</div>
              <div className="proj-sub">
                Showing the current drawing — <b>{projectName}</b>. Workspace projects need the
                platform database; the demo runs fine without it.
              </div>
            </div>
          ) : !orgId ? (
            <div className="proj-empty">
              <div className="proj-sub">No workspace org yet. Create one to keep projects and jobs.</div>
              <button className="btn primary proj-act" onClick={() => { onCreateOrg(); }} disabled={orgBusy}>
                {orgBusy ? 'Creating…' : 'Create workspace org'}
              </button>
            </div>
          ) : (
            <>
              <div className="proj-menu-head">Projects{loading ? ' · loading' : ''}</div>
              <ul className="proj-list">
                {projects.map((p) => {
                  const pid = p.project_id || p.id
                  const active = pid === openProjectId
                  return (
                    <li key={pid}>
                      <button
                        className={`proj-row ${active ? 'active' : ''}`}
                        onClick={() => pick(pid)}
                        role="menuitem"
                      >
                        <span className="proj-row-name">{p.name}</span>
                        {active && <span className="proj-mark">open</span>}
                      </button>
                    </li>
                  )
                })}
                {projects.length === 0 && !loading && (
                  <li className="proj-note-li">No projects yet.</li>
                )}
              </ul>
              <button className="btn ghost proj-act" onClick={() => { onCreateProject(); }} disabled={projectBusy}>
                {projectBusy ? 'Creating…' : 'New project'}
              </button>
            </>
          )}
        </div>
      )}
    </span>
  )
}
