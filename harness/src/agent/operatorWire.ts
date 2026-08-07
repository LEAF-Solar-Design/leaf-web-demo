// Operator wire vocabulary (contract/OPERATOR.md section 3). Separate
// constants from the tenant ConverseEventType/ConverseStopReason, which do
// not grow. Pinned by test/operatorLoop.freeze.test.ts.

export const OPERATOR_EVENT_TYPES = [
  "operator_turn_started",
  "operator_text_delta",
  "operator_tool_call",
  "operator_tool_result",
  "operator_proposed_action",
  "operator_authority_minted",
  "operator_authority_redeemed",
  "operator_turn_usage",
  "operator_turn_complete",
  "operator_session_state",
  "operator_error",
] as const;
export type OperatorEventType = (typeof OPERATOR_EVENT_TYPES)[number];

export const OPERATOR_STOP_REASONS = [
  "end_turn",
  "awaiting_approval",
  "cap_hit",
  "error",
  "timeout",
] as const;
export type OperatorStopReason = (typeof OPERATOR_STOP_REASONS)[number];

/** Server-attested operator context: assembled ONLY by the app from its
 * require_operator resolution, forwarded over the secret-gated hop. The
 * harness never mints one from client input. */
export interface OperatorTurnContext {
  subject: string;
  roleRevision: number;
  profile: string;
  environment: string;
}

export interface OperatorTurnInput {
  sessionId: string;
  turnId: string;
  text: string;
  operator: OperatorTurnContext;
}

export interface OperatorTurnResult {
  stopReason: OperatorStopReason;
  text: string;
  toolCalls: Array<{ tool: string; ok: boolean; summary: string }>;
}
