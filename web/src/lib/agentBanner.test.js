/**
 * W2a mechanical dedupe: pins the exact banner copy each shell shipped
 * before the extraction (App.jsx's `agentBannerFor` vs site/ToolCast.jsx's),
 * since the two disagreed on wording for the same classifyAgentError kinds.
 */
import { describe, expect, it } from 'vitest'

import {
  CONSOLE_AGENT_BANNER_COPY, OPERATOR_AGENT_BANNER_COPY, agentBannerFor,
} from './agentBanner.js'

describe('agentBannerFor', () => {
  it('defaults to the console (App.jsx) copy', () => {
    expect(agentBannerFor({ errorCode: 'llm_quota_exhausted' })).toEqual({
      kind: 'quota',
      message: 'AI paused — your built tools keep working.',
    })
    expect(agentBannerFor({ status: 401 })).toEqual({
      kind: 'grant',
      message: 'Chat needs a linked Claude account.',
    })
    expect(agentBannerFor({ status: 403 })).toEqual({
      kind: 'entitlement',
      message: 'Chat isn’t included in your plan — your built tools keep working.',
    })
    expect(agentBannerFor({ status: 409, errorCode: 'turn_in_progress' })).toEqual({
      kind: 'busy',
      message: 'The assistant is mid-turn — routed deterministically instead.',
    })
  })

  it('reproduces the ToolCast (operator) copy verbatim when asked', () => {
    expect(agentBannerFor({ errorCode: 'llm_quota_exhausted' }, { copy: OPERATOR_AGENT_BANNER_COPY })).toEqual({
      kind: 'quota',
      message: 'AI paused. Your built tools keep working.',
    })
    expect(agentBannerFor({ status: 403 }, { copy: OPERATOR_AGENT_BANNER_COPY })).toEqual({
      kind: 'entitlement',
      message: 'Chat is not included in your plan. Your built tools keep working.',
    })
    expect(agentBannerFor({ status: 409, errorCode: 'turn_in_progress' }, { copy: OPERATOR_AGENT_BANNER_COPY }))
      .toEqual({ kind: 'busy', message: 'The assistant is mid-turn. The catalog route is still available.' })
  })

  it('normalizes ANY unmatched kind — including classifyAgentError\'s own "unreachable" — to the unreachable entry', () => {
    // Both originals fall through a chain of explicit ifs to one default
    // branch; a kind like 'too_large' or 'not_found' must land there too,
    // never leak through with its own (unhandled) kind label.
    expect(agentBannerFor({ status: 413, errorCode: 'BAD_PARAMS' })).toEqual({
      kind: 'unreachable',
      message: CONSOLE_AGENT_BANNER_COPY.unreachable,
    })
    expect(agentBannerFor({ status: 502 })).toEqual({
      kind: 'unreachable',
      message: CONSOLE_AGENT_BANNER_COPY.unreachable,
    })
  })
})
