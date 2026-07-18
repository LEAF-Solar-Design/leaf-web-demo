/**
 * Prompt/request routing (the run / one-off / build classifier).
 *
 *   run      - dispatch an EXISTING registered tool. Cheapest: NO author session,
 *              the Agent SDK is never constructed. (POST /run-registered)
 *   one-off  - author a tool + run it once, then discard. Uses the author
 *              mechanism but does NOT persist (no registerTool, no commit).
 *   build    - author a tool + register it (persist to registry.json + one
 *              harness commit). (POST /author, default)
 *
 * one-off and build share the author mechanism; only build persists. This is the
 * seam that keeps the design-time-only invariant honest: the run route is a
 * separate code path that never reaches the author loop.
 */

export type Route = "run" | "one-off" | "build";

export interface RouteInput {
  path: string;
  body: { mode?: string; description?: string; tool?: string } | null;
}

export function classifyRoute(input: RouteInput): Route {
  // The run route is addressed by its own endpoint - never inferred from a prompt.
  if (input.path === "/run-registered") return "run";

  const mode = input.body?.mode;
  if (mode === "run" || mode === "one-off" || mode === "build") return mode;

  // /author with no explicit mode defaults to build (author + register).
  return "build";
}
