// Calm degraded copy for the agent tier (two-tier dispatch, wire §11). The
// deterministic path is never blocked by any of these — the banner just says
// so honestly. Keyed off classifyAgentError, never message text.
//
// Extracted from App.jsx (~line 162, `agentBannerFor`) and site/ToolCast.jsx
// (~line 173, `agentBannerFor`). The two shells shipped genuinely different
// wording for the same kinds (App: em-dash punctuation, "routed
// deterministically"; ToolCast: period punctuation, "The catalog route is
// still available") — preserved verbatim per caller via the `copy` option
// rather than merged into one wording. Any kind not explicitly listed below
// (including 'unreachable' itself) normalizes to the 'unreachable' entry,
// matching both originals' fallthrough behavior exactly.
import { classifyAgentError } from '../converse.js'

export const CONSOLE_AGENT_BANNER_COPY = {
  quota: 'AI paused — your built tools keep working.',
  grant: 'Chat needs a linked Claude account.',
  entitlement: 'Chat isn’t included in your plan — your built tools keep working.',
  busy: 'The assistant is mid-turn — routed deterministically instead.',
  rate_limited: 'AI rate-limited — routed deterministically; retry shortly.',
  unreachable: 'AI assistant unavailable — routed deterministically.',
}

export const OPERATOR_AGENT_BANNER_COPY = {
  quota: 'AI paused. Your built tools keep working.',
  grant: 'Chat needs a linked Claude account.',
  entitlement: 'Chat is not included in your plan. Your built tools keep working.',
  busy: 'The assistant is mid-turn. The catalog route is still available.',
  rate_limited: 'AI is rate-limited. The catalog route is still available; retry shortly.',
  unreachable: 'AI assistant unavailable. The catalog route is still available.',
}

export function agentBannerFor(error, { copy = CONSOLE_AGENT_BANNER_COPY } = {}) {
  const kind = classifyAgentError(error)
  if (kind === 'quota') return { kind, message: copy.quota }
  if (kind === 'grant') return { kind, message: copy.grant }
  if (kind === 'entitlement') return { kind, message: copy.entitlement }
  if (kind === 'busy') return { kind, message: copy.busy }
  if (kind === 'rate_limited') return { kind, message: copy.rate_limited }
  return { kind: 'unreachable', message: copy.unreachable }
}
