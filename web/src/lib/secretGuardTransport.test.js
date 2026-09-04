/**
 * THE GUARD ON THE WIRE (standardization slice 8a, round 3).
 *
 * These are the rows that make the PR's claim checkable. Two review rounds
 * proved that per-composer tests can all pass while a path to model context
 * stays open, because a composer test can only ever prove the composer it
 * renders. Here the subject is the TRANSPORT: the five functions that carry
 * user-typed free text out of this browser toward a model. Every one of them
 * must refuse a named shape before it touches the network, allow ordinary
 * text, honour a per-call override for the fuzzy shape ONLY, and throw an
 * error whose every visible field is free of the credential.
 *
 * `fetch` is stubbed and asserted on directly, so "it refuses" means "no
 * request was made", not "a function returned a falsy thing".
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  SecretRefusedError,
  getCredentialMountAvailable,
  guardedText,
  isSecretRefused,
  setCredentialMountAvailable,
} from './secretGuardTransport.js'
import {
  MASK_BULLETS,
  SECRET_REASONS,
  SECRET_REASONS_NO_MOUNT,
} from './secretPatterns.js'
import { authorTool, nlPrompt, stageAuthorTool } from '../api.js'
import { postMessage } from '../converse.js'
import * as operatorClient from '../operatorClient.js'

// Structurally valid, entirely fake.
const FAKE_ANTHROPIC = `sk-ant-api03-${'A9_-'.repeat(12)}`
const FAKE_GENERIC = `api_key: ${'x'.repeat(24)}`
const BENIGN = 'count the panels within 24in of the roof edge'

let fetchMock

beforeEach(() => {
  setCredentialMountAvailable(false)
  fetchMock = vi.fn(async () => ({
    ok: true,
    status: 202,
    headers: { get: () => null },
    json: async () => ({ lane: 'run', tool: 't', turn_id: 'turn-1', status: 'started', tool_name: 't' }),
    text: async () => '{}',
  }))
  vi.stubGlobal('fetch', fetchMock)
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  setCredentialMountAvailable(false)
})

/** Every string a thrown refusal can put in front of a human or a log. */
const surfaces = (error) => [
  String(error?.message || ''),
  String(error?.stack || '').split('\n')[0],
  JSON.stringify(error?.refusal || {}),
]

describe('guardedText: the decision, and what it refuses to remember', () => {
  it('passes ordinary text straight through', () => {
    expect(guardedText(BENIGN)).toEqual({ ok: true, text: BENIGN })
  })

  it('refuses a named shape and hands back identity, copy and a mask only', () => {
    const result = guardedText(FAKE_ANTHROPIC)
    expect(result.ok).toBe(false)
    expect(result.refusal.id).toBe('anthropic')
    expect(result.refusal.overridable).toBe(false)
    expect(result.refusal.masked).toBe(`sk-a${'•'.repeat(MASK_BULLETS)}`)
    expect(JSON.stringify(result.refusal)).not.toContain(FAKE_ANTHROPIC.slice(8))
  })

  it('honours allowSecretOnce for the overridable shape', () => {
    expect(guardedText(FAKE_GENERIC).ok).toBe(false)
    expect(guardedText(FAKE_GENERIC, { allowSecretOnce: true }).ok).toBe(true)
  })

  // The policy boundary a caller must not be able to widen by passing a
  // boolean: a named shape has no override anywhere in the product.
  it('refuses a NAMED shape even with allowSecretOnce set', () => {
    const result = guardedText(FAKE_ANTHROPIC, { allowSecretOnce: true })
    expect(result.ok).toBe(false)
    expect(result.refusal.id).toBe('anthropic')
  })

  it('refuses a mixed paste even with allowSecretOnce set', () => {
    const result = guardedText(`${FAKE_GENERIC} and ${FAKE_ANTHROPIC}`, { allowSecretOnce: true })
    expect(result.ok).toBe(false)
    expect(result.refusal.id).toBe('anthropic')
  })

  // The whole round-3 fix in one row: an authorisation lives for one call.
  it('remembers nothing: a granted override does not carry to the next call', () => {
    expect(guardedText(FAKE_GENERIC, { allowSecretOnce: true }).ok).toBe(true)
    expect(guardedText(FAKE_GENERIC).ok).toBe(false)
    expect(guardedText(FAKE_ANTHROPIC).ok).toBe(false)
  })

  it('fails honest on the mount question, and defaults to naming no control', () => {
    expect(getCredentialMountAvailable()).toBe(false)
    expect(guardedText(FAKE_ANTHROPIC).refusal.reason).toBe(SECRET_REASONS_NO_MOUNT.anthropic)

    setCredentialMountAvailable(true)
    expect(guardedText(FAKE_ANTHROPIC).refusal.reason).toBe(SECRET_REASONS.anthropic)

    // A per-call answer beats the module answer, for a caller that knows.
    expect(guardedText(FAKE_ANTHROPIC, { credentialMountAvailable: false }).refusal.reason)
      .toBe(SECRET_REASONS_NO_MOUNT.anthropic)
  })

  it('only a literal true answers the mount question', () => {
    setCredentialMountAvailable('yes')
    expect(getCredentialMountAvailable()).toBe(false)
  })
})

describe('SecretRefusedError', () => {
  it('is recognisable without instanceof, and carries a frozen refusal', () => {
    const error = new SecretRefusedError(guardedText(FAKE_ANTHROPIC).refusal)
    expect(isSecretRefused(error)).toBe(true)
    expect(isSecretRefused(new Error('nope'))).toBe(false)
    expect(Object.isFrozen(error.refusal)).toBe(true)
    expect(error.name).toBe('SecretRefusedError')
  })

  it('reads as the frozen sentence, so an unhandled throw still cannot leak', () => {
    const error = new SecretRefusedError(guardedText(FAKE_ANTHROPIC).refusal)
    expect(error.message).toBe(SECRET_REASONS_NO_MOUNT.anthropic)
    for (const shown of surfaces(error)) expect(shown).not.toContain(FAKE_ANTHROPIC.slice(4))
  })

  it('fails closed on a malformed refusal rather than inventing an override', () => {
    const error = new SecretRefusedError(null)
    expect(error.refusal.overridable).toBe(false)
    expect(error.refusal.id).toBe('generic')
  })
})

// ---------------------------------------------------------------------------
// The five transports. One shared table: the same four rows for each, because
// the promise is the same for each. A sixth sender added without the seam
// fails composer.test.mjs's source pin, not this file.
// ---------------------------------------------------------------------------
const TRANSPORTS = [
  {
    name: 'api.nlPrompt (POST /api/nl-prompt)',
    send: (text, opts) => nlPrompt(false, text, [], opts),
  },
  {
    name: 'api.authorTool (POST /api/author)',
    send: (text, opts) => authorTool(false, text, opts),
  },
  {
    name: 'api.stageAuthorTool (POST /api/author/stage)',
    send: (text, opts) => stageAuthorTool(false, text, null, opts),
  },
  {
    name: 'converse.postMessage (POST /api/sessions/{id}/messages)',
    send: (text, opts) => postMessage('sess-1', { text, ...opts }),
  },
  {
    name: 'operatorClient.postMessage (POST /api/operator/sessions/{id}/messages)',
    send: (text, opts) => operatorClient.postMessage('sess-1', text, opts),
  },
]

for (const transport of TRANSPORTS) {
  describe(`${transport.name} is guarded`, () => {
    it('refuses a named shape BEFORE any request is made', async () => {
      await expect(transport.send(FAKE_ANTHROPIC)).rejects.toSatisfy(isSecretRefused)
      expect(fetchMock, 'nothing may reach the network').not.toHaveBeenCalled()
    })

    it('lets ordinary text through to the wire', async () => {
      await transport.send(BENIGN).catch(() => {})
      expect(fetchMock).toHaveBeenCalled()
      const body = String(fetchMock.mock.calls[0][1]?.body || '')
      expect(body).toContain('count the panels')
    })

    it('refuses the overridable shape until the call authorises it', async () => {
      await expect(transport.send(FAKE_GENERIC)).rejects.toSatisfy(isSecretRefused)
      expect(fetchMock).not.toHaveBeenCalled()

      await transport.send(FAKE_GENERIC, { allowSecretOnce: true }).catch(() => {})
      expect(fetchMock).toHaveBeenCalledTimes(1)
    })

    it('does not remember that authorisation for the next call', async () => {
      await transport.send(FAKE_GENERIC, { allowSecretOnce: true }).catch(() => {})
      fetchMock.mockClear()

      await expect(transport.send(FAKE_GENERIC)).rejects.toSatisfy(isSecretRefused)
      await expect(transport.send(FAKE_ANTHROPIC, { allowSecretOnce: true })).rejects.toSatisfy(isSecretRefused)
      expect(fetchMock).not.toHaveBeenCalled()
    })

    it('throws a typed error that never shows the value', async () => {
      const error = await transport.send(FAKE_ANTHROPIC).catch((e) => e)
      expect(isSecretRefused(error)).toBe(true)
      expect(error.refusal.id).toBe('anthropic')
      for (const shown of surfaces(error)) expect(shown).not.toContain(FAKE_ANTHROPIC.slice(4))
    })

    it('never puts the authorisation flag on the wire', async () => {
      await transport.send(FAKE_GENERIC, { allowSecretOnce: true }).catch(() => {})
      const body = String(fetchMock.mock.calls[0][1]?.body || '')
      expect(body).not.toContain('allowSecretOnce')
      expect(body).not.toContain('credentialMountAvailable')
    })
  })
}

describe('the demo refuses exactly as the live app does', () => {
  // A bar that accepts a pasted key in the demo and refuses it in production
  // teaches the wrong thing, and the demo is where most people paste first.
  // The guard therefore sits ABOVE each transport's mock branch.
  it('mock nlPrompt refuses', async () => {
    await expect(nlPrompt(true, FAKE_ANTHROPIC, [])).rejects.toSatisfy(isSecretRefused)
  })

  it('mock authorTool refuses', async () => {
    await expect(authorTool(true, FAKE_ANTHROPIC)).rejects.toSatisfy(isSecretRefused)
  })

  it('mock stageAuthorTool refuses', async () => {
    await expect(stageAuthorTool(true, FAKE_ANTHROPIC)).rejects.toSatisfy(isSecretRefused)
  })

  it('and names no control in mock mode, where none is mounted', async () => {
    const error = await nlPrompt(true, FAKE_ANTHROPIC, []).catch((e) => e)
    expect(error.refusal.reason).toBe(SECRET_REASONS_NO_MOUNT.anthropic)
    expect(error.refusal.reason).not.toContain('Claude accounts')
  })

  it('but points at the panel where the live app mounts it', async () => {
    const error = await nlPrompt(false, FAKE_ANTHROPIC, []).catch((e) => e)
    expect(error.refusal.reason).toBe(SECRET_REASONS.anthropic)
  })
})

describe('a poll/resume carries no description, so it is not re-judged', () => {
  // The one deliberate exemption, stated so nobody reads it as a hole: with
  // `pollUrl` set, stageAuthorTool reads a durable status and posts no
  // description at all. Guarding it would refuse the RESUME of a send the user
  // already authorised, for text that is not on the wire.
  it('resumes without refusing', async () => {
    fetchMock.mockImplementation(async () => ({
      ok: true,
      status: 200,
      headers: { get: () => null },
      json: async () => ({ status: 'succeeded', change_set_id: 'c1', tool: { name: 't' }, receipt: { change_set_id: 'c1', state: 'staged' } }),
    }))
    const staged = await stageAuthorTool(false, FAKE_ANTHROPIC, null, {
      pollUrl: '/api/author/stages/c1', changeSetId: 'c1',
    })
    expect(staged).toBeTruthy()
    expect(fetchMock).toHaveBeenCalled()
  })
})
