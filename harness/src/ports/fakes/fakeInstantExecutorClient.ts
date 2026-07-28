import type { InstantExecutorClient, InstantInvocation, InstantInvocationResponse, InstantSessionAssignment } from "../index.js";

export class FakeInstantExecutorClient implements InstantExecutorClient {
  readonly calls: Array<{ assignment: InstantSessionAssignment; invocation: InstantInvocation }> = [];
  failure: Error | null = null;
  response: Partial<InstantInvocationResponse> = {};

  async invoke(assignment: InstantSessionAssignment, invocation: InstantInvocation): Promise<InstantInvocationResponse> {
    this.calls.push({ assignment, invocation });
    if (this.failure) throw this.failure;
    return {
      contract: "leaf.instant-execution/v1", invocation_id: invocation.invocation_id,
      tenant_id: invocation.tenant_id, session_id: invocation.session_id,
      status: "succeeded", code_digest: invocation.code_digest,
      completed_at: new Date().toISOString(), result: { ok: true }, ...this.response,
    } as InstantInvocationResponse;
  }
}
