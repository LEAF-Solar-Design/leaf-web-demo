// The ONE derivation of "what is actually open here", consumed by every
// surface that shows it: the header project chip, the F-8 continuity rail, and
// the Browser / Solar CAD surface cards.
//
// WHY THIS MODULE EXISTS. On 2026-09-01 production showed, on ONE screen,
// "Project rooftop_demo · 2345 polylines · 4 layers" in the header while three
// cards said "No project open". Every one of them was rendering truthfully —
// over two DIFFERENT concepts the UI never named:
//
//   drawing            the CAD scene mounted right now (rooftop_demo). Fully
//                      editable on its own. Served by the legacy da/store.py
//                      authority, which has no project_id column at all
//                      (server/routers/drawings.py likewise), so a mounted
//                      drawing is genuinely NOT project-scoped.
//   workspace project  the org-scoped container (platform/models.py Project)
//                      that adds project-scoped files, conversation,
//                      annotations, and versions AROUND a drawing.
//
// The contradiction was three call sites each inventing their own phrasing for
// two unnamed things. There is exactly one derivation now: a fourth consumer
// must read from here, never add a fourth vocabulary.
//
// Pure, no I/O, no allocation on any path but the one frozen result — this runs
// on every render of the header chip. Fails closed: unrecognised or absent
// input degrades to the honest 'empty' state, never to an invented project.

// Bounded so an accidental or hostile 10k-character name cannot blow out the
// header chip's single line. The full name still lives in the switcher menu.
const MAX_LABEL = 64

export const WORKSPACE_PROJECT_COPY = Object.freeze({
  // Named so the two concepts can never be read as one. "workspace project" is
  // deliberately longer than "project": the extra word IS the disambiguation.
  drawingOnlyHeadline: 'No workspace project',
  drawingOnlyExplainer:
    'This drawing is open and editable in CAD. A workspace project is the container that adds '
    + 'project-scoped files, conversation, and annotations around it.',
  drawingOnlyRail: 'no workspace project',
  emptyHeadline: 'No project open',
  emptyExplainer: 'Open a project from the header, or add a drawing to start working in CAD.',
  emptyRail: 'no project open',
  actionLabel: 'Create project from this drawing',
  // Every disabled reason names the blocker AND where the user goes next.
  // A bare "unavailable" is what makes a pilot user retry forever.
  reasonNoPlatform: 'Workspace projects need the platform database, which this deployment is running without.',
  reasonNoOrg: 'Create a workspace first — the Project menu in the header offers it.',
  reasonDemo: 'This is the offline demo build; it talks to no workspace service.',
  reasonNoHandler: 'This surface is not wired to create projects yet.',
})

function trimmedLabel(value) {
  const text = String(value ?? '').trim()
  if (!text) return null
  return text.length > MAX_LABEL ? `${text.slice(0, MAX_LABEL - 1)}…` : text
}

// Identifiers get the SAME normalization as names. Trimming names but not ids
// was an inconsistency this module could not survive: a whitespace-only
// openProjectId is truthy in JS, so '   ' plus a valid name would have invented
// an open project out of a blank identifier, and a whitespace-only orgId would
// have enabled a create action the server must then reject. Both are exactly
// the "invented state" this module exists to prevent. Ids are not truncated —
// a truncated id is a WRONG id, so an over-long one is passed through intact
// and only the label it produces is bounded.
function presentId(value) {
  const text = String(value ?? '').trim()
  return text || null
}

/**
 * Derive the single legible workspace-project state.
 *
 * @param {object}  input
 * @param {string?} input.openProjectId       id of the OPEN workspace project, if any
 * @param {string?} input.projectName         that project's display name
 * @param {string?} input.drawingName         the MOUNTED drawing's name/id (never a project)
 * @param {string?} input.orgId               the workspace org of record
 * @param {string?} input.projectsUnavailable why the project list could not load
 * @param {boolean} input.mock                offline demo build (no platform at all)
 * @returns {Readonly<object>} frozen state; `kind` is always one of
 *          'project' | 'drawing-only' | 'empty'
 */
export function deriveWorkspaceProjectState({
  openProjectId = null,
  projectName = null,
  drawingName = null,
  orgId = null,
  projectsUnavailable = null,
  mock = false,
} = {}) {
  const drawing = trimmedLabel(drawingName)
  const project = trimmedLabel(projectName)
  const projectId = presentId(openProjectId)
  const org = presentId(orgId)

  // A workspace project is open only when BOTH the id and a name resolve; an id
  // with no name yet is still hydrating, and labelling that "Project —" is how
  // the old chip started lying in the first place.
  if (projectId && project) {
    return Object.freeze({
      kind: 'project',
      tag: 'Project',
      label: project,
      drawingName: drawing,
      railLabel: null,
      headline: project,
      explainer: null,
      action: null,
    })
  }

  if (drawing) {
    return Object.freeze({
      kind: 'drawing-only',
      tag: 'Drawing',
      label: drawing,
      drawingName: drawing,
      railLabel: WORKSPACE_PROJECT_COPY.drawingOnlyRail,
      headline: WORKSPACE_PROJECT_COPY.drawingOnlyHeadline,
      explainer: WORKSPACE_PROJECT_COPY.drawingOnlyExplainer,
      action: Object.freeze({
        label: WORKSPACE_PROJECT_COPY.actionLabel,
        // The suggested name; the caller owns the actual create call.
        projectName: drawing,
        disabled: Boolean(mock || projectsUnavailable || !org),
        reason: mock
          ? WORKSPACE_PROJECT_COPY.reasonDemo
          : projectsUnavailable
            ? WORKSPACE_PROJECT_COPY.reasonNoPlatform
            : !org
              ? WORKSPACE_PROJECT_COPY.reasonNoOrg
              : null,
      }),
    })
  }

  // `label` is deliberately null here even when a projectName was passed: a
  // name with no open id is NOT an open project, and putting it on the header
  // chip is exactly the lie this module exists to stop. Consumers render their
  // own neutral placeholder.
  return Object.freeze({
    kind: 'empty',
    tag: 'Project',
    label: null,
    drawingName: null,
    railLabel: WORKSPACE_PROJECT_COPY.emptyRail,
    headline: WORKSPACE_PROJECT_COPY.emptyHeadline,
    explainer: WORKSPACE_PROJECT_COPY.emptyExplainer,
    action: null,
  })
}

// The one shared resting state. Consumers with nothing to show read THIS rather
// than calling the derivation with synthesized input: the previous revision had
// the continuity rail pass a literal 'legacy' as an openProjectId, fabricating
// an identifier inside the very module that exists to stop invented state.
// Frozen and module-level, so it also costs no per-render allocation.
export const EMPTY_WORKSPACE_PROJECT = deriveWorkspaceProjectState({})
