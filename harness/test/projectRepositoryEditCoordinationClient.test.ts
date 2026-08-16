import { describe, expect, it } from "vitest";

import {
  PROJECT_REPOSITORY_EDIT_COORDINATION_CONTRACT,
  type ProjectRepositoryEditRecordStagedRequest,
} from "../src/ports/index.js";
import { ProjectRepositoryEditCoordinationClient } from "../src/ports/impl/projectRepositoryEditCoordinationClient.js";

const IDS = {
  tenant: "11111111-1111-4111-8111-111111111111",
  project: "22222222-2222-4222-8222-222222222222",
  repo: "33333333-3333-4333-8333-333333333333",
  edit: "44444444-4444-4444-8444-444444444444",
  actor: "55555555-5555-4555-8555-555555555555",
  lease: "66666666-6666-4666-8666-666666666666",
  publishLease: "77777777-7777-4777-8777-777777777777",
  confirmation: "88888888-8888-4888-8888-888888888888",
};
const SECRET = "dispatch-secret-under-test";
const BASE = "1".repeat(40);
const STAGED = "2".repeat(40);
const TREE = "3".repeat(40);
const DIFF_DIGEST = "4".repeat(64);
const INSTRUCTION_DIGEST = "5".repeat(64);
const RECEIPT_DIGEST = "6".repeat(64);

function receipt() {
  return {
    contract: "leaf.project-repository-edit.v2" as const,
    edit_id: IDS.edit,
    state: "staged" as const,
    operation: "edit" as const,
    source_edit_id: null,
    actor_binding_id: IDS.actor,
    tenant_id: IDS.tenant,
    organization_id: IDS.tenant,
    project_id: IDS.project,
    repo_key: IDS.repo,
    writer_lease_id: IDS.lease,
    writer_lease_generation: 4,
    base_commit: BASE,
    staged_head_commit: STAGED,
    staged_tree: TREE,
    changed_paths: ["src/a.py"],
    diff_digest: DIFF_DIGEST,
    instruction_digest: INSTRUCTION_DIGEST,
    idempotency_key: "stage-key",
  };
}

function recordStagedRequest(): ProjectRepositoryEditRecordStagedRequest {
  return {
    contract: PROJECT_REPOSITORY_EDIT_COORDINATION_CONTRACT,
    action: "record_staged",
    receipt: receipt(),
    receipt_digest: RECEIPT_DIGEST,
    expected_version: 0,
    transition_key: "stage-key",
  };
}

interface Captured {
  url: string;
  init: { method: string; headers: Record<string, string>; body: string };
}

function harness(response: { ok: boolean; status: number; body?: unknown; throws?: boolean }) {
  const calls: Captured[] = [];
  const fetchImpl = async (url: string, init: Captured["init"]) => {
    calls.push({ url, init });
    if (response.throws) throw new Error("socket reset");
    return {
      ok: response.ok,
      status: response.status,
      json: async () => {
        if (response.body instanceof Error) throw response.body;
        return response.body;
      },
    };
  };
  const client = new ProjectRepositoryEditCoordinationClient({
    baseUrl: "https://coordination.test/",
    dispatchSecret: SECRET,
    fetchImpl,
  });
  return { client, calls };
}

describe("ProjectRepositoryEditCoordinationClient", () => {
  it("sends the exact closed record_staged wire shape", async () => {
    const { client, calls } = harness({
      ok: true,
      status: 200,
      body: {
        contract: PROJECT_REPOSITORY_EDIT_COORDINATION_CONTRACT,
        action: "record_staged",
        edit_id: IDS.edit,
        state: "staged",
        version: 1,
      },
    });
    const request = recordStagedRequest();
    const response = await client.recordStaged(request);

    expect(calls).toHaveLength(1);
    expect(calls[0].url).toBe("https://coordination.test/internal/project-repository-edit/record-staged");
    expect(calls[0].init.method).toBe("POST");
    expect(calls[0].init.headers["x-tenant-id"]).toBe(IDS.tenant);
    expect(calls[0].init.headers["x-dispatch-secret"]).toBe(SECRET);
    expect(JSON.parse(calls[0].init.body)).toEqual(request);
    expect(response).toEqual({
      contract: PROJECT_REPOSITORY_EDIT_COORDINATION_CONTRACT,
      action: "record_staged",
      edit_id: IDS.edit,
      state: "staged",
      version: 1,
    });
    expect(Object.isFrozen(response)).toBe(true);
  });

  it("sends the exact closed authorize_publish wire shape and parses the frozen matrix", async () => {
    const matrix = {
      contract: PROJECT_REPOSITORY_EDIT_COORDINATION_CONTRACT,
      action: "authorize_publish",
      edit_id: IDS.edit,
      state: "publishing",
      version: 3,
      receipt_digest: RECEIPT_DIGEST,
      expected_main_commit: BASE,
      staged_head_commit: STAGED,
      staged_tree: TREE,
      private_ref: `refs/leaf/changes/${IDS.edit}`,
      publish_lease_id: IDS.publishLease,
      publish_lease_generation: 5,
    };
    const { client, calls } = harness({ ok: true, status: 200, body: matrix });
    const request = {
      contract: PROJECT_REPOSITORY_EDIT_COORDINATION_CONTRACT,
      action: "authorize_publish" as const,
      tenant_id: IDS.tenant,
      organization_id: IDS.tenant,
      project_id: IDS.project,
      repo_key: IDS.repo,
      edit_id: IDS.edit,
      actor_binding_id: IDS.actor,
      confirmation_id: IDS.confirmation,
      receipt_digest: RECEIPT_DIGEST,
      publish_lease_id: IDS.publishLease,
      publish_lease_generation: 5,
      expected_version: 2,
      transition_key: "publish-key",
    } as const;
    const response = await client.authorizePublish(request);

    expect(JSON.parse(calls[0].init.body)).toEqual(request);
    expect(calls[0].url).toBe(
      "https://coordination.test/internal/project-repository-edit/authorize-publish");
    expect(response).toEqual(matrix);
    expect(Object.isFrozen(response)).toBe(true);
  });

  it("sends the exact closed settle and recover wire shapes", async () => {
    const settlement = {
      contract: PROJECT_REPOSITORY_EDIT_COORDINATION_CONTRACT,
      action: "settle_publish",
      edit_id: IDS.edit,
      state: "published",
      version: 4,
    };
    const { client, calls } = harness({ ok: true, status: 200, body: settlement });
    const settle = {
      contract: PROJECT_REPOSITORY_EDIT_COORDINATION_CONTRACT,
      action: "settle_publish" as const,
      tenant_id: IDS.tenant,
      organization_id: IDS.tenant,
      project_id: IDS.project,
      repo_key: IDS.repo,
      edit_id: IDS.edit,
      actor_binding_id: IDS.actor,
      publish_lease_id: IDS.publishLease,
      publish_lease_generation: 5,
      private_ref_commit: STAGED,
      main_commit: STAGED,
      main_tree: TREE,
      expected_version: 3,
      transition_key: "settle-key",
    } as const;
    expect(await client.settlePublish(settle)).toEqual(settlement);
    expect(JSON.parse(calls[0].init.body)).toEqual(settle);

    const recovery = {
      contract: PROJECT_REPOSITORY_EDIT_COORDINATION_CONTRACT,
      action: "recover_publish" as const,
      tenant_id: IDS.tenant,
      organization_id: IDS.tenant,
      project_id: IDS.project,
      repo_key: IDS.repo,
      edit_id: IDS.edit,
      actor_binding_id: IDS.actor,
      recovery_lease_id: IDS.publishLease,
      recovery_lease_generation: 6,
      private_ref_commit: STAGED,
      main_commit: STAGED,
      main_tree: TREE,
      expected_version: 3,
      transition_key: "recover-key",
      reason_code: "settlement_transport_failure",
    } as const;
    const { client: client2, calls: calls2 } = harness({
      ok: true,
      status: 200,
      body: { ...settlement, action: "recover_publish" },
    });
    expect(await client2.recoverPublish(recovery)).toEqual({
      ...settlement, action: "recover_publish",
    });
    expect(JSON.parse(calls2[0].init.body)).toEqual(recovery);
  });

  it("rejects a request with an extra field before any server call is credited", async () => {
    const { client, calls } = harness({ ok: true, status: 200, body: {} });
    const request = { ...recordStagedRequest(), repository_path: "/tmp/repo" };
    await expect(client.recordStaged(
      request as unknown as Parameters<typeof client.recordStaged>[0],
    )).rejects.toThrow("missing or extra fields");
    expect(calls).toHaveLength(0);
  });

  it("rejects a receipt carrying file bytes before any server call", async () => {
    const { client, calls } = harness({ ok: true, status: 200, body: {} });
    const request = {
      ...recordStagedRequest(),
      receipt: { ...receipt(), file_bytes: "UEsDBAo=" },
    };
    await expect(client.recordStaged(
      request as unknown as Parameters<typeof client.recordStaged>[0],
    )).rejects.toThrow("missing or extra fields");
    expect(calls).toHaveLength(0);
  });

  it("rejects malformed wire values before any server call", async () => {
    const { client, calls } = harness({ ok: true, status: 200, body: {} });
    const upperSha = { ...recordStagedRequest(), receipt_digest: "ABCD" + "5".repeat(60) };
    await expect(client.recordStaged(upperSha)).rejects.toThrow("lowercase SHA-256 digest");
    const badGeneration = {
      ...recordStagedRequest(),
      receipt: { ...receipt(), writer_lease_generation: 0 },
    };
    await expect(client.recordStaged(badGeneration)).rejects.toThrow("positive integer");
    const badVersion = { ...recordStagedRequest(), expected_version: 1 };
    await expect(client.recordStaged(badVersion)).rejects.toThrow("expected_version");
    expect(calls).toHaveLength(0);
  });

  it("fails closed on extra or malformed response fields", async () => {
    const base = {
      contract: PROJECT_REPOSITORY_EDIT_COORDINATION_CONTRACT,
      action: "record_staged",
      edit_id: IDS.edit,
      state: "staged",
      version: 1,
    };
    const extra = harness({ ok: true, status: 200, body: { ...base, debug: "trace" } });
    await expect(extra.client.recordStaged(recordStagedRequest()))
      .rejects.toThrow("missing or extra fields");
    const wrongState = harness({ ok: true, status: 200, body: { ...base, state: "published" } });
    await expect(wrongState.client.recordStaged(recordStagedRequest()))
      .rejects.toThrow("state is invalid");
    const badEdit = harness({ ok: true, status: 200, body: { ...base, edit_id: "not-a-uuid" } });
    await expect(badEdit.client.recordStaged(recordStagedRequest()))
      .rejects.toThrow("canonical UUID");
  });

  it("keeps transport and rejection errors free of bodies, secrets, and paths", async () => {
    const transport = harness({ ok: false, status: 0, throws: true });
    let transportError: unknown;
    await transport.client.recordStaged(recordStagedRequest()).catch((error: unknown) => {
      transportError = error;
    });
    expect(String(transportError)).not.toContain(SECRET);
    expect(String(transportError)).not.toContain("socket reset");
    expect(String(transportError)).toContain("unavailable");

    const rejected = harness({ ok: false, status: 500 });
    let rejection: unknown;
    await rejected.client.recordStaged(recordStagedRequest()).catch((error: unknown) => {
      rejection = error;
    });
    expect(String(rejection)).toContain("rejected request (500)");
    expect(String(rejection)).not.toContain(SECRET);

    const badJson = harness({ ok: true, status: 200, body: new Error("bad json") });
    await expect(badJson.client.recordStaged(recordStagedRequest()))
      .rejects.toThrow("not valid JSON");
  });
});
