/**
 * F-9 acceptance: the ONE derivation that resolves the 2026-09-01 production
 * contradiction — header "Project rooftop_demo" over three cards saying
 * "No project open".
 *
 * The load-bearing assertion is that a mounted drawing with no workspace
 * project is NEVER described as having nothing open, and is NEVER described as
 * a project. Both halves have to hold: dropping either one recreates the bug.
 */
import { describe, expect, it } from 'vitest'
import { deriveWorkspaceProjectState, EMPTY_WORKSPACE_PROJECT, WORKSPACE_PROJECT_COPY } from './workspaceProjectState.js'

describe('deriveWorkspaceProjectState', () => {
  it('the observed production state: a mounted drawing, no workspace project', () => {
    const state = deriveWorkspaceProjectState({
      openProjectId: null,
      projectName: null,
      drawingName: 'rooftop_demo',
      orgId: 'org-1',
    })
    expect(state.kind).toBe('drawing-only')
    // The header half of the bug: the chip must not call a drawing a project.
    expect(state.tag).toBe('Drawing')
    expect(state.label).toBe('rooftop_demo')
    // The cards half: never "nothing is open" while a drawing plainly is.
    expect(state.headline).toBe('No workspace project')
    expect(state.railLabel).toBe('no workspace project')
    expect(state.explainer).toContain('open and editable in CAD')
    expect(state.action.label).toBe('Create project from this drawing')
    expect(state.action.disabled).toBe(false)
    expect(state.action.projectName).toBe('rooftop_demo')
  })

  it('an open workspace project is named as one, and offers no create action', () => {
    const state = deriveWorkspaceProjectState({
      openProjectId: 'p-1',
      projectName: 'Maple St retrofit',
      drawingName: 'rooftop_demo',
      orgId: 'org-1',
    })
    expect(state.kind).toBe('project')
    expect(state.tag).toBe('Project')
    expect(state.label).toBe('Maple St retrofit')
    expect(state.action).toBeNull()
    // The drawing is still carried, so a consumer can show both.
    expect(state.drawingName).toBe('rooftop_demo')
  })

  it('nothing mounted and nothing open stays the plain empty state', () => {
    const state = deriveWorkspaceProjectState({})
    expect(state.kind).toBe('empty')
    expect(state.railLabel).toBe('no project open')
    expect(state.headline).toBe('No project open')
    expect(state.action).toBeNull()
  })

  it('a project id with no name yet is NOT announced as an open project', () => {
    // Mid-hydration. Calling this "Project —" is how the old chip started
    // lying; it degrades to the drawing that genuinely is mounted.
    const state = deriveWorkspaceProjectState({
      openProjectId: 'p-1', projectName: null, drawingName: 'rooftop_demo',
    })
    expect(state.kind).toBe('drawing-only')
  })

  it.each([
    ['no platform database', { projectsUnavailable: 'Projects unavailable', orgId: 'org-1' }, WORKSPACE_PROJECT_COPY.reasonNoPlatform],
    ['no workspace org yet', { orgId: null }, WORKSPACE_PROJECT_COPY.reasonNoOrg],
    ['the offline demo build', { mock: true, orgId: 'org-1' }, WORKSPACE_PROJECT_COPY.reasonDemo],
  ])('the action is disabled WITH a stated reason when %s', (_label, extra, reason) => {
    const state = deriveWorkspaceProjectState({ drawingName: 'rooftop_demo', ...extra })
    expect(state.kind).toBe('drawing-only')
    expect(state.action.disabled).toBe(true)
    // A disabled button with no reason is the same dead end as the bare
    // "No project open" line this replaced.
    expect(state.action.reason).toBe(reason)
  })

  it('a hostile or accidental long name cannot blow out the header chip line', () => {
    const state = deriveWorkspaceProjectState({ drawingName: 'x'.repeat(5000) })
    expect(state.label.length).toBeLessThanOrEqual(64)
    expect(state.label.endsWith('…')).toBe(true)
  })

  it('blank and whitespace-only names fall through to the honest empty state', () => {
    expect(deriveWorkspaceProjectState({ drawingName: '   ' }).kind).toBe('empty')
    expect(deriveWorkspaceProjectState({ openProjectId: 'p-1', projectName: '  ' }).kind).toBe('empty')
  })

  it.each([
    ['spaces', '   '],
    ['tab', String.fromCharCode(9)],
    ['newline', String.fromCharCode(10)],
  ])(
    'a whitespace-only openProjectId (%s) never invents an open project',
    (_label, blank) => {
      // sol-critic finding 1: a whitespace id is TRUTHY in JS, so trimming
      // names but not ids would have opened a project from a blank identifier.
      const state = deriveWorkspaceProjectState({
        openProjectId: blank, projectName: 'Maple St retrofit', drawingName: 'rooftop_demo',
      })
      expect(state.kind).toBe('drawing-only')
    },
  )

  it('a whitespace-only orgId does not enable a create the server must reject', () => {
    const state = deriveWorkspaceProjectState({ drawingName: 'rooftop_demo', orgId: '   ' })
    expect(state.action.disabled).toBe(true)
    expect(state.action.reason).toBe(WORKSPACE_PROJECT_COPY.reasonNoOrg)
  })

  it('an over-long id is never truncated — a truncated id is a WRONG id', () => {
    const id = 'p-'.padEnd(500, '9')
    const state = deriveWorkspaceProjectState({ openProjectId: id, projectName: 'Maple St' })
    expect(state.kind).toBe('project')
    expect(state.label).toBe('Maple St')
  })

  it('EMPTY_WORKSPACE_PROJECT is the shared frozen resting state', () => {
    expect(EMPTY_WORKSPACE_PROJECT.kind).toBe('empty')
    expect(Object.isFrozen(EMPTY_WORKSPACE_PROJECT)).toBe(true)
    // Same object every read: consumers must not allocate one per render.
    expect(EMPTY_WORKSPACE_PROJECT).toBe(EMPTY_WORKSPACE_PROJECT)
  })

  it('a project NAME with no open id never reaches the header chip label', () => {
    // The whole failure mode in one line: a name that is not open must not be
    // rendered as though it were.
    const state = deriveWorkspaceProjectState({ openProjectId: null, projectName: 'Maple St retrofit' })
    expect(state.kind).toBe('empty')
    expect(state.label).toBeNull()
  })

  it('the result is frozen, so no consumer can mutate a shared state object', () => {
    const state = deriveWorkspaceProjectState({ drawingName: 'rooftop_demo' })
    expect(Object.isFrozen(state)).toBe(true)
    expect(Object.isFrozen(state.action)).toBe(true)
  })

  it('"workspace project" and "project" are kept distinct in the copy itself', () => {
    // The extra word IS the disambiguation. If a future edit collapses these
    // two strings the contradiction comes straight back.
    expect(WORKSPACE_PROJECT_COPY.drawingOnlyRail).not.toBe(WORKSPACE_PROJECT_COPY.emptyRail)
    expect(WORKSPACE_PROJECT_COPY.drawingOnlyHeadline).toContain('workspace project')
  })
})
