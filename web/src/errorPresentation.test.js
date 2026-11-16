import { describe, expect, it } from 'vitest'

import { errorActorLabel, errorPresentation } from './errorPresentation.js'

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
