/** Shared Timeline and Build Metrics projection for Git plus request events. */

export interface BrowserGitCommit {
  sha: string;
  when: string;
  subject: string;
  author?: string;
}

export interface BrowserRequestEvent {
  seq: number;
  recordedAt: string;
  eventType: "state" | "message" | "activity";
  state?: string;
  identity: { requestId: string; conversationId: string };
  prompt?: string;
  error?: string;
  result?: Record<string, unknown>;
}

export interface HistoryFeedItem {
  id: string;
  at: string;
  type: "commit" | "request";
  title: string;
  significance: string;
  status: string;
}

export interface BuildMetrics {
  commits: number;
  requests: number;
  succeeded: number;
  failed: number;
  conflicted: number;
  active: number;
}

function short(text: unknown, fallback: string, max = 72): string {
  const value = String(text ?? "").replace(/\s+/g, " ").trim() || fallback;
  return value.length <= max ? value : `${value.slice(0, max - 1).trimEnd()}…`;
}

export function projectHistoryFeed(
  commits: BrowserGitCommit[],
  events: BrowserRequestEvent[],
): HistoryFeedItem[] {
  const commitItems = commits.map((commit) => ({
    id: `commit:${commit.sha}`,
    at: commit.when,
    type: "commit" as const,
    title: short(commit.subject, "Repository update"),
    significance: "Changed the live codebase.",
    status: "committed",
  }));
  const requestItems = events
    .filter((event) => event.eventType === "state" && event.state)
    .map((event) => ({
      id: `request:${event.seq}`,
      at: event.recordedAt,
      type: "request" as const,
      title: event.state === "accepted"
        ? short(event.prompt, "Request accepted")
        : `Request ${String(event.state).replaceAll("_", " ")}`,
      significance: event.state === "succeeded"
        ? "Result saved and ready."
        : event.state === "conflicted"
          ? "Work preserved for review."
          : event.state === "failed"
            ? short(event.error, "The request needs attention.")
            : "Execution state changed.",
      status: event.state!,
    }));
  return [...commitItems, ...requestItems]
    .sort((left, right) => right.at.localeCompare(left.at) || right.id.localeCompare(left.id));
}

export function calculateBuildMetrics(
  commits: BrowserGitCommit[],
  events: BrowserRequestEvent[],
): BuildMetrics {
  const latest = new Map<string, string>();
  for (const event of events) {
    if (event.eventType === "state" && event.state) {
      latest.set(event.identity.requestId, event.state);
    }
  }
  const states = [...latest.values()];
  return {
    commits: new Set(commits.map((commit) => commit.sha)).size,
    requests: latest.size,
    succeeded: states.filter((state) => state === "succeeded").length,
    failed: states.filter((state) => state === "failed" || state === "interrupted").length,
    conflicted: states.filter((state) => state === "conflicted").length,
    active: states.filter((state) =>
      state === "accepted" || state === "queued" || state === "running" || state === "cancelling").length,
  };
}
