import { useRef, useState } from 'react'

// First-run project entry for the board. The workspace controller owns all IO.
export function ProjectStartPanel({
  bootstrapState = 'unknown', projects = [], projectsLoaded = false,
  projectsLoading = false, openProjectId, projectsError,
  orgBusy = false, projectBusy = false, orgDraftError, projectDraftError,
  orgConflict = false, drawingMounted = false,
  onCreateOrg, onCreateProject, onOpenProject, onLoadProjects, onAttachDrawing,
}) {
  const [orgName, setOrgName] = useState('My workspace')
  const [projectName, setProjectName] = useState('')
  const [pending, setPending] = useState(false)
  const [error, setError] = useState(null)
  const inFlight = useRef(false)
  const rows = useRef([])
  const busy = pending || orgBusy || projectBusy
  const submitOnEnter = (event) => {
    if (event.key !== 'Enter' || event.nativeEvent.isComposing) return
    event.preventDefault()
    event.currentTarget.form?.requestSubmit()
  }
  const submit = async (kind, attach = false) => {
    const name = (kind === 'org' ? orgName : projectName).trim()
    if (!name || busy || inFlight.current) return
    inFlight.current = true
    setPending(true)
    setError(null)
    try {
      const result = await (kind === 'org' ? onCreateOrg(name) : onCreateProject(name))
      if (!result) return
      if (kind === 'org') setOrgName('My workspace')
      else {
        setProjectName('')
        const id = result.project_id || result.id
        if (attach && id && onAttachDrawing) await onAttachDrawing(id)
      }
    } catch (failure) { setError(String(failure?.message || failure)) }
    finally { inFlight.current = false; setPending(false) }
  }
  const retry = async () => {
    try { await onLoadProjects?.() }
    catch (failure) { setError(String(failure?.message || failure)) }
  }

  if (openProjectId) return null
  if (bootstrapState === 'unavailable') return (
    <section aria-label="Workspace projects">
      <p role="alert">{error || projectsError || 'Workspace projects are unavailable. Please retry.'}</p>
      <button type="button" onClick={retry}>Retry</button>
    </section>
  )
  if (bootstrapState === 'unknown') return <p role="status">Loading workspace projects…</p>
  if (bootstrapState === 'unbound') return (
    <section aria-label="Create your workspace">
      <h2>Create your workspace</h2>
      <form onSubmit={(event) => { event.preventDefault(); submit('org') }}>
        <label>Workspace name<input value={orgName} onChange={(event) => setOrgName(event.target.value)} onKeyDown={submitOnEnter} required /></label>
        <button type="submit" aria-disabled={busy} title={busy ? 'Workspace creation is in progress.' : undefined}>
          {busy ? 'Creating…' : 'Create workspace'}
        </button>
        {(orgDraftError || error) && <p role="alert">{orgDraftError || error}</p>}
      </form>
      {orgConflict && <button type="button" onClick={retry}>Use my existing workspace</button>}
    </section>
  )
  return (
    <section aria-label="Workspace projects">
      <h2>Open a project</h2>
      {projectsLoading && <p role="status">Loading projects…</p>}
      {!projectsLoading && projectsLoaded && projects.length === 0 && <p>No projects yet.</p>}
      <ul aria-label="Projects">
        {projects.map((project, index) => (
          <li key={project.project_id || project.id}>
            <button type="button" ref={(node) => { rows.current[index] = node }}
              onClick={() => onOpenProject(project.project_id || project.id)}
              onKeyDown={(event) => {
                if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return
                event.preventDefault()
                rows.current[(index + (event.key === 'ArrowDown' ? 1 : projects.length - 1)) % projects.length]?.focus()
              }}>{project.name}</button>
          </li>
        ))}
      </ul>
      <form onSubmit={(event) => { event.preventDefault(); submit('project') }}>
        <label>Project name<input value={projectName} onChange={(event) => setProjectName(event.target.value)} onKeyDown={submitOnEnter} required /></label>
        <button type="submit" aria-disabled={busy} title={busy ? 'Project creation is in progress.' : undefined}>
          {busy ? 'Creating…' : 'Create project'}
        </button>
        {drawingMounted && onAttachDrawing && (
          <button type="button" aria-disabled={busy} title={busy ? 'Project creation is in progress.' : undefined}
            onClick={() => submit('project', true)}>Create project from this drawing</button>
        )}
        {(projectDraftError || error) && <p role="alert">{projectDraftError || error}</p>}
      </form>
    </section>
  )
}

export default ProjectStartPanel
