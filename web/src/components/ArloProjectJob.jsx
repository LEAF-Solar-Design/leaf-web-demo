import { useEffect, useRef, useState } from 'react'
import { openProject } from '../api.js'
import ArloProposalReview from './ArloProposalReview.jsx'

// Use the authenticated canonical workspace read, never the legacy job store.
export default function ArloProjectJob({ project, job }) {
  const [opened, setOpened] = useState(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const request = useRef(0)
  useEffect(() => () => { request.current += 1 }, [])
  const show = async () => {
    const attempt = ++request.current
    setLoading(true); setError(''); setOpened(null)
    try {
      const workspace = await openProject(project.project_id, project.org_id)
      const fresh = workspace.jobs?.find(row => row.job_id === job.job_id)
      if (workspace.project?.project_id !== project.project_id || workspace.project?.org_id !== project.org_id
          || fresh?.org_id !== project.org_id || fresh?.project_id !== project.project_id
          || fresh?.tool_name !== 'arlo-design' || fresh?.status !== 'succeeded'
          || fresh?.result?.solver !== 'arlo-design') throw new Error('This proposal is no longer available in this project.')
      if (attempt !== request.current) return
      setOpened({ envelope:fresh.result, context:{org_id:fresh.org_id, project_id:fresh.project_id,
        job_id:fresh.job_id, input_version_id:fresh.input_version_id} })
    } catch (cause) {
      if (attempt === request.current) setError(cause.message || 'Could not open this proposal.')
    } finally {
      if (attempt === request.current) setLoading(false)
    }
  }
  const close = () => { request.current += 1; setOpened(null); setError(''); setLoading(false) }
  return <div>
    <button type="button" className="btn ghost" disabled={loading} onClick={show}>Open feeder proposal</button>
    {loading && <p role="status">Opening saved proposal…</p>}
    {error && <p role="alert">{error}</p>}
    {opened && <section aria-label="Saved feeder proposal">
      <button type="button" className="btn ghost" onClick={close}>Close proposal</button>
      <ArloProposalReview envelope={opened.envelope} context={opened.context} />
    </section>}
  </div>
}
