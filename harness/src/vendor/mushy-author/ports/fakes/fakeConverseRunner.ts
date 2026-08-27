/**
 * Fake SpineConverseRunner - a SCRIPTED stand-in for the live conversational SDK
 * session. NO network, NO Anthropic auth. It behaves like an obedient spine
 * model: it honors canUseTool, drives tools ONLY through the injected
 * ToolExecutor, ends its turn after a proposed/pending result, and re-invokes
 * with confirmation_id on a "CONFIRMATION <id> APPROVED" turn (mirroring the
 * system-prompt policy the real model follows).
 *
 * Deterministic directives (embedded in the user text by tests):
 *   RUN:<tool> [DWG:<drawing>] [PARAMS:<json>]
 *                                  -> run_capability {tool, params, dwg?}
 *   SEARCH:<query>              -> catalog_search {query}
 *   STATE:<what>                -> drawing_state {what}
 *   JOB:<job_id>                -> job_status {job_id}
 *   AUTHOR:<description>        -> author_tool {description, mode:"build"}
 *   PUBLISH:<change_set_id>     -> request_publication {change_set_id}
 *   CONFIRM_REQ:<kind>          -> request_confirmation {kind, payload:{}}
 *   CUSTOMIZE:<title> [PARAMS:{edits:[...]}]      -> customize_platform {op:"propose", ...}
 *   CUSTOMIZE_LAND:<change_id> [PARAMS:{commit_sha}] -> customize_platform {op:"land", ...}
 *   CUSTOMIZE_LIST:<limit>      -> customize_platform {op:"list", limit}
 *   CUSTOMIZE_READ:<path>       -> customize_platform {op:"read_source", path}
 *   CUSTOMIZE_TREE:<dir|.>      -> customize_platform {op:"list_source", dir} ("." = root)
 *   FORBIDDEN_TOOL              -> attempts a non-spine tool via canUseTool
 *   FAIL:<stop_reason>          -> terminal failure (quota/rate-limit drills)
 *   SLOW                        -> hold the turn open on `slowGate` (turn-lock tests)
 *   (anything else)             -> plain streamed chat
 */

import type {
  ConverseRunInput,
  SpineConverseRunner,
  ConverseRunnerEvent,
  ConverseStopReason,
  ConverseTurnUsage,
} from "../index.js";

const FAKE_USAGE: ConverseTurnUsage = {
  turns: 1,
  input_tokens: 120,
  output_tokens: 40,
  cache_creation_tokens: 10,
  cache_read_tokens: 300,
  cost_tokens: 170, // input + output + cache_creation (cache_read excluded)
};

/** The user-authored (or loop-injected) tail of the built turn prompt. */
function tailOf(message: string): string {
  const confirmIdx = message.indexOf("=== CONFIRMATION ===");
  if (confirmIdx >= 0) return message.slice(confirmIdx);
  const userIdx = message.indexOf("=== USER MESSAGE ===");
  return userIdx >= 0 ? message.slice(userIdx) : message;
}

export class FakeConverseRunner implements SpineConverseRunner {
  /** Every run() input, for assertions (resume ids, prompts, models). */
  readonly runs: ConverseRunInput[] = [];
  /** Test hook: a turn whose text contains SLOW awaits this before finishing. */
  slowGate: Promise<void> | null = null;
  private sessions = 0;

  async *run(input: ConverseRunInput): AsyncIterable<ConverseRunnerEvent> {
    this.runs.push(input);
    const tail = tailOf(input.userMessage);
    let stopReason: ConverseStopReason = "end_turn";
    let error: { error_code: string; message: string; retryable: boolean } | undefined;

    const say = (text: string): ConverseRunnerEvent => ({ type: "text_delta", text });

    if (tail.includes("SLOW") && this.slowGate) {
      yield say("Thinking...");
      await this.slowGate;
    }

    const fail = /FAIL:([a-z_]+)/.exec(tail);
    const confirm = /CONFIRMATION (\S+) (APPROVED|DENIED)/.exec(tail);

    if (fail) {
      stopReason = fail[1] as ConverseStopReason;
      error = {
        error_code: stopReason,
        message: `fake terminal failure: ${stopReason}`,
        retryable: stopReason !== "error",
      };
    } else if (confirm) {
      const [, confirmationId, verdict] = confirm;
      if (verdict === "DENIED") {
        yield say("Understood — I won't dispatch that.");
      } else {
        // Re-invoke the confirmed action args-exact, plus confirmation_id.
        const action = /action: (\S+)/.exec(tail)?.[1] ?? "run_capability";
        const argsJson = /args: (\{.*\})/.exec(tail)?.[1] ?? "{}";
        const args = JSON.parse(argsJson) as Record<string, unknown>;
        yield* this.invoke(input, action, { ...args, confirmation_id: confirmationId });
      }
    } else {
      const directive =
        /(RUN|SEARCH|STATE|JOB|AUTHOR|AUTHOR_ONCE|PUBLISH|CONFIRM_REQ|CUSTOMIZE_LAND|CUSTOMIZE_LIST|CUSTOMIZE_READ|CUSTOMIZE_TREE|CUSTOMIZE):(\S+)(?:\s+DWG:(\S+))?(?:\s+PARAMS:(\{.*\}))?/.exec(tail);
      if (tail.includes("FORBIDDEN_TOOL")) {
        const verdict = await input.canUseTool("mcp__other__shell", {});
        yield say(
          verdict.behavior === "deny"
            ? `I can't do that: ${verdict.message}`
            : "unexpectedly allowed",
        );
      } else if (directive) {
        const [, kind, value, dwg, paramsJson] = directive;
        const params = paramsJson ? (JSON.parse(paramsJson) as Record<string, unknown>) : {};
        const argsByKind: Record<string, [string, Record<string, unknown>]> = {
          RUN: [
            "run_capability",
            { tool: value, params, ...(dwg ? { dwg } : {}) },
          ],
          SEARCH: ["catalog_search", { query: value }],
          STATE: ["drawing_state", { what: value }],
          JOB: ["job_status", { job_id: value }],
          AUTHOR: ["author_tool", { description: value, mode: "build" }],
          AUTHOR_ONCE: ["author_tool", { description: value, mode: "one_off" }],
          PUBLISH: ["request_publication", { change_set_id: value }],
          CONFIRM_REQ: ["request_confirmation", { kind: value, payload: {} }],
          CUSTOMIZE: [
            "customize_platform",
            { op: "propose", title: value, ...params },
          ],
          CUSTOMIZE_LAND: [
            "customize_platform",
            { op: "land", change_id: value, ...params },
          ],
          CUSTOMIZE_LIST: [
            "customize_platform",
            { op: "list", limit: Number(value) || undefined },
          ],
          CUSTOMIZE_READ: ["customize_platform", { op: "read_source", path: value }],
          CUSTOMIZE_TREE: [
            "customize_platform",
            { op: "list_source", ...(value === "." ? {} : { dir: value }) },
          ],
        };
        const [toolName, args] = argsByKind[kind!]!;
        yield* this.invoke(input, toolName, args);
      } else {
        yield say("Hello! I'm Leaf. Ask me about your drawing or a tool to run.");
      }
    }

    yield { type: "usage", usage: { ...FAKE_USAGE } };
    yield {
      type: "done",
      stopReason,
      sdkSessionId: input.resumeSdkSessionId ?? `fake-sdk-session-${++this.sessions}`,
      ...(error ? { error } : {}),
    };
  }

  /** canUseTool gate, then ToolExecutor.execute, then relay — like the real model. */
  private async *invoke(
    input: ConverseRunInput,
    toolName: string,
    args: Record<string, unknown>,
  ): AsyncIterable<ConverseRunnerEvent> {
    const say = (text: string): ConverseRunnerEvent => ({ type: "text_delta", text });
    const verdict = await input.canUseTool(`mcp__spine__${toolName}`, args);
    if (verdict.behavior === "deny") {
      yield say(`I can't use ${toolName}: ${verdict.message}`);
      return;
    }
    const result = await input.tools.execute(toolName, verdict.updatedInput);
    let parsed: Record<string, unknown> = {};
    try {
      parsed = JSON.parse(result.content) as Record<string, unknown>;
    } catch {
      // non-JSON tool content: relay generically below
    }
    if (result.isError) {
      // Deny/error results are RELAYED calmly (system-prompt policy), never retried.
      yield say(`The platform declined that: ${String(parsed.reason ?? result.content)}`);
    } else if (parsed.proposed) {
      yield say(
        `I've proposed running ${String(parsed.tool)} — approve the confirmation to dispatch it.`,
      );
    } else if (parsed.pending) {
      yield say("I need your confirmation before going further.");
    } else if (parsed.job_id) {
      yield say(`Dispatched: job ${String(parsed.job_id)} is ${String(parsed.status)}.`);
    } else {
      yield say(`Here's what I found: ${result.content}`);
    }
  }
}
