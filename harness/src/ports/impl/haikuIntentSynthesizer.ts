/**
 * Per-turn INTENT SYNTHESIS with a small fast model.
 *
 * The spine can act on two entirely different things — the DRAWING (the CAD
 * file) and the PRODUCT (this web app) — and a request like "change the
 * background" names neither on its face. Resolving that by pattern-matching
 * words in the system prompt ("the app", "the page", "light mode", …) is a
 * keyword list: it is brittle, it never generalizes past the phrasings someone
 * thought of, and it grows without bound. So the classification is done by a
 * MODEL reading the sentence, not by string matching.
 *
 * Contract and discipline:
 * - ADVISORY ONLY. The result is injected as a signal the spine model may
 *   override. It never selects a tool and never gates one — the agent gate,
 *   entitlements, and approvals are untouched by anything here.
 * - FAIL OPEN, ALWAYS. Any error, timeout, unparseable answer, or missing SDK
 *   yields `null`, and the turn proceeds exactly as it did before this existed.
 *   A classifier outage must never cost the user a turn.
 * - BOUNDED. One short call, hard wall-clock timeout, tiny max token budget.
 * - CREDENTIAL DISCIPLINE mirrors ConverseSdkRunner: the grant is injected into
 *   a scrubbed child env, never logged, never returned, and never embedded in
 *   the value this module hands back.
 */

import type { AgentGrant, IntentSynthesizer, TurnIntent, TurnIntentTarget } from "../index.js";
import { buildScrubbedEnv } from "./agentSdkRunner.js";

interface SdkModule {
  query(args: {
    prompt: string | AsyncIterable<unknown>;
    options: Record<string, unknown>;
  }): AsyncIterable<unknown>;
}

function dynImport(parts: string[]): Promise<unknown> {
  return import(parts.join("/"));
}

const TARGETS: readonly TurnIntentTarget[] = ["product", "drawing", "unclear"];

/**
 * Deliberately describes the DISTINCTION and lets the model reason about the
 * sentence. It carries no vocabulary list to match against, because the whole
 * point is to generalize past any list.
 */
const RUBRIC = `You classify one message sent to a CAD copilot.

The copilot can change two unrelated things:
- "drawing": the CAD file the user is working on — its geometry, layers, panels,
  strings, versions. Anything that lives inside the drawing.
- "product": the web application the user is looking at while they work — its
  pages, panels, controls, colors, theme, copy, layout, behavior. The software
  itself, not the file it opens.

Decide which one the message is asking about. Many words ("background",
"colour", "layout", "panel", "size") belong to either world depending on what
the person meant, so judge the sentence, not the words in it. If the message is
about neither, or you genuinely cannot tell which of the two is meant, answer
"unclear" — a wrong confident answer is worse than an honest "unclear".

Reply with ONLY a JSON object, no prose and no code fence:
{"target":"product"|"drawing"|"unclear","rationale":"<at most 12 words>"}`;

export interface HaikuIntentSynthesizerOptions {
  /** The tenant's Agent SDK credential. Injected into a scrubbed child env. */
  grant: AgentGrant;
  /** Model id. Default: LEAF_INTENT_MODEL env, else Haiku. */
  model?: string;
  /** Hard wall-clock budget. Default: LEAF_INTENT_TIMEOUT_MS env, else 6000. */
  timeoutMs?: number;
  /** Test seam. */
  sdkImport?: () => Promise<unknown>;
}

export class HaikuIntentSynthesizer implements IntentSynthesizer {
  private readonly grant: AgentGrant;
  private readonly model: string;
  private readonly timeoutMs: number;
  private readonly sdkImport: () => Promise<unknown>;

  constructor(opts: HaikuIntentSynthesizerOptions) {
    this.grant = opts.grant;
    this.model =
      opts.model ?? process.env.LEAF_INTENT_MODEL ?? "claude-haiku-4-5-20251001";
    this.timeoutMs =
      opts.timeoutMs ?? (Number(process.env.LEAF_INTENT_TIMEOUT_MS || "") || 6000);
    this.sdkImport =
      opts.sdkImport ?? (() => dynImport(["@anthropic-ai", "claude-agent-sdk"]));
  }

  async synthesize(text: string): Promise<TurnIntent | null> {
    const message = (text ?? "").trim();
    if (!message) return null;

    try {
      return await this.race(this.classify(message));
    } catch {
      // Fail open: an unclassified turn behaves exactly as it did before.
      return null;
    }
  }

  /** Bound the call so a hung classifier can never hold the turn. */
  private async race(work: Promise<TurnIntent | null>): Promise<TurnIntent | null> {
    let timer: ReturnType<typeof setTimeout> | undefined;
    const budget = new Promise<null>((resolve) => {
      timer = setTimeout(() => resolve(null), this.timeoutMs);
    });
    try {
      return await Promise.race([work, budget]);
    } finally {
      if (timer) clearTimeout(timer);
    }
  }

  private async classify(message: string): Promise<TurnIntent | null> {
    const sdk = (await this.sdkImport()) as SdkModule;
    const childEnv = buildScrubbedEnv(this.grant, process.env);

    let text = "";
    for await (const event of sdk.query({
      // The message is DATA to be classified. It is fenced and labelled so a
      // sentence containing instructions cannot redirect the classifier; the
      // worst case is a wrong label, which is advisory and overridable.
      prompt: `${RUBRIC}\n\n<message>\n${message}\n</message>`,
      options: {
        model: this.model,
        env: childEnv,
        maxTurns: 1,
        // No tools: this call reads one sentence and returns one label.
        allowedTools: [],
      },
    })) {
      text += extractText(event);
    }
    return parseIntent(text);
  }
}

/** Pull assistant text out of the SDK's event stream, shape-tolerantly. */
function extractText(event: unknown): string {
  if (typeof event !== "object" || event === null) return "";
  const e = event as Record<string, unknown>;
  const msg = e.message as Record<string, unknown> | undefined;
  const content = (msg?.content ?? e.content) as unknown;
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return "";
  return content
    .map((block) => {
      if (typeof block !== "object" || block === null) return "";
      const b = block as Record<string, unknown>;
      return b.type === "text" && typeof b.text === "string" ? b.text : "";
    })
    .join("");
}

/**
 * Accept only a well-formed verdict. Anything else is `null` (no signal),
 * never a guess — an invented label would be worse than none.
 */
export function parseIntent(raw: string): TurnIntent | null {
  const start = raw.indexOf("{");
  const end = raw.lastIndexOf("}");
  if (start < 0 || end <= start) return null;
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw.slice(start, end + 1));
  } catch {
    return null;
  }
  if (typeof parsed !== "object" || parsed === null) return null;
  const p = parsed as Record<string, unknown>;
  const target = p.target;
  if (typeof target !== "string") return null;
  if (!TARGETS.includes(target as TurnIntentTarget)) return null;
  const rationale = typeof p.rationale === "string" ? p.rationale.slice(0, 120) : "";
  return { target: target as TurnIntentTarget, rationale };
}
