// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import CheckoutControls from './CheckoutControls.jsx'

afterEach(cleanup)

describe('checkout read failure banner', () => {
  it('keeps the sentence and Retry without a code when no record is supplied', () => {
    render(<CheckoutControls unknown readFailed onRetry={vi.fn()} />)
    expect(screen.queryByTestId('checkout-failure-code')).toBeNull()
    expect(screen.getByText(/Could not read the edit lock/)).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Retry' })).toBeTruthy()
  })

  it.each([
    { status: 401, errorCode: 'UNAUTHENTICATED', errorId: null },
    { status: 503, errorCode: null, errorId: '0123456789abcdef' },
    { status: null, errorCode: null, errorId: null },
  ])('keeps the supplied failure record off screen %j', (failure) => {
    render(<CheckoutControls unknown readFailed failure={failure} onRetry={vi.fn()} />)
    expect(screen.queryByTestId('checkout-failure-code')).toBeNull()
    expect(document.body.textContent).not.toMatch(/UNAUTHENTICATED|HTTP|401|503|error_id|0123456789abcdef|code local/)
    expect(screen.getByText(/Could not read the edit lock/)).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Retry' })).toBeTruthy()
  })
})
