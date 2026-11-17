import { describe, expect, it } from 'vitest'

import { compactRefusal, errorActorLabel, errorPresentation } from './errorPresentation.js'

describe('shared error presentation', () => {
  it.each(['SOLAREDGE_GRID_UNMATCHED', 'SOLAREDGE_GRID_AMBIGUOUS', 'CLOUD_REQUEST_REJECTED', 'solve_outcome_unknown'])(
    'shows the specific product cause %s and keeps its object and action', (reason_code) => {
      const error = { reason_code, error_code: 'GRAPH_JOB_FAILED',
        message: 'Panel group A needs a grid.', next_action: 'Choose a grid.' }
      for (const input of [error, { error }, { body: { error } }]) {
        expect(errorPresentation(input)).toMatchObject({ code: reason_code,
          message: error.message, nextAction: error.next_action })
      }
    },
  )

  it('keeps the explicit reason override and ignores malformed product codes', () => {
    expect(errorPresentation({ reason_code: 'INNER' }, '', 'EXPLICIT').code).toBe('EXPLICIT')
    for (const reason_code of [{ bad: 1 }, '<invalid>', '', 'a'.repeat(129)]) {
      expect(errorPresentation({ reason_code, error_code: 'GRAPH_JOB_FAILED' }).code).toBe('GRAPH_JOB_FAILED')
    }
  })

  it('reads the typed error nested on a thrown API error', () => {
    const error = new Error('legacy text')
    error.body = {
      error: {
        error_code: 'AUTHORING_DISABLED',
        message: 'Tool authoring is disabled for this workspace.',
        retryable: false,
        actor: 'workspace_admin',
        next_action: 'Enable authoring in account controls.',
      },
    }

    expect(errorPresentation(error)).toEqual({
      message: 'Tool authoring is disabled for this workspace.',
      code: 'AUTHORING_DISABLED',
      retryable: false,
      actor: 'workspace_admin',
      nextAction: 'Enable authoring in account controls.',
    })
    expect(errorActorLabel('workspace_admin')).toBe('Workspace admin')
  })

  it('keeps a plain transport error usable', () => {
    expect(errorPresentation(new Error('Network unavailable.'))).toMatchObject({
      message: 'Network unavailable.',
      code: '',
      nextAction: '',
      actor: '',
    })
  })
})

describe('compact X02 refusals', () => {
  const cases = [
    ['LAYER_NOT_FOUND', 'Layer Roof-West does not exist.', 'Choose an existing layer.'],
    ['PANEL_LAYER_FILTER_REQUIRED', 'No panel layer filter is set (panel_layer_contains).', 'Set panel_layer_contains in Solar settings.'],
    ['NO_PANELS_RECOGNISED', 'No panels matched panel_layer_contains="PV-West".', 'Choose a matching panel layer filter.'],
    ['CLOUD_REQUEST_REJECTED', 'Cloud request for array West rejected: capacity exceeds limit.', 'Correct the cloud request.'],
    ['SOLVE_TIMEOUT', 'Solve for array West timed out.', 'Reconcile the solve status before retrying.'],
    ['solve_outcome_unknown', 'Solve for array West has an unknown outcome.', 'Reconcile the solve status before retrying.'],
    ['INVALID_SEED_REQUEST', 'Solar setup was refused: units.crs is not valid.', 'Correct the Solar setup field.'],
    ['UNKNOWN_UNITS', 'The drawing units in project.units are not recognised.', 'Set recognised drawing units.'],
    ['SIP_R1_CHECKOUT_EXPIRED', 'Your drawing checkout expired.', 'Take the drawing checkout again.'],
    ['IMPORT_PDF_MALFORMED', 'PDF Roof-West.pdf is malformed: no pages.', 'Upload a valid PDF.'],
  ]

  it.each(cases)('keeps the cause and named object for %s', (reason_code, message, action) => {
    const refusal = compactRefusal({ reason_code, error_code: 'INTERNAL', message })
    expect(refusal).toMatchObject({ code: reason_code, text: `${message} ${action}` })
    expect(refusal.text.length).toBeLessThanOrEqual(160)
  })

  it('follows nested own data bodies on Errors and gives reason codes precedence', () => {
    const input = new Error('POST /api/run -> 502')
    input.body = { error_code: 'SOLVE_TIMEOUT', body: { error: {
      reason_code: 'LAYER_NOT_FOUND', message: 'Layer West does not exist.',
    } } }
    Object.defineProperty(input, 'error', { get() { throw new Error('accessor read') } })
    expect(compactRefusal(input)?.text).toBe('Layer West does not exist. Choose an existing layer.')
  })

  it('does not duplicate server actions', () => {
    expect(compactRefusal({ reason_code: 'SIP_R1_CHECKOUT_EXPIRED',
      message: 'Your drawing checkout expired. Take the checkout again, then retry.',
      next_action: 'Take the checkout again, then retry.' })?.text)
      .toBe('Your drawing checkout expired. Take the drawing checkout again.')
  })

  it('does not replace an unrelated specific reason with a generic recognised class', () => {
    expect(compactRefusal({ reason_code: 'AUTHORING_DISABLED', error_code: 'SOLVE_TIMEOUT' })).toBeNull()
  })

  it.each([199, 200, 201, 4096])('marks over-budget identities unresolved (%i units)', (length) => {
    const notice = compactRefusal({ reason_code: 'LAYER_NOT_FOUND', message: `Layer ${'x'.repeat(length)} missing.` })
    expect(notice.text).toContain('identity unresolved')
    expect(notice.text).toContain('Choose an existing layer.')
    expect(notice.text.length).toBeLessThanOrEqual(160)
  })

  it('removes markup and hidden control characters while preserving Unicode names', () => {
    const notice = compactRefusal({ reason_code: 'LAYER_NOT_FOUND', message: '<b>Layer 屋根😀</b>\u0000\u202e is missing.' })
    expect(notice.text).toBe('Layer 屋根😀 is missing. Choose an existing layer.')
    expect(notice.text).not.toMatch(/[<>\p{Cc}\p{Cf}]/u)
  })

  it('bounds cycles and deep bodies and ignores inherited fields', () => {
    const cycle = {}
    cycle.body = cycle
    expect(compactRefusal(cycle)).toBeNull()
    let deep = { reason_code: 'SOLVE_TIMEOUT' }
    for (let index = 0; index < 100; index += 1) deep = { body: deep }
    expect(compactRefusal(deep)).toBeNull()
    expect(compactRefusal(Object.create({ reason_code: 'SOLVE_TIMEOUT' }))).toBeNull()
    expect(compactRefusal({ reason_code: 'SOLVE_TIMEOUT' }).text).toContain('identity unresolved')
  })

  it.each([
    ['SOLAREDGE_GRID_UNMATCHED', [], 'redraw_frame', 'Group frame:A-7: no matching SolarEdge grid. Redraw this group.'],
    ['SOLAREDGE_GRID_AMBIGUOUS', [4, 17], 'pick_candidate', 'Group frame:A-7: multiple SolarEdge grids (candidates 4, 17). Select one grid.'],
  ])('formats the verified typed carrier %s', (reason_code, candidate_ids, action, text) => {
    expect(compactRefusal({ reason_code, frame_ref: 'frame:A-7', candidate_ids, action })?.text).toBe(text)
  })

  it.each([
    {}, { frame_ref: '<fake>' }, { action: 'redraw_frame' }, { candidate_ids: [] },
    { candidate_ids: [4, 4] }, { candidate_ids: ['4', 17] }, { candidate_ids: [-1, 17] },
    { frame_ref: 'x'.repeat(128) },
  ])('uses explicit unresolved group guidance for missing, invalid or oversized identity %j', (override) => {
    const carrier = { reason_code: 'SOLAREDGE_GRID_AMBIGUOUS', frame_ref: 'frame:A-7', candidate_ids: [4, 17], action: 'pick_candidate', ...override }
    if (Object.keys(override).length === 0) delete carrier.frame_ref
    const notice = compactRefusal(carrier)
    expect(notice.text).toBe('Group identity unresolved: multiple SolarEdge grids. Select one grid.')
    expect(notice.text.length).toBeLessThanOrEqual(160)
  })
})
