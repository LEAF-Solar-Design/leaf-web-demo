import JobRail from '../components/JobRail.jsx'
import ReceiptPanel, { deepRedact } from '../projects/ReceiptPanel.jsx'
import ShipReceipts from '../ios/ShipReceipts.jsx'
import '../site/projectBoard.css'

export const PANE_NAMES = Object.freeze([
  'material', 'versions', 'conversation', 'tools', 'catalog', 'jobs', 'receipts',
  'annotations', 'authoring', 'settings',
])

const CAPABILITY_PANES = Object.freeze({
  conversation: 'conversation', annotations: 'annotations', authoring: 'authoring',
  approvals: 'annotations', versions: 'versions', receipts: 'receipts',
  marathons: 'jobs', 'one-shot execution': 'jobs',
})

export function paneForCapability(name) {
  return Object.hasOwn(CAPABILITY_PANES, name) ? CAPABILITY_PANES[name] : null
}

function pretty(value) {
  const text = JSON.stringify(deepRedact(value), null, 2) ?? 'No result yet.'
  return text.length > 4000 ? `${text.slice(0, 4000)}…` : text
}

function JobDetail({ job }) {
  return (
    <section className="ground-job-detail" aria-label="Job detail">
      <h3>{job.tool_name || job.kind || 'Job detail'}</h3>
      <dl>
        <dt>Status</dt><dd>{job.status || 'Unknown'}</dd>
        <dt>Created</dt><dd>{job.created_at || 'Not recorded'}</dd>
        <dt>Updated</dt><dd>{job.updated_at || 'Not recorded'}</dd>
        {job.cost_usd != null && <><dt>Cost (USD)</dt><dd>${job.cost_usd}</dd></>}
        <dt>Input version</dt><dd>{job.input_version_id || 'Not recorded'}</dd>
        <dt>Output version</dt><dd>{job.output_version_id || 'Not recorded'}</dd>
      </dl>
      <pre>{pretty(job.result)}</pre>
    </section>
  )
}

export default function ProjectWorkspacePanels({
  project, workspace, pane, onSelectPane, onBack, receipts, shipReceipts, slots,
  onOpenVersion, onSelectJob, currentJob, mock,
}) {
  if (!PANE_NAMES.includes(pane)) return null
  const title = pane[0].toUpperCase() + pane.slice(1)
  const material = workspace?.drawing_artifacts || []
  const versions = [...(workspace?.drawing_versions || [])].sort((a, b) =>
    (Date.parse(b.created_at) || 0) - (Date.parse(a.created_at) || 0) || (b.seq || 0) - (a.seq || 0))
  const tools = workspace?.built_tools || []
  let content
  switch (pane) {
    case 'material':
      content = material.length ? <ul>{material.map((row, index) => (
        <li key={row.artifact_id || row.drawing_id || index}>
          <strong>{row.name || row.drawing_id}</strong> · {row.status || 'Not recorded'} · {row.created_at || 'Not recorded'}
        </li>
      ))}</ul> : <p>No material attached yet. Upload a drawing to attach it.</p>
      break
    case 'versions':
      content = versions.length ? <ul>{versions.map((version) => (
        <li key={version.version_id}>
          {onOpenVersion ? <button type="button" className="ground-row-action" onClick={() => onOpenVersion(version)}>v{version.seq} · {version.version_id}</button>
            : <>v{version.seq} · {version.version_id}</>}
        </li>
      ))}</ul> : <p>No versions yet.</p>
      break
    case 'tools':
      content = tools.length ? <ul>{tools.map((tool, index) => (
        <li key={tool.tool_id || tool.name || index}>
          <strong>{tool.name || tool.tool_id}</strong> · {tool.version || 'Version not recorded'}
          {tool.provenance != null && <pre>{pretty(tool.provenance)}</pre>}
        </li>
      ))}</ul> : <p>No built tools yet. Author one from the Manage tab.</p>
      break
    case 'jobs':
      content = <>
        <JobRail jobs={[...(workspace?.jobs || [])].reverse()} currentJob={currentJob} onSelectJob={onSelectJob} mock={mock} />
        {currentJob && <JobDetail job={currentJob} />}
      </>
      break
    case 'receipts':
      content = receipts?.length || shipReceipts?.length ? <>
        {!!receipts?.length && <ReceiptPanel receipts={receipts} />}
        {!!shipReceipts?.length && <ShipReceipts receipts={shipReceipts} />}
      </> : <p>No receipts yet.</p>
      break
    default:
      content = slots?.[pane] ?? <p>{title} is not mounted on this surface.</p>
  }
  return (
    <section className="ground-pane" data-pane={pane} aria-label={title}>
      <header className="ground-pane-head">
        <div><h2>{title}</h2><p>{(project || workspace?.project)?.name || (project || workspace?.project)?.label || 'No project open'}</p></div>
        <button type="button" onClick={onBack}>Back to board</button>
      </header>
      {content}
    </section>
  )
}
