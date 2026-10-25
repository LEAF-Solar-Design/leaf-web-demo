// Calm degraded copy for the agent tier (two-tier dispatch, wire §11). The
// deterministic path is never blocked by any of these — the banner just says
// so honestly. Keyed off classifyAgentError, never message text.
//
// Shared by App.jsx and site/ToolCast.jsx. Each shell can supply copy;
// unmatched kinds normalize to the unavailable-assistant message.
import { classifyAgentError } from '../converse.js'
import { humanizeError } from '../errorHumanize.js'

export const CONSOLE_AGENT_BANNER_COPY = {
  quota: 'AI paused. Your built tools keep working.',
  grant: 'Chat needs a linked Claude account.',
  entitlement: 'Chat isn’t included in your plan. Your built tools keep working.',
  authentication: 'Your session has expired. Sign in again to continue.',
  workspace: 'Your account cannot access this workspace. Ask your workspace administrator for access.',
  busy: 'The assistant is working. You can still run your existing tools.',
  rate_limited: 'The assistant is unavailable. You can still run your existing tools. Try chat again shortly.',
  unreachable: 'The assistant is unavailable. You can still run your existing tools. Try chat again shortly.',
}

export const OPERATOR_AGENT_BANNER_COPY = {
  quota: 'AI paused. Your built tools keep working.',
  grant: 'Chat needs a linked Claude account.',
  entitlement: 'Chat is not included in your plan. Your built tools keep working.',
  authentication: CONSOLE_AGENT_BANNER_COPY.authentication,
  workspace: CONSOLE_AGENT_BANNER_COPY.workspace,
  busy: CONSOLE_AGENT_BANNER_COPY.busy,
  rate_limited: CONSOLE_AGENT_BANNER_COPY.rate_limited,
  unreachable: CONSOLE_AGENT_BANNER_COPY.unreachable,
}

export function agentBannerFor(error, { copy = CONSOLE_AGENT_BANNER_COPY } = {}) {
  const kind = classifyAgentError(error)
  if (kind === 'authentication' || kind === 'workspace') return { kind, message: humanizeError(copy[kind] || CONSOLE_AGENT_BANNER_COPY[kind]) }
  if (kind === 'quota') return { kind, message: humanizeError(copy.quota) }
  if (kind === 'grant') return { kind, message: humanizeError(copy.grant) }
  if (kind === 'entitlement') return { kind, message: humanizeError(copy.entitlement) }
  if (kind === 'busy') return { kind, message: humanizeError(copy.busy) }
  if (kind === 'rate_limited') return { kind, message: humanizeError(copy.rate_limited) }
  return { kind: 'unreachable', message: humanizeError(copy.unreachable) }
}
