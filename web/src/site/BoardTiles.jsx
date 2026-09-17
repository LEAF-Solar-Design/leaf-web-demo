import { Children } from 'react'
import { START_BOARD_COPY } from './startBoardCopy.js'
import { isWriteTool } from '../lib/toolRecord.js'
import { formatElementId } from '../lib/elementIdentity.js'
import { SHARED_WORKSPACE_CAPABILITIES } from './productSurfaces.js'
import { shortId } from './groundWindow.js'

const listCount = (list) => (Array.isArray(list) ? list.length : 0)

function capabilityTotal(families) {
  return families.reduce((count, family) => count + listCount(family.capabilities), 0)
}

// ---------------------------------------------------------------------------
// Browser: the open-project board. Tiles are the project's real objects —
// the mounted drawing, its versions, jobs, built tools, the live tenant
// catalog — plus the capabilities every surface shares. `workspace` is the
// same GET /api/projects/:id/workspace payload WorkspaceSummary renders;
// null (no project open, or the offline demo) renders the honest empties.
// ---------------------------------------------------------------------------
export function BoardTiles({ workspace, drawing, catalog, renderTile, studioPresentation = false }) {
  const versions = workspace?.drawing_versions || []
  const jobs = [...(workspace?.jobs || [])].reverse().slice(0, 5) // newest first
  const tools = workspace?.built_tools || []
  const families = catalog?.families || []
  const tiles = (
    <>
          <section className="ground-tile" data-tile="drawing" aria-label="Drawing">
            <h3>Drawing</h3>
            {drawing ? (
              <>
                <strong>{drawing.name}</strong>
                <p>{drawing.polylines} polylines · {drawing.layers} layers</p>
              </>
            ) : <p className="ground-empty">No drawing mounted</p>}
          </section>
          <section className="ground-tile" data-tile="versions" aria-label="Versions">
            <h3>Versions</h3>
            {versions.length ? (
              <>
                <strong>{versions.length} drawing version{versions.length === 1 ? '' : 's'}</strong>
                <ul>
                  {[...versions].slice(-3).reverse().map((version) => (
                    <li key={version.version_id} data-element-id={formatElementId('version', version.version_id) || undefined}>v{version.seq} · {shortId(version.drawing_id)}</li>
                  ))}
                </ul>
              </>
            ) : <p className="ground-empty">{workspace ? 'No versions yet' : 'Versions live with a workspace project'}</p>}
          </section>
          <section className="ground-tile" data-tile="jobs" aria-label="Jobs">
            <h3>Jobs</h3>
            {jobs.length ? (
              <ul>
                {jobs.map((job) => (
                  <li key={job.job_id} data-element-id={formatElementId('job', job.job_id) || undefined}>
                    <strong>{job.tool_name || job.kind}</strong> · {job.status || 'pending'}
                  </li>
                ))}
              </ul>
            ) : <p className="ground-empty">{workspace ? 'No jobs yet' : 'Runs appear here with a project open'}</p>}
          </section>
          <section className="ground-tile" data-tile="tools" aria-label="Built tools">
            <h3>Built tools</h3>
            {tools.length ? (
              <ul>{tools.map((tool, i) => {
                const realId = tool.tool_id || tool.name || ''
                return (
                  <li key={realId || i} data-element-id={(realId && formatElementId('tool', realId)) || undefined}>
                    {tool.name || tool.tool_id}
                  </li>
                )
              })}</ul>
            ) : <p className="ground-empty">{workspace ? 'No built tools yet' : 'Authored tools attach to the project'}</p>}
          </section>
          <section className="ground-tile" data-tile="catalog" aria-label="Catalog">
            <h3>Catalog</h3>
            {families.length ? (
              <>
                <strong>{families.length} {families.length === 1 ? 'family' : 'families'} · {capabilityTotal(families)} tools</strong>
                <ul>{families.map((family) => (
                  <li key={family.family_id} data-element-id={formatElementId('family', family.family_id) || undefined}>{family.label}
                    {studioPresentation && <ul className="ground-catalog-tools">
                      {(family.capabilities || []).map((tool) => (
                        <li key={tool.name} className="ground-catalog-tool">
                          <strong>{tool.label || tool.name}</strong>
                          <p>{tool.description || START_BOARD_COPY.missingDescription}</p>
                          <p>{isWriteTool(tool) ? START_BOARD_COPY.changesDrawing
                            : Array.isArray(tool.capabilities) && tool.capabilities.includes('drawing.read')
                              ? START_BOARD_COPY.readsDrawing : START_BOARD_COPY.unspecifiedDrawingEffect}</p>
                        </li>
                      ))}
                    </ul>}
                  </li>
                ))}</ul>
              </>
            ) : <p className="ground-empty">Loading the live catalog</p>}
          </section>
          <section className="ground-tile" data-tile="shared" aria-label="Shared everywhere">
            <h3>Shared everywhere</h3>
            <ul>{SHARED_WORKSPACE_CAPABILITIES.map((capability) => <li key={capability}>{capability}</li>)}</ul>
          </section>
    </>
  )
  return renderTile ? Children.toArray(tiles.props.children).map((tile) => renderTile(tile.props['data-tile'], tile)) : tiles
}
