// @vitest-environment jsdom
import React from 'react'
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import IosSurface from './IosSurface.jsx'
import { DeviceGround } from './DeviceGround.jsx'

afterEach(cleanup)

describe('ShipStage idle lane', () => {
  it('an idle lane shows the sign-in reason and no ladder', () => {
    render(<IosSurface enabled ship={{ phase: 'idle', readiness: null, sources: [], approvals: [], execution: null, receipt: null }} />)
    expect(screen.getByRole('status').textContent).toBe('Sign in and select an approved revision to launch.')
    expect(screen.queryByTestId('ios-stage-ladder')).toBeNull()
    expect(screen.queryByTestId('ios-ship-sources')).toBeNull()
    expect(screen.queryByText(/missing|unavailable|No imported source revision/)).toBeNull()
    expect(screen.queryByRole('button')).toBeNull()
  })

  it('the device ground shows the same idle reason', () => {
    render(<DeviceGround active enabled ship={{ phase: 'idle', readiness: null }} />)
    expect(screen.getByText('Sign in and select an approved revision to launch.')).toBeInTheDocument()
    expect(screen.queryByTestId('ios-stage-ladder')).toBeNull()
  })

  it('a non-idle lane keeps its ladder', () => {
    render(<IosSurface enabled ship={{ phase: 'setup-required', readiness: { setupState: 'no-approved-revision' } }} />)
    expect(screen.getByTestId('ios-stage-ladder')).toBeInTheDocument()
    expect(screen.getByRole('status').textContent).toBe('setup-required')
  })
})
