import { DecryptCommand, EncryptCommand } from "@aws-sdk/client-kms";
import { describe, expect, it, vi } from "vitest";
import { GrantEnvelopeError, KmsKeyProvider, openGrant, sealGrant } from "../src/security/grantEnvelope/index.js";
import { createKmsGrantTransport, kmsClientConfig } from "../src/security/grantEnvelope/kmsTransport.js";
import type { KmsSendClient } from "../src/security/grantEnvelope/kmsTransport.js";

const arn = "arn:aws:kms:us-east-1:123456789012:key/12345678-1234-1234-1234-123456789abc";
const context = { tenantId: "synthetic-tenant", grantId: "synthetic-grant" };
const EncryptionContext = Object.freeze({ purpose: "leaf.grant-envelope.v1" as const, tenant: context.tenantId, grant: context.grantId });
const fields = { KeyId: arn, EncryptionAlgorithm: "SYMMETRIC_DEFAULT" as const, EncryptionContext };
const plaintext = Buffer.alloc(32, 37);
const ciphertext = Buffer.from([251, 255, 0, 128]);

async function failure(promise: Promise<unknown>) {
  const error: unknown = await promise.then(() => { throw new Error("unexpected success"); }, value => value);
  expect(error).toBeInstanceOf(GrantEnvelopeError);
  expect(error).toMatchObject({ message: "Grant envelope operation failed" });
  expect(error).not.toHaveProperty("cause");
  expect(String(error)).not.toContain("arn:");
  return error;
}

describe("KmsGrantTransport", () => {
  it("sends an EncryptCommand with copied context and the caller signal, returning only encrypt fields", async () => {
    const client = { send: vi.fn<KmsSendClient["send"]>(async () => ({
      KeyId: arn, CiphertextBlob: ciphertext, $metadata: { requestId: "synthetic-request" }, EncryptionAlgorithm: "SYMMETRIC_DEFAULT",
    })) };
    const transport = createKmsGrantTransport({ region: "us-east-1", client });
    const abortSignal = new AbortController().signal;
    const request = { ...fields, Plaintext: plaintext };
    const result = await transport.encrypt(request, { abortSignal });
    expect(result).toStrictEqual({ KeyId: arn, CiphertextBlob: ciphertext });
    expect(result.CiphertextBlob).toBe(ciphertext);
    expect(client.send).toHaveBeenCalledTimes(1);
    const [command, options] = client.send.mock.calls[0];
    expect(command).toBeInstanceOf(EncryptCommand);
    if (!(command instanceof EncryptCommand)) throw new Error("unexpected command");
    expect(command.input).toStrictEqual(request);
    expect(command.input.EncryptionContext).not.toBe(EncryptionContext);
    expect(Object.getPrototypeOf(command.input.EncryptionContext)).toBe(Object.prototype);
    expect(Object.isFrozen(command.input.EncryptionContext)).toBe(false);
    expect(command.input.Plaintext).toBe(plaintext);
    expect(options).toStrictEqual({ abortSignal });
    expect(options.abortSignal).toBe(abortSignal);
  });

  it("sends a DecryptCommand with copied context and the caller signal, returning only decrypt fields", async () => {
    const client = { send: vi.fn<KmsSendClient["send"]>(async () => ({
      KeyId: arn, Plaintext: plaintext, $metadata: { requestId: "synthetic-request" }, EncryptionAlgorithm: "SYMMETRIC_DEFAULT",
    })) };
    const transport = createKmsGrantTransport({ region: "us-east-1", client });
    const abortSignal = new AbortController().signal;
    const request = { ...fields, CiphertextBlob: ciphertext };
    const result = await transport.decrypt(request, { abortSignal });
    expect(result).toStrictEqual({ KeyId: arn, Plaintext: plaintext });
    expect(result.Plaintext).toBe(plaintext);
    expect(client.send).toHaveBeenCalledTimes(1);
    const [command, options] = client.send.mock.calls[0];
    expect(command).toBeInstanceOf(DecryptCommand);
    if (!(command instanceof DecryptCommand)) throw new Error("unexpected command");
    expect(command.input).toStrictEqual(request);
    expect(command.input.EncryptionContext).not.toBe(EncryptionContext);
    expect(Object.getPrototypeOf(command.input.EncryptionContext)).toBe(Object.prototype);
    expect(Object.isFrozen(command.input.EncryptionContext)).toBe(false);
    expect(command.input.CiphertextBlob).toBe(ciphertext);
    expect(options).toStrictEqual({ abortSignal });
    expect(options.abortSignal).toBe(abortSignal);
  });

  it("redacts both synchronous throws and rejections to fresh errors without causes", async () => {
    const original = new Error("arn:aws:kms:... secret detail");
    for (const synchronous of [true, false]) {
      const client = { send: vi.fn<KmsSendClient["send"]>(() => {
        if (synchronous) throw original;
        return Promise.reject(original);
      }) };
      const transport = createKmsGrantTransport({ region: "us-east-1", client });
      const options = { abortSignal: new AbortController().signal };
      const encryptError = await failure(transport.encrypt({ ...fields, Plaintext: plaintext }, options));
      const decryptError = await failure(transport.decrypt({ ...fields, CiphertextBlob: ciphertext }, options));
      expect(encryptError).not.toBe(original);
      expect(decryptError).not.toBe(original);
      expect(encryptError).not.toBe(decryptError);
    }
  });

  it("rejects non-object outputs and preserves missing fields as explicit undefined values", async () => {
    const client = { send: vi.fn<KmsSendClient["send"]>() };
    const transport = createKmsGrantTransport({ region: "us-east-1", client });
    const options = { abortSignal: new AbortController().signal };
    for (const output of [undefined, null, false, 42, "response", []]) {
      client.send.mockResolvedValue(output);
      await failure(transport.encrypt({ ...fields, Plaintext: plaintext }, options));
      await failure(transport.decrypt({ ...fields, CiphertextBlob: ciphertext }, options));
    }
    client.send.mockResolvedValue({});
    expect(await transport.encrypt({ ...fields, Plaintext: plaintext }, options)).toStrictEqual({ KeyId: undefined, CiphertextBlob: undefined });
    expect(await transport.decrypt({ ...fields, CiphertextBlob: ciphertext }, options)).toStrictEqual({ KeyId: undefined, Plaintext: undefined });
  });

  it("rejects invalid regions and supplied clients without send", () => {
    const client = { send: vi.fn<KmsSendClient["send"]>() };
    for (const region of ["", "us east 1", "US-EAST-1", undefined, "us-east-1 ", "us-east-1\n", "us-east", null, 42]) {
      expect(() => createKmsGrantTransport({ region: region as string, client })).toThrow(GrantEnvelopeError);
    }
    for (const invalid of [{}, { send: 42 }, null]) {
      expect(() => createKmsGrantTransport({ region: "us-east-1", client: invalid as unknown as KmsSendClient })).toThrow(GrantEnvelopeError);
    }
    expect(client.send).not.toHaveBeenCalled();
  });

  it("uses a region-only client config and constructs the real transport without sending", () => {
    expect(kmsClientConfig("us-east-1")).toStrictEqual({ region: "us-east-1" });
    const transport = createKmsGrantTransport({ region: "us-east-1" });
    expect(transport.encrypt).toBeTypeOf("function");
    expect(transport.decrypt).toBeTypeOf("function");
  });

  it("round trips sealGrant and openGrant through fake SDK commands", async () => {
    let storedKey: Buffer | undefined;
    const client = { send: vi.fn<KmsSendClient["send"]>(async command => {
      if (command instanceof EncryptCommand) {
        storedKey = Buffer.from(command.input.Plaintext as Uint8Array);
        expect(storedKey.length).toBe(32);
        return { KeyId: command.input.KeyId, CiphertextBlob: ciphertext };
      }
      expect(command).toBeInstanceOf(DecryptCommand);
      expect(command.input.CiphertextBlob).toEqual(ciphertext);
      expect(storedKey).toBeDefined();
      return { KeyId: command.input.KeyId, Plaintext: Buffer.from(storedKey!) };
    }) };
    const keys = new KmsKeyProvider({ keyArn: arn, transport: createKmsGrantTransport({ region: "us-east-1", client }) });
    const input = "synthetic-grant-value";
    const sealed = await sealGrant(input, context, keys);
    expect(await openGrant(sealed, context, keys)).toEqual(Buffer.from(input));
    expect(client.send).toHaveBeenCalledTimes(2);
  });
});
