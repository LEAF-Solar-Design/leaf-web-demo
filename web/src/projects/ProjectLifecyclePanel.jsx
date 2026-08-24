/**
 * ProjectLifecyclePanel — the operator's project-lifecycle surface, mounted in
 * the /try workspace's "Project" tab (web/src/site/ToolCast.jsx) for a project
 * that is already open. It is NOT a project list: ProjectSwitcher owns
 * list/create/open, and this panel owns what you can do to the one open project.
 *
 * Composition, per the ratified graft:
 *   inline           Membership (roster + invite/role/revoke), ReceiptPanel
 *   behind a button  CloneDialog, ExportDialog, DangerZone
 * All five are props-driven and own no fetch. The single
 * GET /api/projects/{id}/lifecycle read lives in useProjectLifecycle.js and is
 * refetched after every mutation, which is the contract each of those
 * components' own header comment already documents.
 *
 * `enabled` defaults to the build flag so this component is inert if it is ever
 * rendered unguarded, but that default is a backstop, NOT the fence: the fence
 * is `ENV_LIFECYCLE_UI && ...` at the ToolCast call site, which is what keeps
 * this whole subtree out of a flag-off bundle (see flag.js, bundleFence.test.js).
 */
import { useCallback, useState } from 'react'

import CloneDialog from './CloneDialog.jsx'
import DangerZone from './DangerZone.jsx'
import ExportDialog from './ExportDialog.jsx'
import Membership from './Membership.jsx'
import ReceiptPanel from './ReceiptPanel.jsx'
import { ENV_LIFECYCLE_UI } from './flag.js'
import useProjectLifecycle from './useProjectLifecycle.js'

// The export artifact is JSON (leaf.project-export.v1), already sanitized
// server-side. Named from the server's project name so a downloaded file is
// identifiable without opening it.
function exportFilename(name) {
  const slug = String(name || 'project').trim().toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '')
  return `${slug || 'project'}-export.json`
}

export default function ProjectLifecyclePanel({
  projectId,
  projectName,
  enabled = ENV_LIFECYCLE_UI,
  onProjectDeleted,
}) {
  const [openTool, setOpenTool] = useState(null) // null | 'clone' | 'export'
  const lifecycle = useProjectLifecycle(projectId, { enabled: enabled && !!projectId })

  const { actions } = lifecycle
  const name = lifecycle.project?.name || projectName || ''

  const runExport = useCallback(async ({ signal }) => {
    const result = await actions.export({ signal })
    const artifact = result?.export
    return {
      receiptId: result?.receipt?.receipt_id,
      blob: artifact ? new Blob([JSON.stringify(artifact, null, 2)], { type: 'application/json' }) : undefined,
      filename: exportFilename(name),
    }
  }, [actions, name])

  // Delete is the one action whose own receipt this panel cannot display: the
  // project is gone, so the panel goes with it. The receipt id is handed to the
  // parent instead, which is where it stays visible after the unmount.
  const removeProject = useCallback(async () => {
    const result = await actions.remove()
    onProjectDeleted?.(projectId, result?.receipt?.receipt_id || null)
    return result
  }, [actions, onProjectDeleted, projectId])

  if (!enabled || !projectId) return null

  return (
    <section className="project-lifecycle" data-testid="projects-surface" aria-label="Project lifecycle">
      {lifecycle.status === 'loading' && (
        <div className="project-lifecycle-loading" role="status" aria-label="Loading project lifecycle">
          <div className="skeleton-stack" aria-hidden="true">
            <div className="skeleton-row" />
            <div className="skeleton-row" />
          </div>
        </div>
      )}

      {/* A failed REFRESH keeps the surface mounted and shows the staleness
          inline; only a failed cold load replaces the surface entirely. */}
      {lifecycle.error && (
        <div
          className={lifecycle.status === 'error' ? 'project-lifecycle-error' : 'project-lifecycle-stale'}
          role="alert"
        >
          <p>{lifecycle.error}</p>
          <button type="button" className="chip-act" onClick={() => lifecycle.refetch()}>Try again</button>
        </div>
      )}

      {lifecycle.status === 'ready' && (
        <>
          {lifecycle.refreshing && (
            <div className="project-lifecycle-refreshing" role="status">Updating…</div>
          )}
          <div data-testid="membership-panel">
            <Membership
              viewerId={lifecycle.viewerId}
              authority={lifecycle.authority}
              members={lifecycle.members}
              onInvite={actions.invite}
              onChangeRole={actions.changeRole}
              onRevoke={actions.revoke}
              inviteLabel="Invite by binding id"
              inviteInputType="text"
            />
          </div>

          <div className="project-lifecycle-tools">
            <button type="button" className="chip-act" onClick={() => setOpenTool('clone')}>Clone project</button>
            <button type="button" className="chip-act" onClick={() => setOpenTool('export')}>Export project</button>
          </div>

          {openTool === 'clone' && (
            <CloneDialog
              open
              project={{ project_id: projectId, name }}
              // CloneDialog asks for the clone by project id; the server names
              // the copy, so the name is derived here and echoed back from the
              // server's own `project` in the response.
              onClone={async () => {
                const result = await actions.clone(`${name} (copy)`)
                return {
                  receipt_id: result?.receipt?.receipt_id,
                  project_id: result?.project?.project_id,
                  name: result?.project?.name,
                }
              }}
              onClose={() => setOpenTool(null)}
            />
          )}

          {openTool === 'export' && (
            <ExportDialog onExport={runExport} onDismiss={() => setOpenTool(null)} />
          )}

          <ReceiptPanel receipts={lifecycle.receipts} />

          {/* No undo props on purpose: platform/project_lifecycle.py's reset and
              delete mint no restore token, so both must render as terminal. */}
          <DangerZone
            projectName={name}
            onReset={actions.reset}
            onDelete={removeProject}
          />
        </>
      )}
    </section>
  )
}
