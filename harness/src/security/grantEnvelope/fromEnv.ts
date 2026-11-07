import type { KeyProvider } from "./keyProvider.js";
import { KmsKeyProvider } from "./kmsKeyProvider.js";
import type { KmsGrantTransport } from "./kmsKeyProvider.js";
import { createKmsGrantTransport } from "./kmsTransport.js";

export type KmsTransportFactory = (options: { region: string }) => KmsGrantTransport;

export class GrantStoreConfigError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "GrantStoreConfigError";
  }
}

const KEY_ARN = /^arn:aws(?:-[a-z]+)*:kms:([a-z]{2}(?:-[a-z0-9]+)+-\d+):\d{12}:key\/(?:[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}|mrk-[0-9a-fA-F]{32})$/;

export function grantKeyProviderFromEnv(
  env: NodeJS.ProcessEnv = process.env,
  transportFactory: KmsTransportFactory = createKmsGrantTransport,
): KeyProvider | undefined {
  const backend = (env.LEAF_GRANT_STORE ?? "file").trim().toLowerCase();
  if (backend === "file") return undefined;
  if (backend !== "vault") {
    throw new GrantStoreConfigError("LEAF_GRANT_STORE must be file or vault");
  }
  const keyArn = (env.LEAF_GRANT_KMS_KEY_ARN ?? "").trim();
  const match = KEY_ARN.exec(keyArn);
  if (!match) {
    throw new GrantStoreConfigError("LEAF_GRANT_STORE=vault requires LEAF_GRANT_KMS_KEY_ARN to be a full KMS key ARN");
  }
  return new KmsKeyProvider({ keyArn, transport: transportFactory({ region: match[1] }) });
}
