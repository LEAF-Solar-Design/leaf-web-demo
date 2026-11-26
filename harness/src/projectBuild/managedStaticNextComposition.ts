/** Protected, offline-constructible composition. Provisioning, caller auth,
 * durable admission and authenticated preview HTTP routes remain host-owned.
 */
import { GenericNextProducer, nextRequestDigest } from "./genericNextBuild.js";
import type { CandidateOperation, NextBuildScope, PrivateStaticArtifacts } from "./genericNextBuild.js";
import { ExecutorManagedNextRuntime } from "./managedNextRuntime.js";
import type { StaticNextJobExecutor } from "./managedNextRuntime.js";
import { DiskPrivateStaticArtifacts } from "./privateStaticArtifactStore.js";
import { DurablePrivateNextViews } from "./durablePrivateNextViews.js";
import { loadPrivateStaticView } from "./staticNextCustody.js";
import type { StaticNextObjectStore } from "./codebuildStaticNextExecutor.js";

type ProducerOptions = {
  repository: ConstructorParameters<typeof GenericNextProducer>[0]["repository"];
  executor: StaticNextJobExecutor;
  artifactRoot: string;
  artifactBindingId: string;
  isCurrent(scope: NextBuildScope, operation: CandidateOperation): Promise<boolean>;
};

export function createManagedStaticNextProducer(options: ProducerOptions): GenericNextProducer {
  return createManagedStaticNextProducerComposition(options).producer;
}

export function createManagedStaticNextProducerComposition(options: ProducerOptions) {
  const artifacts = new DiskPrivateStaticArtifacts({ root: options.artifactRoot,
    bindingId: options.artifactBindingId, isCurrent: options.isCurrent });
  const producer = new GenericNextProducer({ repository: options.repository,
    runtime: new ExecutorManagedNextRuntime(options.executor), artifacts, isCurrent: options.isCurrent });
  return { producer, artifacts };
}

/** Compose readback only; the protected caller supplies current candidate access. */
export function createManagedStaticNextViews(options: {
  artifacts: PrivateStaticArtifacts;
  custodyStore: StaticNextObjectStore;
  resolveStaticNextBundle(executionId: string): Promise<string | null>;
  loadCandidate: ConstructorParameters<typeof DurablePrivateNextViews>[0]["loadCandidate"];
  now(): Date;
}): DurablePrivateNextViews {
  return new DurablePrivateNextViews({
    artifacts: options.artifacts,
    loadCandidate: options.loadCandidate,
    catalog: {
      async load(authority, request) {
        const { jobId, sourceCommit, sourceTree } = request;
        const executionId = nextRequestDigest({ ...authority, jobId, sourceCommit, sourceTree });
        const bundleSha256 = await options.resolveStaticNextBundle(executionId);
        if (!bundleSha256) return null;
        return loadPrivateStaticView({ store: options.custodyStore, artifacts: options.artifacts,
          authority, request, bundleSha256, now: options.now });
      },
    },
  });
}
