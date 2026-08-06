import { describe, expect, it } from 'vitest'

import { errorActorLabel, errorPresentation } from './errorPresentation.js'

describe('shared error presentation', () => {
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
