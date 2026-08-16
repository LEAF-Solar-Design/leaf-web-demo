import { describe, expect, it, vi } from "vitest";

import type { TenantChangeSet } from "../src/vendor/mushy-author/ports/impl/tenantChangeRepo.js";
import {
  ProjectRepositoryEditCoordinator,
  ProjectRepositoryEditError,
  ProjectRepositoryEditSettlementUnavailable,
  type ProjectRepositoryEditCoordinatorPorts,
  type ProjectRepositoryEditGit,
} from "../src/agent/projectRepositoryEditCoordinator.js";
import {
  PROJECT_REPOSITORY_EDIT_COORDINATION_CONTRACT,
  type ProjectRepositoryAuthority,
  type ProjectRepositoryEditCoordination,
  type ProjectRepositoryEditPublishMatrix,
  type ProjectRepositoryEditRecordStagedResponse,
  type ProjectRepositoryEditSettlementResponse,
  type TenantRepoProvider,
  type WriterLeaseWitness,
} from "../src/ports/index.js";

const AUTHORITY: ProjectRepositoryAuthority = {
  tenantId: "11111111-1111-4111-8111-111111111111",
  organizationId: "22222222-2222-4222-8222-222222222222",
  projectId: "33333333-3333-4333-8333-333333333333",
  repoKey: "44444444-4444-4444-8444-444444444444",
};
const EDIT_ID = "55555555-5555-4555-8555-555555555555";
const ACTOR_ID = "66666666-6666-4666-8666-666666666666";
const CONFIRMATION_ID = "77777777-7777-4777-8777-777777777777";
const STAGE_LEASE = "88888888-8888-4888-8888-888888888888";
const PUBLISH_LEASE = "99999999-9999-4999-8999-999999999999";
const RECOVERY_LEASE = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const BASE = "1".repeat(40);
const STAGED_HEAD = "2".repeat(40);
const STAGED_TREE = "3".repeat(40);
const DIFF_DIGEST = "4".repeat(64);
const INSTRUCTION_DIGEST = "5".repeat(64);
const RECEIPT_DIGEST_PLACEHOLDER = "6".repeat(64);

function privateRef(editId: string): string {
  return `refs/leaf/changes/${editId}`;
}

function witness(leaseId: string, generation: string): WriterLeaseWitness {
  return { writerLeaseId: leaseId, writerLeaseGeneration: generation };
}

function leaseProvider(generation: string, leaseId: string = STAGE_LEASE): TenantRepoProvider {
  return {
    withProjectWriterLease: vi.fn(async (_authority, action) =>
      action(witness(leaseId, generation), async <T,>(fn: () => T | Promise<T>) => fn())),
  } as unknown as TenantRepoProvider;
}

function fakeGit(overrides: Partial<ProjectRepositoryEditGit> = {}): ProjectRepositoryEditGit {
  return {
    createOrResume: vi.fn(() => ({
      id: EDIT_ID, ref: privateRef(EDIT_ID), dir: "/tmp/change", expectedBaseSha: BASE,
      stagedSha: null,
    }) as TenantChangeSet),
    stageCommitWitness: vi.fn(() => Object.freeze({
      base_commit: BASE, staged_head_commit: STAGED_HEAD, staged_tree: STAGED_TREE,
      private_ref: privateRef(EDIT_ID),
    })),
    publishToMainObserved: vi.fn(() => Object.freeze({
      private_ref: privateRef(EDIT_ID), private_ref_commit: STAGED_HEAD,
      before_main_commit: BASE, after_main_commit: STAGED_HEAD, after_main_tree: STAGED_TREE,
      compare_and_swap: true,
    })),
    observeGitMatrix: vi.fn(() => Object.freeze({
      private_ref_commit: STAGED_HEAD, main_commit: BASE, main_tree: BASE,
    })),
    resolveCommitTree: vi.fn(() => STAGED_TREE),
    cleanupWorktree: vi.fn(),
    readRef: vi.fn(() => BASE),
    ...overrides,
  };
}

function fakeCoordination(
  overrides: Partial<ProjectRepositoryEditCoordination> = {},
): ProjectRepositoryEditCoordination & Record<string, ReturnType<typeof vi.fn>> {
  return {
    recordStaged: vi.fn(async (): Promise<ProjectRepositoryEditRecordStagedResponse> => Object.freeze({
      contract: PROJECT_REPOSITORY_EDIT_COORDINATION_CONTRACT,
      action: "record_staged", edit_id: EDIT_ID, state: "staged", version: 1,
    })),
    authorizePublish: vi.fn(async (): Promise<ProjectRepositoryEditPublishMatrix> => Object.freeze({
      contract: PROJECT_REPOSITORY_EDIT_COORDINATION_CONTRACT,
      action: "authorize_publish", edit_id: EDIT_ID, state: "publishing", version: 2,
      receipt_digest: RECEIPT_DIGEST_PLACEHOLDER, expected_main_commit: BASE,
      staged_head_commit: STAGED_HEAD, staged_tree: STAGED_TREE, private_ref: privateRef(EDIT_ID),
      publish_lease_id: PUBLISH_LEASE, publish_lease_generation: 8,
    })),
    settlePublish: vi.fn(async (): Promise<ProjectRepositoryEditSettlementResponse> => Object.freeze({
      contract: PROJECT_REPOSITORY_EDIT_COORDINATION_CONTRACT,
      action: "settle_publish", edit_id: EDIT_ID, state: "published", version: 3,
    })),
    recoverPublish: vi.fn(async (): Promise<ProjectRepositoryEditSettlementResponse> => Object.freeze({
      contract: PROJECT_REPOSITORY_EDIT_COORDINATION_CONTRACT,
      action: "recover_publish", edit_id: EDIT_ID, state: "published", version: 3,
    })),
    ...overrides,
  } as ProjectRepositoryEditCoordination & Record<string, ReturnType<typeof vi.fn>>;
}

function coordinator(ports: Partial<ProjectRepositoryEditCoordinatorPorts> = {}) {
  const full: ProjectRepositoryEditCoordinatorPorts = {
    leases: ports.leases ?? leaseProvider("7"),
    changeRepo: ports.changeRepo ?? (() => fakeGit()),
    coordination: ports.coordination ?? fakeCoordination(),
  };
  return new ProjectRepositoryEditCoordinator(full);
}

function stageRequest(overrides: Record<string, unknown> = {}) {
  return {
    authority: AUTHORITY,
    editId: EDIT_ID,
    actorBindingId: ACTOR_ID,
    operation: "edit" as const,
    sourceEditId: null,
    expectedBaseCommit: BASE,
    changedPaths: ["src/b.py", "src/a.py"],
    diffDigest: DIFF_DIGEST,
    instructionDigest: INSTRUCTION_DIGEST,
    idempotencyKey: "stage-key",
    commitMessage: "stage change",
    apply: vi.fn(async () => {}),
    ...overrides,
  };
}

describe("ProjectRepositoryEditCoordinator.stageEdit", () => {
  it("stages a bounded edit, sorts changed paths, and binds the stage lease witness", async () => {
    const coordination = fakeCoordination();
    const git = fakeGit();
    const c = coordinator({ leases: leaseProvider("7"), changeRepo: () => git, coordination });

    const staged = await c.stageEdit(stageRequest());

    expect(staged.receipt.writer_lease_id).toBe(STAGE_LEASE);
    expect(staged.receipt.writer_lease_generation).toBe(7);
    expect(staged.receipt.changed_paths).toEqual(["src/a.py", "src/b.py"]);
    expect(staged.receipt.base_commit).toBe(BASE);
    expect(staged.receipt.staged_head_commit).toBe(STAGED_HEAD);
    expect(staged.receipt.staged_tree).toBe(STAGED_TREE);
    expect(staged.version).toBe(1);
    expect(coordination.recordStaged).toHaveBeenCalledTimes(1);
    const request = (coordination.recordStaged as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(request.action).toBe("record_staged");
    expect(request.expected_version).toBe(0);
    expect(request.transition_key).toBe("stage-key");
    expect(request.receipt_digest).toBe(staged.receiptDigest);
    expect(git.cleanupWorktree).toHaveBeenCalledTimes(1);
  });

  it("refuses to stage when main moved during the edit, but still cleans up the worktree", async () => {
    const git = fakeGit({ readRef: vi.fn(() => "9".repeat(40)) });
    const c = coordinator({ changeRepo: () => git });

    await expect(c.stageEdit(stageRequest())).rejects.toMatchObject({ code: "main_moved_during_stage" });
    expect(git.cleanupWorktree).toHaveBeenCalledTimes(1);
  });

  it("refuses to stage without a project writer lease", async () => {
    const c = coordinator({ leases: {} as TenantRepoProvider });
    await expect(c.stageEdit(stageRequest())).rejects.toMatchObject({ code: "project_writer_lease_required" });
  });

  it("rejects a malformed authority tuple before touching any port", async () => {
    const coordination = fakeCoordination();
    const c = coordinator({ coordination });
    await expect(c.stageEdit(stageRequest({
      authority: { ...AUTHORITY, repoKey: "not-a-uuid" },
    }))).rejects.toMatchObject({ code: "invalid_authority" });
    expect(coordination.recordStaged).not.toHaveBeenCalled();
  });
});

function publishRequest(overrides: Record<string, unknown> = {}) {
  return {
    authority: AUTHORITY,
    editId: EDIT_ID,
    actorBindingId: ACTOR_ID,
    confirmationId: CONFIRMATION_ID,
    receiptDigest: RECEIPT_DIGEST_PLACEHOLDER,
    expectedVersion: 1,
    transitionKey: "publish-key",
    ...overrides,
  };
}

describe("ProjectRepositoryEditCoordinator.publishEdit", () => {
  it("publishes under a strictly newer publish lease and returns the settlement", async () => {
    const coordination = fakeCoordination();
    const git = fakeGit();
    const c = coordinator({ leases: leaseProvider("8", PUBLISH_LEASE), changeRepo: () => git, coordination });

    const published = await c.publishEdit(publishRequest());

    expect(published.matrix.state).toBe("publishing");
    expect(published.observation.compare_and_swap).toBe(true);
    expect(published.settlement.state).toBe("published");
    expect(git.resolveCommitTree).toHaveBeenCalledWith(STAGED_HEAD);
    expect(git.publishToMainObserved).toHaveBeenCalledTimes(1);
    const settleRequest = (coordination.settlePublish as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(settleRequest.publish_lease_id).toBe(PUBLISH_LEASE);
    expect(settleRequest.transition_key).toBe("publish-key:settle");
  });

  it("rechecks the staged tree before any compare-and-swap and refuses on drift", async () => {
    const git = fakeGit({ resolveCommitTree: vi.fn(() => "f".repeat(40)) });
    const c = coordinator({ leases: leaseProvider("8", PUBLISH_LEASE), changeRepo: () => git });

    await expect(c.publishEdit(publishRequest())).rejects.toMatchObject({ code: "staged_tree_mismatch" });
    expect(git.publishToMainObserved).not.toHaveBeenCalled();
  });

  it.each([
    ["edit_id", { edit_id: "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee" }],
    ["receipt_digest", { receipt_digest: "f".repeat(64) }],
    ["publish_lease_id", { publish_lease_id: "b".repeat(8) + "-bbbb-4bbb-8bbb-bbbbbbbbbbbb" }],
    ["private_ref", { private_ref: "refs/leaf/changes/not-the-edit" }],
    ["state", { state: "conflicted" }],
  ])("refuses a publish matrix with a mismatched %s before any Git mutation", async (_label, patch) => {
    const coordination = fakeCoordination({
      authorizePublish: vi.fn(async () => Object.freeze({
        contract: PROJECT_REPOSITORY_EDIT_COORDINATION_CONTRACT,
        action: "authorize_publish", edit_id: EDIT_ID, state: "publishing", version: 2,
        receipt_digest: RECEIPT_DIGEST_PLACEHOLDER, expected_main_commit: BASE,
        staged_head_commit: STAGED_HEAD, staged_tree: STAGED_TREE, private_ref: privateRef(EDIT_ID),
        publish_lease_id: PUBLISH_LEASE, publish_lease_generation: 8,
        ...patch,
      }) as unknown as ProjectRepositoryEditPublishMatrix),
    });
    const git = fakeGit();
    const c = coordinator({ leases: leaseProvider("8", PUBLISH_LEASE), changeRepo: () => git, coordination });

    await expect(c.publishEdit(publishRequest())).rejects.toMatchObject({ code: "publish_matrix_mismatch" });
    expect(git.publishToMainObserved).not.toHaveBeenCalled();
  });

  it("refuses a publish observation that does not match the authorized matrix", async () => {
    const git = fakeGit({
      publishToMainObserved: vi.fn(() => Object.freeze({
        private_ref: privateRef(EDIT_ID), private_ref_commit: "f".repeat(40),
        before_main_commit: BASE, after_main_commit: "f".repeat(40), after_main_tree: STAGED_TREE,
        compare_and_swap: true,
      })),
    });
    const coordination = fakeCoordination();
    const c = coordinator({ leases: leaseProvider("8", PUBLISH_LEASE), changeRepo: () => git, coordination });

    await expect(c.publishEdit(publishRequest())).rejects.toMatchObject({ code: "publish_observation_mismatch" });
    expect(coordination.settlePublish).not.toHaveBeenCalled();
  });

  it("wraps a settlement transport failure with the frozen observation, never a second compare-and-swap", async () => {
    const git = fakeGit();
    const coordination = fakeCoordination({
      settlePublish: vi.fn(async () => { throw new Error("socket reset"); }),
    });
    const c = coordinator({ leases: leaseProvider("8", PUBLISH_LEASE), changeRepo: () => git, coordination });

    let captured: unknown;
    try {
      await c.publishEdit(publishRequest());
    } catch (error) {
      captured = error;
    }

    expect(captured).toBeInstanceOf(ProjectRepositoryEditSettlementUnavailable);
    const unavailable = captured as ProjectRepositoryEditSettlementUnavailable;
    expect(unavailable.observation.after_main_commit).toBe(STAGED_HEAD);
    expect(unavailable.settleRequest.main_tree).toBe(STAGED_TREE);
    expect(git.publishToMainObserved).toHaveBeenCalledTimes(1);
    expect(coordination.settlePublish).toHaveBeenCalledTimes(1);
  });
});

function recoverRequest(overrides: Record<string, unknown> = {}) {
  return {
    authority: AUTHORITY,
    editId: EDIT_ID,
    actorBindingId: ACTOR_ID,
    expectedMainCommit: BASE,
    stagedHeadCommit: STAGED_HEAD,
    stagedTree: STAGED_TREE,
    expectedVersion: 2,
    transitionKey: "recover-key",
    reasonCode: "settlement_transport_failure",
    ...overrides,
  };
}

describe("ProjectRepositoryEditCoordinator.recoverEdit", () => {
  it("observes an already-published transaction without a second compare-and-swap", async () => {
    const git = fakeGit({
      observeGitMatrix: vi.fn(() => Object.freeze({
        private_ref_commit: STAGED_HEAD, main_commit: STAGED_HEAD, main_tree: STAGED_TREE,
      })),
    });
    const coordination = fakeCoordination();
    const c = coordinator({ leases: leaseProvider("9", RECOVERY_LEASE), changeRepo: () => git, coordination });

    const recovered = await c.recoverEdit(recoverRequest());

    expect(recovered.compareAndSwap).toBe(false);
    expect(git.publishToMainObserved).not.toHaveBeenCalled();
    const recoverCall = (coordination.recoverPublish as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(recoverCall.main_commit).toBe(STAGED_HEAD);
    expect(recoverCall.recovery_lease_generation).toBe(9);
  });

  it("resumes a resumable interrupted publish with exactly one compare-and-swap", async () => {
    const git = fakeGit({
      observeGitMatrix: vi.fn(() => Object.freeze({
        private_ref_commit: STAGED_HEAD, main_commit: BASE, main_tree: BASE,
      })),
      publishToMainObserved: vi.fn(() => Object.freeze({
        private_ref: privateRef(EDIT_ID), private_ref_commit: STAGED_HEAD,
        before_main_commit: BASE, after_main_commit: STAGED_HEAD, after_main_tree: STAGED_TREE,
        compare_and_swap: true,
      })),
    });
    const c = coordinator({ leases: leaseProvider("9", RECOVERY_LEASE), changeRepo: () => git });

    const recovered = await c.recoverEdit(recoverRequest());

    expect(recovered.compareAndSwap).toBe(true);
    expect(git.publishToMainObserved).toHaveBeenCalledTimes(1);
  });

  it("stays observation-only when the frozen matrix matches neither published nor resumable shape", async () => {
    const git = fakeGit({
      observeGitMatrix: vi.fn(() => Object.freeze({
        private_ref_commit: STAGED_HEAD, main_commit: "f".repeat(40), main_tree: "f".repeat(40),
      })),
    });
    const coordination = fakeCoordination();
    const c = coordinator({ leases: leaseProvider("9", RECOVERY_LEASE), changeRepo: () => git, coordination });

    const recovered = await c.recoverEdit(recoverRequest());

    expect(recovered.compareAndSwap).toBe(false);
    expect(git.publishToMainObserved).not.toHaveBeenCalled();
    const recoverCall = (coordination.recoverPublish as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(recoverCall.main_commit).toBe("f".repeat(40));
  });

  it("rejects an incomplete frozen matrix before contacting the coordination port", async () => {
    const git = fakeGit({
      observeGitMatrix: vi.fn(() => Object.freeze({
        private_ref_commit: null, main_commit: null, main_tree: null,
      })),
    });
    const coordination = fakeCoordination();
    const c = coordinator({ leases: leaseProvider("9", RECOVERY_LEASE), changeRepo: () => git, coordination });

    await expect(c.recoverEdit(recoverRequest())).rejects.toMatchObject({ code: "recovery_witness_incomplete" });
    expect(coordination.recoverPublish).not.toHaveBeenCalled();
  });
});

describe("ProjectRepositoryEditError", () => {
  it("carries only a fixed code, never request content", () => {
    const error = new ProjectRepositoryEditError("main_moved_during_stage");
    expect(error.code).toBe("main_moved_during_stage");
    expect(error.name).toBe("ProjectRepositoryEditError");
  });
});
