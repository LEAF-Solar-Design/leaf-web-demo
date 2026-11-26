export const UNVERIFIED_REPORT_VERSION = 'Report input version could not be verified'

export function savedReportBinding(job, projectId) {
  const envelope = job.result
  const context = envelope?.project_context
  const foreignProject = context?.project_id != null && context.project_id !== projectId
  const isReport = (job.status === 'complete' || job.status === 'succeeded') &&
    envelope?.ok === true && Object.hasOwn(envelope, 'result') && envelope.result != null
  const inputVersion = job.input_version_id
  const version = isReport && typeof projectId === 'string' && projectId &&
    context?.project_id === projectId && typeof inputVersion === 'string' && inputVersion &&
    context?.input_version_id === inputVersion ? inputVersion : null
  return { version, unverified: isReport && version === null, foreignProject }
}
