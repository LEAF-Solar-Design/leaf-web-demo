// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { webcrypto, createHash } from 'node:crypto'
import { TextEncoder } from 'node:util'

import ChangeCapsule, { acceptGate, capsulePhase, CHANGE_CAPSULE_REASONS } from './ChangeCapsule.jsx'
import { getSurfaceConfig, submitSurfaceConfig } from '../api.js'
import { refreshSurfaceConfigOverlay } from '../site/useSurfaceContract.js'

vi.mock('../api.js', () => ({ getSurfaceConfig: vi.fn(), submitSurfaceConfig: vi.fn() }))
vi.mock('../site/useSurfaceContract.js', async (importOriginal) => ({
  ...await importOriginal(),
  refreshSurfaceConfigOverlay: vi.fn().mockResolvedValue(undefined),
}))

afterEach(cleanup)
afterEach(() => { vi.clearAllMocks(); vi.unstubAllGlobals() })

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

describe('surface-config commit', () => {
  const overlay = { cad: { authoring: false } }
  const proposal = () => annotation({ kind: 'surface-config', baseSha256: 'a'.repeat(64), overlay })

  it('refuses a stale base with the literal reason and never submits', async () => {
    getSurfaceConfig.mockResolvedValue({ surfaces: {}, source: { sha256: 'b'.repeat(64) } })
    render(<ChangeCapsule annotation={proposal()} />)
    fireEvent.click(screen.getByTestId('change-capsule-accept'))
    expect(await screen.findByRole('alert')).toHaveTextContent(CHANGE_CAPSULE_REASONS.staleBase)
    expect(submitSurfaceConfig).not.toHaveBeenCalled()
    expect(getSurfaceConfig).toHaveBeenCalledWith(false, { fresh: true })
    expect(screen.getByTestId('change-capsule-accept')).toBeDisabled()
  })

  it('submits a matching base once, refreshes, and displays the file receipt', async () => {
    vi.stubGlobal('crypto', webcrypto)
    vi.stubGlobal('TextEncoder', TextEncoder)
    getSurfaceConfig.mockResolvedValue({ surfaces: {}, source: { sha256: 'a'.repeat(64) } })
    const receipt = { sha256: 'c'.repeat(64), authored_at: '2026-09-06T12:00:00+00:00' }
    submitSurfaceConfig.mockResolvedValue(receipt)
    const onAccept = vi.fn()
    render(<ChangeCapsule annotation={proposal()} onAccept={onAccept} />)
    const button = screen.getByTestId('change-capsule-accept')
    fireEvent.click(button)
    fireEvent.click(button)
    expect(await screen.findByText(`Surface config committed: cccccccc, authored ${receipt.authored_at}.`)).toBeTruthy()
    expect(submitSurfaceConfig).toHaveBeenCalledTimes(1)
    expect(submitSurfaceConfig).toHaveBeenCalledWith(overlay)
    expect(refreshSurfaceConfigOverlay).toHaveBeenCalledTimes(1)
    expect(onAccept).not.toHaveBeenCalled()
    expect(screen.queryByTestId('change-capsule-accept')).toBeNull()
    expect(screen.queryByTestId('change-capsule-undo')).toBeNull()
  })

  it.each(['rejects', 'has no receipt'])('stays unconfirmed when submit %s and refresh confirms the submitted digest', async (failure) => {
    vi.stubGlobal('crypto', webcrypto)
    vi.stubGlobal('TextEncoder', TextEncoder)
    getSurfaceConfig.mockResolvedValue({ surfaces: {}, source: { sha256: 'a'.repeat(64) } })
    if (failure === 'rejects') submitSurfaceConfig.mockRejectedValue(new Error('lost response'))
    else submitSurfaceConfig.mockResolvedValue(null)
    render(<ChangeCapsule annotation={proposal()} />)
    fireEvent.click(screen.getByTestId('change-capsule-accept'))
    expect(await screen.findByRole('alert')).toHaveTextContent(CHANGE_CAPSULE_REASONS.commitFailed)
    expect(screen.getByTestId('change-capsule-status')).toHaveTextContent(CHANGE_CAPSULE_REASONS.commitFailed)
    expect(screen.queryByText(/Nothing has changed yet/)).toBeNull()
    expect(screen.queryByTestId('change-capsule-accept')).toBeNull()
    getSurfaceConfig.mockResolvedValue({ surfaces: {}, source: { sha256: 'b'.repeat(64) } })
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
    expect(await screen.findByText(`Live SHA-256: ${'b'.repeat(64)}`)).toBeTruthy()
    expect(screen.queryByText(/Nothing has changed yet/)).toBeNull()
    const receipt = {
      sha256: createHash('sha256').update(JSON.stringify(overlay, null, 2) + '\n').digest('hex'),
      authored_at: '2026-09-06T12:00:00+00:00',
    }
    getSurfaceConfig.mockResolvedValue({ surfaces: overlay, source: receipt })
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
    expect(await screen.findByText(`Surface config committed: ${receipt.sha256.slice(0, 8)}, authored ${receipt.authored_at}.`)).toBeTruthy()
    expect(refreshSurfaceConfigOverlay).toHaveBeenCalledTimes(2)
    expect(getSurfaceConfig).toHaveBeenLastCalledWith(false, { fresh: true })
    expect(submitSurfaceConfig).toHaveBeenCalledTimes(1)
    expect(screen.queryByRole('button', { name: 'Refresh' })).toBeNull()
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('refuses a live unknown slot with no submit', async () => {
    getSurfaceConfig.mockResolvedValue({ surfaces: { cad: { unknown: {} } }, source: { sha256: 'a'.repeat(64) } })
    render(<ChangeCapsule annotation={proposal()} />)
    fireEvent.click(screen.getByTestId('change-capsule-accept'))
    expect(await screen.findByRole('alert')).toHaveTextContent(CHANGE_CAPSULE_REASONS.invalidManifest)
    expect(submitSurfaceConfig).not.toHaveBeenCalled()
  })

  it('refuses an unavailable manifest rather than submitting blind', async () => {
    getSurfaceConfig.mockRejectedValue(new Error('offline'))
    render(<ChangeCapsule annotation={proposal()} />)
    fireEvent.click(screen.getByTestId('change-capsule-accept'))
    expect(await screen.findByRole('alert')).toHaveTextContent(CHANGE_CAPSULE_REASONS.manifestUnavailable)
    expect(submitSurfaceConfig).not.toHaveBeenCalled()
  })

  it('a record without kind retains the entity containment path', async () => {
    const onAccept = vi.fn()
    render(<ChangeCapsule annotation={annotation()} identity={entityIdentity} onAccept={onAccept}
      resolveScopedIntakes={() => ({ baseIntake: base, candidateIntake: scopedCandidate })} />)
    fireEvent.click(screen.getByTestId('change-capsule-accept'))
    await waitFor(() => expect(onAccept).toHaveBeenCalledTimes(1))
    expect(getSurfaceConfig).not.toHaveBeenCalled()
    expect(submitSurfaceConfig).not.toHaveBeenCalled()
  })
})

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
