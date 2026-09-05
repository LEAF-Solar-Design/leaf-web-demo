// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import ChangeCapsule, { acceptGate, capsulePhase, CHANGE_CAPSULE_REASONS } from './ChangeCapsule.jsx'

afterEach(cleanup)

const annotation = (over = {}) => ({
  decisionCopy: 'Move panel AB12 by 2ft.',
  batchId: 'batch-1',
  revision: 1,
  state: 'pending',
  baseVersion: 3,
  targetVersion: 4,
  ...over,
})

const entityIdentity = { kind: 'entity', id: 'AB12' }
const base = { polylines: [{ handle: 'AB12', pts: [0, 0] }, { handle: 'CD34', pts: [1, 1] }] }
const scopedCandidate = { polylines: [{ handle: 'AB12', pts: [9, 9] }, { handle: 'CD34', pts: [1, 1] }] }
const outOfScopeCandidate = { polylines: [{ handle: 'AB12', pts: [9, 9] }, { handle: 'CD34', pts: [8, 8] }] }

describe('capsulePhase (pure)', () => {
  it('walks every transition', () => {
    expect(capsulePhase({ annotation: null, busy: false, error: null })).toBe('empty')
    expect(capsulePhase({ annotation: annotation(), busy: false, error: null })).toBe('pending')
    expect(capsulePhase({ annotation: annotation(), busy: true, error: null })).toBe('applying')
    expect(capsulePhase({ annotation: annotation({ state: 'accepted' }), busy: false, error: null })).toBe('accepted')
    expect(capsulePhase({ annotation: annotation({ state: 'rejected' }), busy: false, error: null })).toBe('rejected')
    expect(capsulePhase({ annotation: annotation({ state: 'expired' }), busy: false, error: null })).toBe('expired')
    expect(capsulePhase({ annotation: annotation({ state: 'stale' }), busy: false, error: null })).toBe('stale')
    expect(capsulePhase({ annotation: annotation(), busy: false, error: 'boom' })).toBe('error')
    // error takes precedence over busy: a failed action is not "still applying"
    expect(capsulePhase({ annotation: annotation(), busy: true, error: 'boom' })).toBe('error')
  })
})

describe('acceptGate (pure containment)', () => {
  it('offers nothing outside the pending phase', () => {
    expect(acceptGate({ identity: entityIdentity, phase: 'accepted', intakes: null })).toEqual({
      allowed: false, reason: null, outside: [],
    })
  })

  it('refuses a kind with no containment mechanism, honestly', () => {
    const gate = acceptGate({ identity: { kind: 'tool', id: 'fit' }, phase: 'pending', intakes: null })
    expect(gate).toEqual({ allowed: false, reason: CHANGE_CAPSULE_REASONS.noContainmentForKind, outside: [] })
  })

  it('refuses when no scoped-intake adapter answered', () => {
    const gate = acceptGate({ identity: entityIdentity, phase: 'pending', intakes: null })
    expect(gate.reason).toBe(CHANGE_CAPSULE_REASONS.noScopedDiff)
  })

  it('allows a delta scoped to exactly the target handle', () => {
    const gate = acceptGate({
      identity: entityIdentity, phase: 'pending',
      intakes: { baseIntake: base, candidateIntake: scopedCandidate },
    })
    expect(gate).toEqual({ allowed: true, reason: null, outside: [] })
  })

  it('refuses a delta that touches another handle, naming it', () => {
    const gate = acceptGate({
      identity: entityIdentity, phase: 'pending',
      intakes: { baseIntake: base, candidateIntake: outOfScopeCandidate },
    })
    expect(gate.allowed).toBe(false)
    expect(gate.reason).toBe(CHANGE_CAPSULE_REASONS.outOfScope)
    expect(gate.outside).toEqual([{ key: 'polylines:h:CD34', handle: 'CD34', change: 'modified' }])
  })
})

function Harness({
  identity = entityIdentity,
  ann = annotation(),
  busy = false,
  error = null,
  confirmation = null,
  resolveScopedIntakes,
  onAccept = vi.fn(),
  onReject = vi.fn(),
  onRetry = vi.fn(),
  onUndo = vi.fn(),
  onEdit = vi.fn(),
  onPreview = vi.fn(),
}) {
  return (
    <ChangeCapsule
      identity={identity}
      annotation={ann}
      busy={busy}
      error={error}
      confirmation={confirmation}
      onPreview={onPreview}
      onAccept={onAccept}
      onReject={onReject}
      onRetry={onRetry}
      onUndo={onUndo}
      onEdit={onEdit}
      resolveScopedIntakes={resolveScopedIntakes}
    />
  )
}

describe('ChangeCapsule (mounted)', () => {
  it('renders nothing with no annotation', () => {
    render(<Harness ann={null} />)
    expect(screen.queryByTestId('change-capsule')).toBeNull()
  })

  it('shows the proposal, the affected element, and a disabled Accept with the honest reason', () => {
    render(<Harness resolveScopedIntakes={() => null} />)
    expect(screen.getByTestId('change-capsule-proposal').textContent).toBe('Move panel AB12 by 2ft.')
    expect(screen.getByTestId('change-capsule-artifact').textContent).toBe('Affects entity AB12')
    const accept = screen.getByTestId('change-capsule-accept')
    expect(accept).toBeDisabled()
    expect(accept.getAttribute('data-reason')).toBe(CHANGE_CAPSULE_REASONS.noScopedDiff)
  })

  it('enables Accept once the scoped delta touches only this handle', () => {
    render(<Harness resolveScopedIntakes={() => ({ baseIntake: base, candidateIntake: scopedCandidate })} />)
    expect(screen.getByTestId('change-capsule-accept')).not.toBeDisabled()
  })

  it('refuses accept and names the outside handle when the delta reaches beyond the element', () => {
    render(<Harness resolveScopedIntakes={() => ({ baseIntake: base, candidateIntake: outOfScopeCandidate })} />)
    const accept = screen.getByTestId('change-capsule-accept')
    expect(accept).toBeDisabled()
    expect(accept.getAttribute('data-reason')).toBe(CHANGE_CAPSULE_REASONS.outOfScope)
    expect(screen.getByTestId('change-capsule-outside').textContent).toBe('Also touches: CD34')
  })

  it('shows the applying phase as unsettled while busy, then reconciles to the receipt', () => {
    const { rerender } = render(<Harness busy ann={annotation()} />)
    expect(screen.getByTestId('change-capsule-status').textContent).toBe('Applying — not yet confirmed by the server.')
    rerender(<Harness busy={false} ann={annotation({ state: 'accepted', targetVersion: 4 })}
      confirmation={{ kind: 'apply', revision: 2, targetVersion: 4 }} />)
    expect(screen.getByTestId('change-capsule-status').textContent).toBe('Accepted and confirmed by the server.')
    expect(screen.getByText('Confirmed at revision 2, target version 4.')).toBeTruthy()
  })

  it('calls onAccept only through the button, never bypassing the disabled state', () => {
    const onAccept = vi.fn()
    render(<Harness resolveScopedIntakes={() => null} onAccept={onAccept} />)
    fireEvent.click(screen.getByTestId('change-capsule-accept'))
    expect(onAccept).not.toHaveBeenCalled()
  })

  it('offers Edit and Reject while pending, Retry once rejected, Undo once accepted', () => {
    const { rerender } = render(<Harness />)
    expect(screen.getByTestId('change-capsule-edit')).toBeTruthy()
    rerender(<Harness ann={annotation({ state: 'rejected' })} />)
    expect(screen.getByRole('button', { name: 'Retry as a fresh proposal' })).toBeTruthy()
    rerender(<Harness ann={annotation({ state: 'accepted' })} />)
    expect(screen.getByTestId('change-capsule-undo')).toBeTruthy()
  })

  it('dismiss hides the capsule locally without touching the annotation', () => {
    render(<Harness />)
    fireEvent.click(screen.getByTestId('change-capsule-dismiss'))
    expect(screen.queryByTestId('change-capsule')).toBeNull()
  })
})
