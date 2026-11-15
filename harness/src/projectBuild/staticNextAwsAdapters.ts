import { GetObjectCommand, PutObjectCommand } from "@aws-sdk/client-s3";
import type { GetObjectCommandOutput, PutObjectCommandOutput } from "@aws-sdk/client-s3";
import { BatchGetBuildsCommand, StartBuildCommand, StopBuildCommand } from "@aws-sdk/client-codebuild";
import type { BatchGetBuildsCommandOutput, StartBuildCommandOutput, StopBuildCommandOutput } from "@aws-sdk/client-codebuild";
import type { StaticNextBuildProvider, StaticNextBuildState, StaticNextObjectStore } from "./codebuildStaticNextExecutor.js";

export interface StaticNextS3Client {
  send(command: PutObjectCommand): Promise<PutObjectCommandOutput>;
  send(command: GetObjectCommand): Promise<GetObjectCommandOutput>;
}
export interface StaticNextCodeBuildClient {
  send(command: StartBuildCommand): Promise<StartBuildCommandOutput>;
  send(command: BatchGetBuildsCommand): Promise<BatchGetBuildsCommandOutput>;
  send(command: StopBuildCommand): Promise<StopBuildCommandOutput>;
}

function validKey(key: string): void {
  if (!/^(in|out|map)\/[0-9a-f]{64}\.json$/.test(key) || !key.endsWith(".json")) throw new Error("static_next_key_invalid");
}
function awsError(error: unknown): { name?: string; $metadata?: { httpStatusCode?: number } } {
  return error && typeof error === "object" ? error : {};
}

export function createS3StaticNextObjectStore(options: { client: StaticNextS3Client; bucket: string }): StaticNextObjectStore {
  const { client, bucket } = options;
  return {
    async putIfAbsent(key, body) {
      validKey(key);
      for (let attempt = 0; ; attempt++) {
        try {
          await client.send(new PutObjectCommand({ Bucket: bucket, Key: key, Body: body,
            IfNoneMatch: "*", ServerSideEncryption: "AES256" }));
          return "created";
        } catch (error) {
          const { name, $metadata } = awsError(error);
          if (name === "PreconditionFailed" || $metadata?.httpStatusCode === 412) return "exists";
          if (name === "ConditionalRequestConflict" || $metadata?.httpStatusCode === 409) {
            if (attempt === 0) continue;
            return "exists";
          }
          throw error;
        }
      }
    },
    async get(key) {
      validKey(key);
      try {
        const response = await client.send(new GetObjectCommand({ Bucket: bucket, Key: key }));
        if (!response.Body) throw new Error("static_next_body_missing");
        return Buffer.from(await response.Body.transformToByteArray());
      } catch (error) {
        const { name, $metadata } = awsError(error);
        if (name === "NoSuchKey" || $metadata?.httpStatusCode === 404) return null;
        throw error;
      }
    },
  };
}

export function createCodeBuildStaticNextBuildProvider(options: {
  client: StaticNextCodeBuildClient; projectName: string;
}): StaticNextBuildProvider {
  const { client, projectName } = options;
  return {
    async start(executionId) {
      const response = await client.send(new StartBuildCommand({ projectName,
        environmentVariablesOverride: [{ name: "EXECUTION_ID", value: executionId, type: "PLAINTEXT" }] }));
      if (!response.build?.id) throw new Error("static_next_build_id_missing");
      return response.build.id;
    },
    async state(buildId) {
      const response = await client.send(new BatchGetBuildsCommand({ ids: [buildId] }));
      const status = response.builds?.find(build => build.id === buildId)?.buildStatus;
      switch (status) {
        case "QUEUED": case "IN_PROGRESS": case "SUCCEEDED": case "FAILED":
        case "FAULT": case "TIMED_OUT": case "STOPPED":
          return status as StaticNextBuildState;
        default: return "FAULT";
      }
    },
    async stop(buildId) {
      await client.send(new StopBuildCommand({ id: buildId }));
    },
  };
}
