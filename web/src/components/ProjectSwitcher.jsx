import { useEffect, useRef, useState } from 'react'
import './panels.css'

// The header PROJECT chip, made real: a calm switcher over the canonical
// org-scoped Project entity (platform/api.py). LIVE only — in mock mode it is a
// static label with zero /api calls (matching today's demo header exactly).
//
// States (live):
//   - platform unavailable (no DATABASE_URL / 500): show today's static drawing
//     name + a calm "Projects unavailable (no database configured)" note. Never
//     a broken surface.
//   - no org stored: a one-line "Create workspace org" affordance (POST /api/orgs).
//   - org present: the O2 resolver menu — 11px muted header with the count
//     right in muted, rows with a 2px accent left bar + tint + Enter cap on the
//     active row, arrow-key + Enter selection.
export default function ProjectSwitcher({
  mock, projectName, orgId, projects, openProjectId, currentName,
  unavailable, loading, orgBusy, projectBusy,
  onCreateOrg, onCreateProject, onOpenProject,
}) {
  const [open, setOpen] = useState(false)
  const [hi, setHi] = useState(0) // keyboard-highlighted row (resolver "active")
  const rootRef = useRef(null)

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
      if (e.key === 'Escape') { setOpen(false); return }
      const n = (projects || []).length
      if (unavailable || !orgId || n === 0) return
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
  }, [open, projects, hi, orgId, unavailable, openProjectId, onOpenProject])

  // Mock: no platform, no switcher — the classic static chip.
  if (mock) {
    return (
      <span className="proj-chip static">
        <span className="tag">Project</span>
        <span className="name">{projectName}</span>
      </span>
    )
  }

  const label = currentName || projectName
  const pick = (pid) => { onOpenProject(pid); setOpen(false) }
  const count = (projects || []).length

  return (
    <span className="proj-switch" ref={rootRef}>
      <button
        className="proj-chip"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        aria-haspopup="menu"
      >
        <span className="tag">Project</span>
        <span className="name">{label}</span>
        <span className="proj-caret" aria-hidden="true">▾</span>
      </button>

      {open && (
        <div className="proj-menu resolver" role="menu">
          {unavailable ? (
            <div className="proj-empty">
              <div className="proj-note">Projects unavailable (no database configured)</div>
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
                  {projects.length === 0 && !loading && (
                    <li className="proj-note-li">No projects yet.</li>
                  )}
                </ul>
              )}
              <button className="chip-act proj-act" onClick={() => { onCreateProject(); }} disabled={projectBusy}>
                {projectBusy ? 'Creating…' : 'New project'}
              </button>
            </>
          )}
        </div>
      )}
    </span>
  )
}
