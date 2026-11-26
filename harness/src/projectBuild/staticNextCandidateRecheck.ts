import type { BrowserCandidateLoader, BrowserCandidateWitness, PublishedBrowserCandidate } from "./browserCandidateBuild.js";
import { nextRequestDigest } from "./genericNextBuild.js";
import type { CandidateOperation, NextBuildScope } from "./genericNextBuild.js";

const PATH = "/internal/project-static-preview/candidate-recheck";
const SCHEMA = "leaf.browser-static-candidate-recheck.v1";
const MAX_RESPONSE = 16 * 1024;
const RESPONSE_KEYS = "execution_id,job_id,organization_id,project_id,receipt_digest,repo_key,schema,source_commit,source_request_id,source_tree,tenant_id,version";
const UUID = /^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/;
const SHA256 = /^[0-9a-f]{64}$/;

async function readSmallJson(response: Response): Promise<unknown> {
  if (!response.body) throw new Error("candidate_unavailable");
  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > MAX_RESPONSE) throw new Error("candidate_unavailable");
      chunks.push(value);
    }
  } catch (error) {
    await reader.cancel().catch(() => {});
    throw error;
  } finally {
    reader.releaseLock();
  }
  return JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(Buffer.concat(chunks, size)));
}

export function createStaticNextCandidateRecheck(options: { appUrl: string; dispatchSecret: string; fetch?: typeof fetch }) {
  const app = new URL(options.appUrl);
  const dispatchSecret = options.dispatchSecret.trim();
  if (!["http:", "https:"].includes(app.protocol) || app.username || app.password || !dispatchSecret) {
    throw new Error("static_next_dispatch_config_invalid");
  }
  const url = new URL(PATH, app);
  const send = options.fetch ?? fetch;

  async function checked(scope: NextBuildScope, operation: CandidateOperation,
    witness?: BrowserCandidateWitness): Promise<PublishedBrowserCandidate | null> {
    const payload = {
      tenant_id: scope.tenantId, organization_id: scope.organizationId, project_id: scope.projectId,
      repo_key: scope.repoKey, job_id: scope.jobId, source_commit: scope.sourceCommit, source_tree: scope.sourceTree,
    };
    try {
      const response = await send(url, {
        method: "POST", redirect: "error", signal: AbortSignal.timeout(3_000),
        headers: { "Content-Type": "application/json", "X-Tenant-Id": scope.tenantId, "X-Dispatch-Secret": dispatchSecret },
        body: JSON.stringify({
          ...payload, operation,
          ...(witness && { source_request_id: witness.sourceRequestId, version: witness.version, receipt_digest: witness.receiptDigest }),
        }),
      });
      if (response.status !== 200) {
        await response.body?.cancel();
        return null;
      }
      const raw = await readSmallJson(response);
      if (!raw || typeof raw !== "object" || Object.keys(raw).sort().join(",") !== RESPONSE_KEYS) return null;
      const row = raw as Record<string, unknown>;
      if (row.schema !== SCHEMA || row.execution_id !== nextRequestDigest(scope) ||
          Object.entries(payload).some(([key, value]) => row[key] !== value) ||
          typeof row.source_request_id !== "string" || !UUID.test(row.source_request_id) ||
          typeof row.version !== "number" || !Number.isSafeInteger(row.version) || row.version < 1 ||
          typeof row.receipt_digest !== "string" || !SHA256.test(row.receipt_digest) ||
          (witness && (row.source_request_id !== witness.sourceRequestId ||
            row.version !== witness.version || row.receipt_digest !== witness.receiptDigest))) return null;
      return {
        schema: "leaf.browser-project-source.v1", state: "published",
        authority: { tenantId: scope.tenantId, organizationId: scope.organizationId, projectId: scope.projectId, repoKey: scope.repoKey },
        sourceRequestId: row.source_request_id, version: row.version, receiptDigest: row.receipt_digest,
        sourceCommit: scope.sourceCommit, sourceTree: scope.sourceTree,
      };
    } catch {
      return null;
    }
  }

  const loadCandidate: BrowserCandidateLoader = (authority, sourceRequestId, request, operation) =>
    sourceRequestId === request.sourceRequestId
      ? checked({ ...authority, jobId: request.jobId, sourceCommit: request.sourceCommit, sourceTree: request.sourceTree }, operation, request)
      : Promise.resolve(null);

  return { loadCandidate, async isCurrent(scope: NextBuildScope, operation: CandidateOperation) {
    return (await checked(scope, operation)) !== null;
  } };
}
