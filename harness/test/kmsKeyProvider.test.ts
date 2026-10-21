import { afterEach, describe, expect, it, vi } from "vitest";
import { GrantEnvelopeError, KmsKeyProvider, openGrant, sealGrant } from "../src/security/grantEnvelope/index.js";
import type { EnvelopeContext, KmsGrantTransport } from "../src/security/grantEnvelope/index.js";

const arn = "arn:aws:kms:us-east-1:123456789012:key/12345678-1234-1234-1234-123456789abc";
const otherArn = arn.replace("789abc", "789abd");
const context = { tenantId: "synthetic-tenant", grantId: "synthetic-grant" };
const kmsContext = { purpose: "leaf.grant-envelope.v1", tenant: context.tenantId, grant: context.grantId };
const ciphertext = Buffer.from([251, 255, 0, 128]);
const wrapped = ciphertext.toString("base64url");
const key = () => Buffer.alloc(32, 37);

function transport() {
  return {
    encrypt: vi.fn<KmsGrantTransport["encrypt"]>(async () => ({ KeyId: arn, CiphertextBlob: ciphertext })),
    decrypt: vi.fn<KmsGrantTransport["decrypt"]>(async () => ({ KeyId: arn, Plaintext: key() })),
  };
}

function provider(client: KmsGrantTransport = transport(), timeoutMs?: number) {
  return new KmsKeyProvider({ keyArn: arn, transport: client, timeoutMs });
}

async function failure(promise: Promise<unknown>) {
  const error: unknown = await promise.then(() => { throw new Error("unexpected success"); }, value => value);
  expect(error).toBeInstanceOf(GrantEnvelopeError);
  expect(error).toMatchObject({ code: "grant_envelope_invalid", message: "Grant envelope operation failed", name: "GrantEnvelopeError" });
  expect(Object.getOwnPropertyNames(error).sort()).toEqual(["code", "message", "name", "stack"]);
  expect(error).not.toHaveProperty("cause");
  return error as GrantEnvelopeError;
}

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("KmsKeyProvider", () => {
  it("requires an immutable full CMK ARN and rejects aliases", () => {
    const client = transport();
    for (const keyArn of ["", " ", arn + " ", " " + arn, arn + "\n", arn.replace("east", "east "),
      "alias/example", "12345678-1234-1234-1234-123456789abc", "mrk-" + "a".repeat(32),
      arn.replace(/key\/.+/, "alias/example"), arn.replace("123456789012", "1234"),
      arn.replace("kms:", "s3:"), arn.replace(/key\/.+/, "key/not-a-key"), null, 42]) {
      expect(() => new KmsKeyProvider({ keyArn: keyArn as string, transport: client })).toThrow(GrantEnvelopeError);
    }
    for (const keyArn of [arn, arn.replace(/key\/.+/, "key/mrk-" + "a".repeat(32)),
      arn.replace("arn:aws:", "arn:aws-us-gov:").replace("us-east-1", "us-gov-west-1")]) {
      expect(new KmsKeyProvider({ keyArn, transport: client }).keyId()).toBe(keyArn);
    }
    const options = { keyArn: arn, transport: client };
    const keys = new KmsKeyProvider(options);
    options.keyArn = otherArn;
    expect(keys.keyId()).toBe(arn);
    expect(client.encrypt).not.toHaveBeenCalled();
    expect(client.decrypt).not.toHaveBeenCalled();
  });

  it("returns the configured CMK ARN without transport calls", () => {
    const client = transport();
    expect(provider(client).keyId()).toBe(arn);
    expect(client.encrypt).not.toHaveBeenCalled();
    expect(client.decrypt).not.toHaveBeenCalled();
  });

  it("wraps exactly 32 bytes with pinned key algorithm and encryption context", async () => {
    const client = transport();
    const caller = key();
    let copied: Buffer | undefined;
    client.encrypt.mockImplementation(async request => {
      expect(request).toEqual({ KeyId: arn, EncryptionAlgorithm: "SYMMETRIC_DEFAULT", EncryptionContext: kmsContext, Plaintext: caller });
      expect(request.Plaintext).not.toBe(caller);
      copied = Buffer.from(request.Plaintext);
      return { KeyId: arn, CiphertextBlob: ciphertext };
    });
    const mutableContext = { ...context };
    const pending = provider(client).wrap(caller, mutableContext);
    caller.fill(99);
    mutableContext.tenantId = "changed";
    await pending;
    expect(copied).toEqual(key());
    expect(client.encrypt.mock.calls[0][0].EncryptionContext).toEqual(kmsContext);
    expect(client.encrypt.mock.calls[0][1].abortSignal).toBeInstanceOf(AbortSignal);
    expect(client.encrypt).toHaveBeenCalledTimes(1);
    expect(client.decrypt).not.toHaveBeenCalled();
  });

  it("encodes wrapped ciphertext as canonical unpadded base64url", async () => {
    const client = transport();
    for (const bytes of [ciphertext, Buffer.from([255]), Buffer.alloc(65536, 251)]) {
      client.encrypt.mockResolvedValueOnce({ KeyId: arn, CiphertextBlob: bytes });
      const encoded = await provider(client).wrap(key(), context);
      expect(encoded).toBe(bytes.toString("base64url"));
      expect(encoded).toMatch(/^[A-Za-z0-9_-]+$/);
      expect(Buffer.from(encoded, "base64url")).toEqual(bytes);
    }
  });

  it("unwraps with the pinned key algorithm and identical encryption context", async () => {
    const client = transport();
    const keys = provider(client);
    await keys.wrap(key(), context);
    expect(await keys.unwrap(wrapped, context)).toEqual(key());
    expect(client.decrypt.mock.calls[0][0]).toEqual({ KeyId: arn, EncryptionAlgorithm: "SYMMETRIC_DEFAULT", EncryptionContext: kmsContext, CiphertextBlob: ciphertext });
    expect(client.decrypt.mock.calls[0][0].EncryptionContext).toEqual(client.encrypt.mock.calls[0][0].EncryptionContext);
    expect(client.decrypt.mock.calls[0][1].abortSignal).toBeInstanceOf(AbortSignal);
    expect(client.decrypt).toHaveBeenCalledTimes(1);
  });

  it("rejects invalid context before calling the transport", async () => {
    const client = transport();
    const keys = provider(client);
    const invalid: unknown[] = [null, undefined, {}, [], "context"];
    for (const field of ["tenantId", "grantId"]) {
      for (const value of ["", "x".repeat(257), "a|b", 42, null, undefined]) invalid.push({ ...context, [field]: value });
    }
    for (const ctx of invalid) {
      await failure(keys.wrap(key(), ctx as EnvelopeContext));
      await failure(keys.unwrap(wrapped, ctx as EnvelopeContext));
    }
    expect(client.encrypt).not.toHaveBeenCalled();
    expect(client.decrypt).not.toHaveBeenCalled();
  });

  it("rejects invalid data key lengths before calling the transport", async () => {
    const client = transport();
    const keys = provider(client);
    for (const input of [Buffer.alloc(0), Buffer.alloc(16), Buffer.alloc(31), Buffer.alloc(33), Buffer.alloc(65536),
      new Uint8Array(32), null, "x".repeat(32)]) await failure(keys.wrap(input as Buffer, context));
    expect(client.encrypt).not.toHaveBeenCalled();
  });

  it("rejects empty malformed noncanonical and oversized wrapped keys before decrypt", async () => {
    const client = transport();
    const keys = provider(client);
    for (const input of ["", "!", "A", "AB", "Zg==", "Z g", "+/8", "_x", null, 42,
      Buffer.alloc(65537).toString("base64url")]) await failure(keys.unwrap(input as string, context));
    expect(client.decrypt).not.toHaveBeenCalled();
    client.decrypt.mockResolvedValueOnce({ KeyId: arn, Plaintext: key() });
    await keys.unwrap(Buffer.alloc(65536).toString("base64url"), context);
    expect(client.decrypt).toHaveBeenCalledTimes(1);
  });

  it("rejects missing empty oversized or wrong-key encrypt responses", async () => {
    const client = transport();
    for (const response of [undefined, null, false, "response", [], {}, { KeyId: arn },
      { CiphertextBlob: ciphertext }, { KeyId: arn, CiphertextBlob: new Uint8Array() },
      { KeyId: arn, CiphertextBlob: Buffer.alloc(65537) }, { KeyId: otherArn, CiphertextBlob: ciphertext },
      { KeyId: arn, CiphertextBlob: [1, 2] }, { KeyId: arn, CiphertextBlob: "synthetic-ciphertext" }]) {
      client.encrypt.mockResolvedValueOnce(response as Awaited<ReturnType<KmsGrantTransport["encrypt"]>>);
      await failure(provider(client).wrap(key(), context));
    }
  });

  it("rejects missing wrong-length or wrong-key decrypt responses", async () => {
    const client = transport();
    for (const response of [undefined, null, 42, "response", [], {}, { KeyId: arn }, { Plaintext: key() },
      ...[0, 16, 31, 33, 65536].map(size => ({ KeyId: arn, Plaintext: Buffer.alloc(size, 37) })),
      { KeyId: otherArn, Plaintext: key() }, { KeyId: arn, Plaintext: Array(32).fill(37) },
      { KeyId: arn, Plaintext: "x".repeat(32) }]) {
      client.decrypt.mockResolvedValueOnce(response as Awaited<ReturnType<KmsGrantTransport["decrypt"]>>);
      await failure(provider(client).unwrap(wrapped, context));
      if (response && typeof response === "object" && "Plaintext" in response && response.Plaintext instanceof Uint8Array) {
        expect(response.Plaintext).toEqual(Buffer.alloc(response.Plaintext.length));
      }
    }
  });

  it("redacts transport errors to GrantEnvelopeError without a cause", async () => {
    const client = transport();
    const marker = "arbitrary-synthetic-transport-detail-834";
    const original = Object.assign(new Error(marker), { request: marker, response: marker, code: marker });
    for (const synchronous of [true, false]) {
      const fail = () => { if (synchronous) throw original; return Promise.reject(original); };
      client.encrypt.mockImplementation(fail);
      client.decrypt.mockImplementation(fail);
      for (const pending of [provider(client).wrap(key(), context), provider(client).unwrap(wrapped, context)]) {
        const error = await failure(pending);
        expect(error).not.toBe(original);
        expect(JSON.stringify(error)).not.toContain(marker);
        expect(String(error.stack)).not.toContain(marker);
      }
    }
  });

  it("clears owned wrapping buffers without mutating the caller key", async () => {
    const client = transport();
    for (const mode of ["success", "throw", "reject", "invalid"]) {
      let owned: Uint8Array | undefined;
      client.encrypt.mockImplementation(request => {
        owned = request.Plaintext;
        expect(owned).toEqual(key());
        if (mode === "throw") throw new Error("synthetic failure");
        if (mode === "reject") return Promise.reject(new Error("synthetic failure"));
        return Promise.resolve(mode === "invalid" ? {} : { KeyId: arn, CiphertextBlob: ciphertext });
      });
      const caller = key();
      const pending = provider(client).wrap(caller, context);
      if (mode === "success") await pending; else await failure(pending);
      expect(owned).toEqual(Buffer.alloc(32));
      expect(caller).toEqual(key());
      expect(owned).not.toBe(caller);
    }
    let resolve!: (response: Awaited<ReturnType<KmsGrantTransport["encrypt"]>>) => void;
    client.encrypt.mockImplementation(() => new Promise(done => { resolve = done; }));
    const caller = key();
    const pending = provider(client).wrap(caller, context);
    const owned = client.encrypt.mock.calls.at(-1)![0].Plaintext;
    caller.fill(98);
    expect(owned).toEqual(key());
    resolve({ KeyId: arn, CiphertextBlob: ciphertext });
    await pending;
    expect(owned).toEqual(Buffer.alloc(32));
    expect(caller).toEqual(Buffer.alloc(32, 98));
  });

  it("copies unwrapped keys and clears transport plaintext", async () => {
    const client = transport();
    for (const plaintext of [key(), new Uint8Array(key())]) {
      client.decrypt.mockResolvedValueOnce({ KeyId: arn, Plaintext: plaintext });
      const result = await provider(client).unwrap(wrapped, context);
      expect(Buffer.isBuffer(result)).toBe(true);
      expect(result).toEqual(key());
      expect(result).not.toBe(plaintext);
      expect(plaintext).toEqual(plaintext instanceof Buffer ? Buffer.alloc(32) : new Uint8Array(32));
      plaintext.fill(99);
      expect(result).toEqual(key());
    }
  });

  it("aborts timed out operations and never accepts a late success", async () => {
    vi.useFakeTimers();
    for (const operation of ["encrypt", "decrypt"] as const) {
      const client = transport();
      let resolve!: (response: { KeyId: string; CiphertextBlob: Buffer; Plaintext: Buffer }) => void;
      const delayed = new Promise<{ KeyId: string; CiphertextBlob: Buffer; Plaintext: Buffer }>(done => { resolve = done; });
      client.encrypt.mockImplementation(() => delayed);
      client.decrypt.mockImplementation(() => delayed);
      const keys = provider(client);
      const caller = key();
      const pending = operation === "encrypt" ? keys.wrap(caller, context) : keys.unwrap(wrapped, context);
      const success = vi.fn();
      const observed = pending.then(success, error => error);
      await vi.advanceTimersByTimeAsync(1499);
      const options = client[operation].mock.calls[0][1];
      expect(options.abortSignal.aborted).toBe(false);
      expect(success).not.toHaveBeenCalled();
      await vi.advanceTimersByTimeAsync(1);
      const error = await observed;
      await failure(Promise.reject(error));
      expect(options.abortSignal.aborted).toBe(true);
      expect(caller).toEqual(key());
      if (operation === "encrypt") expect(client.encrypt.mock.calls[0][0].Plaintext).toEqual(Buffer.alloc(32));
      resolve({ KeyId: arn, CiphertextBlob: ciphertext, Plaintext: key() });
      await vi.advanceTimersByTimeAsync(0);
      expect(success).not.toHaveBeenCalled();
      expect(await observed).toBe(error);
      expect(vi.getTimerCount()).toBe(0);
    }
  });

  it("clears late decrypt plaintext after timeout", async () => {
    vi.useFakeTimers();
    const client = transport();
    let resolve!: (response: Awaited<ReturnType<KmsGrantTransport["decrypt"]>>) => void;
    client.decrypt.mockImplementation(() => new Promise(done => { resolve = done; }));
    const pending = failure(provider(client, 7).unwrap(wrapped, context));
    await vi.advanceTimersByTimeAsync(7);
    await pending;
    const plaintext = key();
    resolve({ KeyId: otherArn, Plaintext: plaintext });
    await vi.advanceTimersByTimeAsync(0);
    expect(plaintext).toEqual(Buffer.alloc(32));
    expect(client.decrypt.mock.calls[0][1].abortSignal.aborted).toBe(true);
  });

  it("rejects invalid timeout configuration", () => {
    const client = transport();
    for (const timeoutMs of [0, -1, 1.5, 30001, NaN, Infinity, null, "1500"]) {
      expect(() => provider(client, timeoutMs as number)).toThrow(GrantEnvelopeError);
    }
    for (const timeoutMs of [undefined, 1, 1500, 30000]) expect(provider(client, timeoutMs).keyId()).toBe(arn);
    expect(client.encrypt).not.toHaveBeenCalled();
    expect(client.decrypt).not.toHaveBeenCalled();
  });

  it("round trips sealGrant and openGrant through a fake KMS transport", async () => {
    const records = new Map<string, { keyId: string; context: string; plaintext: Buffer }>();
    const client: KmsGrantTransport = {
      encrypt: async request => {
        const bytes = Buffer.from(`synthetic-wrapped-key-${records.size}`);
        records.set(bytes.toString("hex"), { keyId: request.KeyId, context: JSON.stringify(request.EncryptionContext), plaintext: Buffer.from(request.Plaintext) });
        return { KeyId: request.KeyId, CiphertextBlob: bytes };
      },
      decrypt: async request => {
        const record = records.get(Buffer.from(request.CiphertextBlob).toString("hex"));
        if (!record || request.KeyId !== record.keyId || JSON.stringify(request.EncryptionContext) !== record.context) {
          throw new Error("synthetic context or key mismatch");
        }
        return { KeyId: record.keyId, Plaintext: Buffer.from(record.plaintext) };
      },
    };
    const keys = provider(client);
    for (const input of ["synthetic-grant-value", Buffer.from([0, 255, 128]), "", Buffer.alloc(65536, 37)]) {
      const sealed = JSON.parse(JSON.stringify(await sealGrant(input, context, keys)));
      expect(await openGrant(sealed, context, keys)).toEqual(typeof input === "string" ? Buffer.from(input) : input);
      for (const ctx of [{ ...context, tenantId: "other-tenant" }, { ...context, grantId: "other-grant" }]) {
        await failure(keys.unwrap(sealed.wk, ctx));
        await failure(openGrant({ ...sealed, tenant: ctx.tenantId, grant: ctx.grantId }, ctx, keys));
      }
      await failure(new KmsKeyProvider({ keyArn: otherArn, transport: client }).unwrap(sealed.wk, context));
    }
  });

  it("does not log plaintext keys ciphertext or transport error contents", async () => {
    const logs = ["log", "info", "warn", "error", "debug", "trace", "dir"] as const;
    const spies = logs.map(method => vi.spyOn(console, method).mockImplementation(() => {}));
    const marker = "ordinary-synthetic-marker-7492";
    const caller = Buffer.from("synthetic-key-marker-32-bytes!!!");
    expect(caller.length).toBe(32);
    const client = transport();
    const cipher = Buffer.from("synthetic-ciphertext-marker");
    client.encrypt.mockResolvedValueOnce({ KeyId: arn, CiphertextBlob: cipher });
    const keys = provider(client);
    const encoded = await keys.wrap(caller, context);
    client.decrypt.mockResolvedValueOnce({ KeyId: arn, Plaintext: Buffer.from(caller) });
    expect(await keys.unwrap(encoded, context)).toEqual(caller);
    client.encrypt.mockRejectedValueOnce(Object.assign(new Error(marker), { request: caller, response: cipher }));
    client.decrypt.mockRejectedValueOnce(marker);
    for (const pending of [keys.wrap(caller, context), keys.unwrap(encoded, context)]) {
      const error = await failure(pending);
      const fields = Object.getOwnPropertyNames(error).map(name => String((error as unknown as Record<string, unknown>)[name])).join(" ");
      for (const secret of [marker, caller.toString(), caller.toString("hex"), caller.toString("base64url"), cipher.toString(), encoded]) {
        expect(fields).not.toContain(secret);
      }
    }
    for (const spy of spies) expect(spy).not.toHaveBeenCalled();
  });
});
