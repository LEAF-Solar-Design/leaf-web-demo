/** Protected, offline-constructible composition. Provisioning, caller auth,
 * durable admission and authenticated preview HTTP routes remain host-owned.
 */
import { GenericNextProducer } from "./genericNextBuild.js";
import type { NextBuildScope } from "./genericNextBuild.js";
import { ExecutorManagedNextRuntime } from "./managedNextRuntime.js";
import type { StaticNextJobExecutor } from "./managedNextRuntime.js";
import { DiskPrivateStaticArtifacts } from "./privateStaticArtifactStore.js";

export function createManagedStaticNextProducer(options: {
  repository: ConstructorParameters<typeof GenericNextProducer>[0]["repository"];
  executor: StaticNextJobExecutor;
  artifactRoot: string;
  artifactBindingId: string;
  isCurrent(scope: NextBuildScope): Promise<boolean>;
}): GenericNextProducer {
  return new GenericNextProducer({ repository: options.repository,
    runtime: new ExecutorManagedNextRuntime(options.executor),
    artifacts: new DiskPrivateStaticArtifacts({ root: options.artifactRoot,
      bindingId: options.artifactBindingId, isCurrent: options.isCurrent }),
    isCurrent: options.isCurrent });
}
