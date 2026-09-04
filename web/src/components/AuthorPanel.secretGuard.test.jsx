/**
 * The Author-a-tool description's credential REFUSAL (slice 8a, round 3).
 *
 * WHY THIS FILE IS NEW. Round 3's review found this textarea reaching model
 * context with no guard anywhere on its path, on BOTH shells: submit() ->
 * onAuthor -> authorStage.stage -> POST /api/author/stage (the authoring agent
 * reads the description verbatim) and, via the authority mint, POST
 * /api/sessions/{id}/messages — the very endpoint the PR body's own table
 * claimed was guarded. It was the third composer found open in three rounds,
 * which is why the guard moved off the composers and onto the wire.
 *
 * So this panel, like the other two, evaluates nothing. `onAuthor` here runs
 * the REAL guard seam, standing in for api.stageAuthorTool, and these rows
 * pin what the panel does with the typed refusal it gets back.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

import AuthorPanel from './AuthorPanel.jsx'
import {
  SecretRefusedError,
  guardedText,
  setCredentialMountAvailable,
} from '../lib/secretGuardTransport.js'
import { MASK_BULLETS, MASK_PREFIX, SECRET_REASONS } from '../lib/secretPatterns.js'

// Structurally valid, entirely fake — no real credential appears in this repo.
const FAKE_ANTHROPIC = `sk-ant-api03-${'A9_-'.repeat(12)}`
const FAKE_GENERIC = `api_key: ${'x'.repeat(24)}`
const BENIGN = 'count panels within 24in of the roof edge'

const SETTLE = { timeout: 5000 }

beforeEach(() => setCredentialMountAvailable(true))
afterEach(() => {
  cleanup()
  vi.clearAllMocks()
  setCredentialMountAvailable(false)
})

/** api.stageAuthorTool's contract, reproduced with the real seam. */
function setup() {
  const onAuthor = vi.fn(async (description, targetToolName, { allowSecretOnce = false } = {}) => {
    const verdict = guardedText(description, { allowSecretOnce, credentialMountAvailable: true })
    if (!verdict.ok) throw new SecretRefusedError(verdict.refusal)
    return { tool: { name: 'authored' }, preview: 'def run(): pass' }
  })
  render(
    <AuthorPanel
      onAuthor={onAuthor}
      onPublish={vi.fn()}
      onUseAuthored={vi.fn()}
      onCancelRevision={vi.fn()}
      onLinkClaude={vi.fn()}
    />,
  )
  return { onAuthor }
}

const field = () => screen.getByLabelText('What should the tool do?')
const generate = () => screen.getByRole('button', { name: /generate tool/i })
const type = (text) => fireEvent.change(field(), { target: { value: text } })
const notice = () => screen.queryByTestId('author-secret-notice')
const reason = () => screen.getByTestId('author-secret-notice-reason')

describe('a named shape in the description is refused', () => {
  it('shows the notice instead of a red authoring failure', async () => {
    setup()
    type(FAKE_ANTHROPIC)
    fireEvent.click(generate())

    await waitFor(() => expect(notice()).toBeInTheDocument(), SETTLE)
    expect(notice()).toHaveAttribute('role', 'alert')
    expect(reason().textContent).toBe(SECRET_REASONS.anthropic)
    // Not an outage, not a plan gate: no other calm gate or error may appear.
    expect(screen.queryByText(/authoring failed/i)).toBeNull()
    expect(screen.queryByText(/link your claude account/i)).toBeNull()
  })

  it('shows a shape prefix behind fixed bullets, never the credential', async () => {
    setup()
    type(FAKE_ANTHROPIC)
    fireEvent.click(generate())

    await waitFor(() => expect(notice()).toBeInTheDocument(), SETTLE)
    expect(screen.getByTestId('author-secret-notice-mask').textContent)
      .toBe(`${FAKE_ANTHROPIC.slice(0, MASK_PREFIX)}${'•'.repeat(MASK_BULLETS)}`)
    const shown = notice().textContent
    const entropy = FAKE_ANTHROPIC.slice(MASK_PREFIX)
    for (let i = 0; i + 8 <= entropy.length; i += 1) {
      expect(shown).not.toContain(entropy.slice(i, i + 8))
    }
  })

  it('offers no override for a named shape', async () => {
    setup()
    type(FAKE_ANTHROPIC)
    fireEvent.click(generate())

    await waitFor(() => expect(notice()).toBeInTheDocument(), SETTLE)
    expect(screen.queryByTestId('author-secret-send-anyway')).toBeNull()
  })

  it('leaves no authored card behind', async () => {
    setup()
    type(FAKE_ANTHROPIC)
    fireEvent.click(generate())

    await waitFor(() => expect(notice()).toBeInTheDocument(), SETTLE)
    expect(screen.queryByRole('button', { name: /publish/i })).toBeNull()
  })
})

describe('the overridable shape takes a per-call authorisation', () => {
  it('refuses first, then re-issues the SAME staging call with the flag', async () => {
    const { onAuthor } = setup()
    type(FAKE_GENERIC)
    fireEvent.click(generate())

    await waitFor(() => expect(notice()).toBeInTheDocument(), SETTLE)
    expect(reason().textContent).toBe(SECRET_REASONS.generic)
    expect(onAuthor.mock.calls[0][2]).toMatchObject({ allowSecretOnce: false })

    fireEvent.click(screen.getByTestId('author-secret-send-anyway'))
    await waitFor(() => expect(onAuthor).toHaveBeenCalledTimes(2), SETTLE)
    expect(onAuthor.mock.calls[1][0]).toBe(FAKE_GENERIC)
    expect(onAuthor.mock.calls[1][2]).toMatchObject({ allowSecretOnce: true })
    await waitFor(() => expect(notice()).toBeNull(), SETTLE)
  })

  // The round-3 fix, stated for this panel: nothing remembers the grant.
  it('does not carry the authorisation to the next Generate', async () => {
    const { onAuthor } = setup()
    type(FAKE_GENERIC)
    fireEvent.click(generate())
    await waitFor(() => expect(notice()).toBeInTheDocument(), SETTLE)
    fireEvent.click(screen.getByTestId('author-secret-send-anyway'))
    await waitFor(() => expect(onAuthor).toHaveBeenCalledTimes(2), SETTLE)

    type(FAKE_ANTHROPIC)
    fireEvent.click(generate())
    await waitFor(() => expect(onAuthor).toHaveBeenCalledTimes(3), SETTLE)
    expect(onAuthor.mock.calls[2][2]).toMatchObject({ allowSecretOnce: false })
    await waitFor(() => expect(reason().textContent).toBe(SECRET_REASONS.anthropic), SETTLE)
  })
})

describe('ordinary descriptions are untouched', () => {
  it('stages normally, with no notice', async () => {
    const { onAuthor } = setup()
    type(BENIGN)
    fireEvent.click(generate())

    await waitFor(() => expect(onAuthor).toHaveBeenCalledTimes(1), SETTLE)
    expect(notice()).toBeNull()
  })

  it('retires the notice as soon as the description changes', async () => {
    setup()
    type(FAKE_ANTHROPIC)
    fireEvent.click(generate())
    await waitFor(() => expect(notice()).toBeInTheDocument(), SETTLE)

    type(BENIGN)
    await waitFor(() => expect(notice()).toBeNull(), SETTLE)
  })
})
