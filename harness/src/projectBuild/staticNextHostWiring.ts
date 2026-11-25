import { DiskStaticNextCustodyStore } from "./diskStaticNextCustodyStore.js";
import { tmpdir } from "node:os";
import { isAbsolute, relative, resolve, sep } from "node:path";
import { S3Client } from "@aws-sdk/client-s3";
import { CodeBuildClient } from "@aws-sdk/client-codebuild";
import { createCodeBuildStaticNextExecutor, createStaticNextBundleResolver } from "./codebuildStaticNextExecutor.js";
import { createCodeBuildStaticNextBuildProvider, createS3StaticNextObjectStore } from "./staticNextAwsAdapters.js";
import type { StaticNextCodeBuildClient, StaticNextS3Client } from "./staticNextAwsAdapters.js";

export interface StaticNextHostDependencies {
  s3Client?: StaticNextS3Client;
  codeBuildClient?: StaticNextCodeBuildClient;
}

/** Explicit opt-in only; callers decide when to install the host binding. */
export function createHostStaticNextExecutor(env: Record<string, string | undefined>, deps: StaticNextHostDependencies = {}) {
  if (env.LEAF_STATIC_NEXT_EXECUTOR !== "enabled") return null;
  const invalid = (name: string): never => { throw new Error(`static_next_config_invalid: ${name}`); };
  const required = (name: string): string => {
    const value = env[name];
    if (!value?.trim()) return invalid(name);
    return value;
  };
  const bucket = required("LEAF_STATIC_NEXT_BUCKET");
  const projectName = required("LEAF_STATIC_NEXT_PROJECT");
  const toolchainSha256 = required("LEAF_STATIC_NEXT_TOOLCHAIN_SHA256");
  if (toolchainSha256.length !== 64 || !/^[0-9a-f]{64}$/.test(toolchainSha256)) invalid("LEAF_STATIC_NEXT_TOOLCHAIN_SHA256");
  const bindingId = required("LEAF_STATIC_NEXT_BINDING_ID");
  if (bindingId.length !== 36 || !/^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/.test(bindingId)) invalid("LEAF_STATIC_NEXT_BINDING_ID");
  const root = required("LEAF_STATIC_NEXT_ARTIFACT_ROOT");
  if (!isAbsolute(root)) invalid("LEAF_STATIC_NEXT_ARTIFACT_ROOT");
  const artifactRoot = resolve(root);
  const fromTmp = relative(resolve(tmpdir()), artifactRoot);
  if (fromTmp === "" || (!isAbsolute(fromTmp) && fromTmp !== ".." && !fromTmp.startsWith(`..${sep}`))) {
    invalid("LEAF_STATIC_NEXT_ARTIFACT_ROOT");
  }
  const region = env.AWS_REGION;
  const store = createS3StaticNextObjectStore({ bucket, client: deps.s3Client ?? new S3Client({ region }) });
  const provider = createCodeBuildStaticNextBuildProvider({ projectName,
    client: deps.codeBuildClient ?? new CodeBuildClient({ region }) });
  return { executor: createCodeBuildStaticNextExecutor({ bindingId, toolchainSha256, store, provider }),
    custodyStore: new DiskStaticNextCustodyStore(artifactRoot),
    resolveStaticNextBundle: createStaticNextBundleResolver(store), artifactRoot };
}
