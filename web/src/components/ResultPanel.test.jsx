import React from 'react'
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import ResultPanel from './ResultPanel.jsx'


afterEach(cleanup)


describe('ResultPanel actionable error guidance', () => {
  it('shows the next action and responsible actor from the shared envelope', () => {
    render(
      <ResultPanel
        running={false}
        tool={{ name: 'drape-onto-spheres' }}
        result={{
          ok: false,
          tool: 'drape-onto-spheres',
          version: '1.0.1',
          error: {
            error_code: 'ENTITLEMENT_REQUIRED',
            message: 'This workspace cannot run drawing-write tools.',
            retryable: false,
            retry_class: 'after_action',
            actor: 'workspace_admin',
            next_action: 'Enable this capability for the workspace, then retry.',
          },
          timing_ms: 0,
          cost: null,
          entitlement_required: 'drawing.write',
        }}
      />,
    )

    expect(screen.getByText(
      'Next: Enable this capability for the workspace, then retry.',
    )).toBeInTheDocument()
    expect(screen.getByText('Workspace admin')).toBeInTheDocument()
  })
})

