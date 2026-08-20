// Operator worker dispatch (contract/OPERATOR.md Lane D). The capability-only
// operator HTTP handler NEVER executes broad/privileged work in the shared
// harness process. It forwards a BOUNDED job here, and this runs it through the
// OperatorWorkerManager, whose substrate is the isolated, egress-locked
// disposable workspace/microVM. If the substrate is not isolating, the manager
// refuses (substrate_not_isolating) rather than running broad work un-jailed.
//
// This is the binding that makes O2/O3 real: broad operator work executes ONLY
// in the isolated worker, with network fully denied, so a neutral helper, a
// subprocess, or an aliased host has no route to production. The handler holds
// no capability to run it anywhere else.

import {
  OperatorWorkerManager,
  type WorkerJobEnvelope,
  type WorkerJobReceipt,
} from "./workerManager.js";

/** The bounded request the capability-only handler forwards. It CANNOT widen
 * the isolation: the workspace is always disposable, network is always denied,
 * and the principal subject is server-attested. */
export interface OperatorWorkerDispatchRequest {
  commands: string[];
  repo?: string;
  idempotencyKey: string;
  principalSubject: string;
  tenantId: string;
  roleRevision: number;
  sessionId: string;
  timeoutMs?: number;
}

// Hard ceilings on a single operator job.
export const MAX_TIMEOUT_MS = 1_800_000;
export const MAX_COMMANDS = 50;

export class OperatorWorkerDispatchError extends Error {
  constructor(public readonly reason: string) {
    super(reason);
  }
}

/** Build the bounded, network-denied envelope. Network is NEVER allowlisted from
 * a dispatch request: broad operator work runs with no egress, so it cannot
 * reach a production deploy route regardless of what it attempts. */
export function buildOperatorWorkerEnvelope(
  req: OperatorWorkerDispatchRequest,
): WorkerJobEnvelope {
  if (!req.principalSubject) {
    throw new OperatorWorkerDispatchError("principal_required");
  }
  if (!req.tenantId || !Number.isInteger(req.roleRevision)) {
    throw new OperatorWorkerDispatchError("worker_binding_invalid");
  }
  if (!req.idempotencyKey) {
    throw new OperatorWorkerDispatchError("idempotency_key_required");
  }
  if (!Array.isArray(req.commands) || req.commands.length === 0
    || req.commands.length > MAX_COMMANDS) {
    throw new OperatorWorkerDispatchError("commands_invalid");
  }
  return {
    workspace: "disposable",
    commands: req.commands,
    repo: req.repo,
    network: [], // ALWAYS fully denied — no egress from broad operator work.
    idempotencyKey: req.idempotencyKey,
    principalSubject: req.principalSubject,
    tenantId: req.tenantId,
    roleRevision: req.roleRevision,
    sessionId: req.sessionId,
    timeoutMs: Math.min(req.timeoutMs ?? 120_000, MAX_TIMEOUT_MS),
  };
}

/** Run a dispatched operator job in the isolated worker. Propagates
 * substrate_not_isolating from the manager rather than ever running broad work
 * in a non-isolating substrate. */
export async function dispatchOperatorWorkerJob(
  manager: OperatorWorkerManager,
  req: OperatorWorkerDispatchRequest,
): Promise<WorkerJobReceipt> {
  const envelope = buildOperatorWorkerEnvelope(req);
  return manager.submit(envelope);
}

/** Admit a bounded job and return its active, owner-bound identity before the
 * worker finishes. Execution continues only inside the manager's substrate. */
export function startOperatorWorkerJob(
  manager: OperatorWorkerManager,
  req: OperatorWorkerDispatchRequest,
) {
  return manager.start(buildOperatorWorkerEnvelope(req));
}
