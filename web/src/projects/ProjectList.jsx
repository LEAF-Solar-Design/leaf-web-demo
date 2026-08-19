/**
 * ProjectList — the projects surface: list + create (blank browser entry).
 *
 * Card B-U1 acceptance:
 *   1. Signed-in user sees their projects (server-derived membership only);
 *      create names a project and lands in it; empty state is truthful.
 *   2. Loading/error/empty states all rendered; no state renders a blank screen.
 *   3. With lifecycle_ui off the surface is absent (fence assertion in test).
 *
 * This component owns its own fetch (./api.js) but never filters, sorts, or
 * otherwise re-derives "the signed-in user's projects" itself — whatever the
 * server returns from GET /api/projects is rendered verbatim. `enabled`
 * defaults to the VITE_LIFECYCLE_UI build flag (off unless explicitly baked
 * in) so a host page can also drive it directly, e.g. for a test fence.
 */
import { useCallback, useEffect, useState } from 'react'
import { listProjects, createProject } from './api.js'
import { humanizeError } from '../errorHumanize.js'

const ENV_LIFECYCLE_UI = import.meta.env?.VITE_LIFECYCLE_UI === '1'

export default function ProjectList({ enabled = ENV_LIFECYCLE_UI, onOpenProject }) {
  const [status, setStatus] = useState('loading') // 'loading' | 'error' | 'ready'
  const [projects, setProjects] = useState([])
  const [listError, setListError] = useState(null)
  const [draftName, setDraftName] = useState('')
  const [creating, setCreating] = useState(false)
  const [createError, setCreateError] = useState(null)
  const [openedProject, setOpenedProject] = useState(null)

  const load = useCallback(() => {
    if (!enabled) return
    setStatus('loading')
    setListError(null)
    listProjects()
      .then((list) => { setProjects(list || []); setStatus('ready') })
      .catch((e) => { setListError(humanizeError(e)); setStatus('error') })
  }, [enabled])

  useEffect(() => { load() }, [load])

  // lifecycle_ui off — this surface is absent entirely, not a disabled husk.
  if (!enabled) return null

  const openProject = (project) => {
    setOpenedProject(project)
    onOpenProject?.(project)
  }

  const submitCreate = async (event) => {
    event.preventDefault()
    const name = draftName.trim()
    if (!name || creating) return
    setCreating(true)
    setCreateError(null)
    try {
      const project = await createProject(name)
      setDraftName('')
      setProjects((prev) => [...prev, project])
      openProject(project)
    } catch (e) {
      setCreateError(humanizeError(e))
    } finally {
      setCreating(false)
    }
  }

  return (
    <section className="projects-surface" aria-label="Projects">
      <div className="projects-head">
        <span className="projects-title">Projects</span>
      </div>

      {openedProject && (
        <p className="projects-opened" role="status">
          Now in <b>{openedProject.name}</b>.
        </p>
      )}

      <form className="projects-create" onSubmit={submitCreate}>
        <label>
          New project name
          <input
            value={draftName}
            onChange={(event) => setDraftName(event.target.value)}
            disabled={creating}
            required
          />
        </label>
        <button type="submit" className="chip-act" disabled={creating || !draftName.trim()}>
          {creating ? 'Creating…' : 'Create project'}
        </button>
      </form>
      {createError && <p className="projects-create-error" role="alert">{createError}</p>}

      {status === 'loading' && (
        <div className="projects-loading" role="status" aria-label="Loading projects">
          <div className="skeleton-stack" aria-hidden="true">
            <div className="skeleton-row" />
            <div className="skeleton-row" />
            <div className="skeleton-row" />
          </div>
        </div>
      )}

      {status === 'error' && (
        <div className="projects-error" role="alert">
          <p>{listError}</p>
          <button type="button" className="chip-act" onClick={load}>Try again</button>
        </div>
      )}

      {status === 'ready' && (
        projects.length === 0 ? (
          <p className="projects-empty">
            You don’t have any projects yet — create one above to get started.
          </p>
        ) : (
          <ul className="projects-roster">
            {projects.map((p) => {
              const id = p.project_id || p.id
              return (
                <li key={id} className="projects-row">
                  <button type="button" className="projects-open" onClick={() => openProject(p)}>
                    {p.name}
                  </button>
                </li>
              )
            })}
          </ul>
        )
      )}
    </section>
  )
}
