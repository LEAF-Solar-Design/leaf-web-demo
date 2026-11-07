import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it, vi } from "vitest";
import { GrantStoreConfigError, grantKeyProviderFromEnv } from "../src/security/grantEnvelope/fromEnv.js";
import { KmsKeyProvider } from "../src/security/grantEnvelope/kmsKeyProvider.js";
import type { KmsGrantTransport } from "../src/security/grantEnvelope/kmsKeyProvider.js";
import { openGrant, sealGrant } from "../src/security/grantEnvelope/index.js";

const keyArn = "arn:aws:kms:us-east-1:123456789012:key/12345678-1234-1234-1234-123456789abc";
const keyError = "LEAF_GRANT_STORE=vault requires LEAF_GRANT_KMS_KEY_ARN to be a full KMS key ARN";

function fakeTransport(): KmsGrantTransport {
  let storedKey: Buffer;
  return {
    async encrypt(request) {
      storedKey = Buffer.from(request.Plaintext);
      return { KeyId: request.KeyId, CiphertextBlob: Buffer.from("wrapped-key") };
    },
    async decrypt(request) {
      return { KeyId: request.KeyId, Plaintext: Buffer.from(storedKey) };
    },
  };
}

describe("grantKeyProviderFromEnv", () => {
  it.each([undefined, "file", " FILE "])("leaves file backend %s without a provider", (backend) => {
    const factory = vi.fn(fakeTransport);
    const env: NodeJS.ProcessEnv = backend === undefined ? {} : { LEAF_GRANT_STORE: backend };
    expect(grantKeyProviderFromEnv(env, factory)).toBeUndefined();
    expect(factory).not.toHaveBeenCalled();
  });

  it("rejects an unknown backend without including its value", () => {
    const value = "unknown-sensitive-backend";
    const factory = vi.fn(fakeTransport);
    const build = () => grantKeyProviderFromEnv({ LEAF_GRANT_STORE: value }, factory);
    expect(build).toThrow(GrantStoreConfigError);
    expect(build).toThrow(expect.objectContaining({
      name: "GrantStoreConfigError",
      message: "LEAF_GRANT_STORE must be file or vault",
    }));
    expect(build).not.toThrow(value);
    expect(factory).not.toHaveBeenCalled();
  });

  it("builds a KMS provider using the trimmed key ARN and its region", () => {
    const factory = vi.fn(fakeTransport);
    const provider = grantKeyProviderFromEnv({
      LEAF_GRANT_STORE: " VAULT ",
      LEAF_GRANT_KMS_KEY_ARN: ` ${keyArn} `,
    }, factory);
    expect(factory).toHaveBeenCalledTimes(1);
    expect(factory).toHaveBeenCalledWith({ region: "us-east-1" });
    expect(provider).toBeInstanceOf(KmsKeyProvider);
    expect(provider?.keyId()).toBe(keyArn);
  });

  it.each([
    undefined,
    "",
    "   ",
    "alias/leaf-grant-envelope-staging-v1",
    "arn:aws:kms:us-east-1:123456789012:alias/x",
    `${keyArn} def`,
  ])("rejects invalid key configuration %s without including its value", (value) => {
    const factory = vi.fn(fakeTransport);
    const env: NodeJS.ProcessEnv = { LEAF_GRANT_STORE: "vault" };
    if (value !== undefined) env.LEAF_GRANT_KMS_KEY_ARN = value;
    const build = () => grantKeyProviderFromEnv(env, factory);
    expect(build).toThrow(GrantStoreConfigError);
    expect(build).toThrow(expect.objectContaining({ name: "GrantStoreConfigError", message: keyError }));
    if (value) expect(build).not.toThrow(value);
    expect(factory).not.toHaveBeenCalled();
  });

  it("round trips a sealed grant through the configured provider", async () => {
    const provider = grantKeyProviderFromEnv({
      LEAF_GRANT_STORE: "vault",
      LEAF_GRANT_KMS_KEY_ARN: keyArn,
    }, fakeTransport);
    if (!provider) throw new Error("Expected a vault provider");
    const context = { tenantId: "tenant-a", grantId: "grant-a" };
    const plaintext = Buffer.from("grant-access-token");
    const sealed = await sealGrant(plaintext, context, provider);
    expect(await openGrant(sealed, context, provider)).toEqual(plaintext);
  });

  it("wires the provider and one backend boot line into the server", () => {
    const source = readFileSync(join(process.cwd(), "src/server.ts"), "utf8");
    expect(source.split("grantKeyProviderFromEnv(")).toHaveLength(2);
    expect(source.split("grant_store_backend=")).toHaveLength(2);
    expect(source).not.toContain('grants: { kind: "file"');
  });
});
