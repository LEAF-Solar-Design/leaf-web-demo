// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import CheckoutControls from './CheckoutControls.jsx'
import { DrawingIdentityProvider, useDrawingIdentity } from '../drawing/DrawingIdentityProvider.jsx'
import { setStoredOrgId } from '../api.js'

afterEach(cleanup)

describe('checkout controls drawing changes', () => {
  it('clears an error when the drawing identity changes', async () => {
    let identity
    function Controls() {
      identity = useDrawingIdentity()
      return <CheckoutControls onTake={vi.fn().mockRejectedValue(new Error('Failed to fetch'))} />
    }
    render(<DrawingIdentityProvider mode="console" search="?drawing=first" publicDemo={false} liveDemo={false}
      readAuthToken={() => null}><Controls /></DrawingIdentityProvider>)
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Take edit lock' })) })
    expect(screen.getByRole('alert')).toBeTruthy()
    act(() => identity.setFromUpload({ drawing_id: 'second' }))
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('resets pending state and ignores an old rejection while the new drawing has a request pending', async () => {
    let rejectOld
    let resolveNew
    const oldAction = vi.fn(() => new Promise((resolve, reject) => { rejectOld = reject }))
    const newAction = vi.fn(() => new Promise(resolve => { resolveNew = resolve }))
    const view = render(<CheckoutControls drawingKey="first" onTake={oldAction} />)
    fireEvent.click(screen.getByRole('button', { name: 'Take edit lock' }))
    expect(screen.getByRole('button').disabled).toBe(true)
    view.rerender(<CheckoutControls drawingKey="second" onTake={newAction} />)
    expect(screen.getByRole('button').disabled).toBe(false)
    fireEvent.click(screen.getByRole('button'))
    await act(async () => { rejectOld(new Error('Failed to fetch')) })
    expect(screen.queryByRole('alert')).toBeNull()
    expect(screen.getByRole('button').disabled).toBe(true)
    await act(async () => { resolveNew({ acquired: true }) })
    expect(screen.getByRole('button').disabled).toBe(false)
  })

  it('ignores an old refused take after moving to another drawing', async () => {
    let resolveOld
    const view = render(<CheckoutControls drawingKey="first" onTake={() => new Promise(resolve => { resolveOld = resolve })} />)
    fireEvent.click(screen.getByRole('button', { name: 'Take edit lock' }))
    view.rerender(<CheckoutControls drawingKey="second" onTake={vi.fn()} />)
    await act(async () => { resolveOld({ acquired: false }) })
    expect(screen.queryByRole('alert')).toBeNull()
    expect(screen.getByRole('button').disabled).toBe(false)
  })
})

describe('checkout controls account changes', () => {
  afterEach(() => localStorage.removeItem('leaf.jwt'))

  it.each([true, false])('handles a pending refusal with account changed: %s', async (changeAccount) => {
    localStorage.setItem('leaf.jwt', `header.${btoa(JSON.stringify({ sub: 'account-a' }))}.signature`)
    let rejectTake
    const onTake = vi.fn(() => new Promise((resolve, reject) => { rejectTake = reject }))
    const view = render(<CheckoutControls drawingKey="drawing-d" onTake={onTake} />)
    fireEvent.click(screen.getByRole('button', { name: 'Take edit lock' }))
    expect(screen.getByRole('button').disabled).toBe(true)
    view.container.tabIndex = -1
    view.container.focus()

    if (changeAccount) {
      act(() => {
        localStorage.setItem('leaf.jwt', `header.${btoa(JSON.stringify({ sub: 'account-b' }))}.signature`)
        window.dispatchEvent(new StorageEvent('storage', { key: 'leaf.jwt' }))
      })
    }
    view.rerender(<CheckoutControls drawingKey="drawing-d" onTake={onTake} />)
    expect(screen.getByRole('button').disabled).toBe(!changeAccount)
    expect(screen.queryByRole('alert')).toBeNull()
    expect(document.activeElement).toBe(view.container)

    await act(async () => { rejectTake(new Error('Failed to fetch')) })
    expect(screen.getByRole('button').disabled).toBe(false)
    if (changeAccount) {
      expect(screen.queryByRole('alert')).toBeNull()
      expect(document.activeElement).toBe(view.container)
    } else {
      expect(screen.getByRole('alert')).toBeTruthy()
    }
  })
})

describe('checkout controls organization changes', () => {
  afterEach(() => { localStorage.removeItem('leaf.jwt'); localStorage.removeItem('leaf.org_id') })

  it.each([true, false])('handles a deferred Take rejection with org changed: %s', async (changeOrg) => {
    const token = `header.${btoa(JSON.stringify({ iss: 'studio', sub: 'account-a', org_id: 'org1' }))}.signature`
    localStorage.setItem('leaf.jwt', token)
    setStoredOrgId('org1')
    let rejectTake
    render(<CheckoutControls drawingKey="drawing-d" onTake={() => new Promise((resolve, reject) => { rejectTake = reject })} />)
    fireEvent.click(screen.getByRole('button', { name: 'Take edit lock' }))
    expect(screen.getByRole('button').disabled).toBe(true)

    if (changeOrg) act(() => setStoredOrgId('org2'))
    expect(localStorage.getItem('leaf.jwt')).toBe(token)
    expect(screen.getByRole('button').disabled).toBe(!changeOrg)
    expect(screen.queryByRole('alert')).toBeNull()

    await act(async () => { rejectTake(new Error('Failed to fetch')) })
    expect(screen.getByRole('button').disabled).toBe(false)
    if (changeOrg) expect(screen.queryByRole('alert')).toBeNull()
    else expect(screen.getByRole('alert')).toBeTruthy()
  })
})

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
