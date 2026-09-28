import { randomBytes } from "node:crypto";
import { describe, expect, it } from "vitest";
import { GrantEnvelopeError, LocalKeyProvider, openGrant, sealGrant } from "../src/security/grantEnvelope/index.js";
import type { KeyProvider } from "../src/security/grantEnvelope/index.js";

const context = { tenantId: "tenant-a", grantId: "grant-a" };
const plaintext = "secret-token:grant-access/never-persist-this-cleartext";
const provider = () => new LocalKeyProvider(randomBytes(32), "test-key");

function tamper(value: string): string {
  const bytes = Buffer.from(value, "base64url");
  bytes[0] ^= 1;
  return bytes.toString("base64url");
}

describe("grant envelope", () => {
  it("round trips strings, binary, empty input, and the maximum size through JSON", async () => {
    const keys = provider();
    for (const input of [plaintext, Buffer.from([0, 255, 128]), "", Buffer.alloc(65536, 42)]) {
      const sealed = await sealGrant(input, context, keys);
      const expected = typeof input === "string" ? Buffer.from(input, "utf8") : input;
      expect(await openGrant(JSON.parse(JSON.stringify(sealed)), context, keys)).toEqual(expected);
    }
  });

  it("uses fresh IVs and data keys and clears the data keys", async () => {
    const local = provider();
    const copies: Buffer[] = [];
    const refs: Buffer[] = [];
    const keys: KeyProvider = {
      keyId: () => local.keyId(),
      wrap: async (key, ctx) => {
        refs.push(key);
        copies.push(Buffer.from(key));
        return local.wrap(key, ctx);
      },
      unwrap: (wrapped, ctx) => local.unwrap(wrapped, ctx),
    };
    const a = await sealGrant(plaintext, context, keys);
    const b = await sealGrant(plaintext, context, keys);
    expect(a.iv).not.toBe(b.iv);
    expect(a.ct).not.toBe(b.ct);
    expect(copies[0]).not.toEqual(copies[1]);
    expect(refs.every(key => key.equals(Buffer.alloc(32)))).toBe(true);
    const serialized = JSON.stringify(a);
    expect(serialized).not.toContain(plaintext);
    for (let i = 0; i <= plaintext.length - 8; i++) {
      expect(serialized).not.toContain(plaintext.slice(i, i + 8));
    }
    for (const key of copies) expect(serialized).not.toContain(key.toString("base64url"));
  });

  it.each(["ct", "tag", "iv", "wk"] as const)("rejects tampered %s", async field => {
    const keys = provider();
    const sealed = await sealGrant(plaintext, context, keys);
    await expect(openGrant({ ...sealed, [field]: tamper(sealed[field]) }, context, keys))
      .rejects.toBeInstanceOf(GrantEnvelopeError);
  });

  it("rejects wrong tenant, grant, key id, version, algorithm, and wrapping key", async () => {
    const keys = provider();
    const sealed = await sealGrant(plaintext, context, keys);
    for (const ctx of [{ ...context, tenantId: "other" }, { ...context, grantId: "other" }]) {
      await expect(openGrant(sealed, ctx, keys)).rejects.toBeInstanceOf(GrantEnvelopeError);
    }
    for (const patch of [{ tenant: "other" }, { grant: "other" }, { kid: "other" }, { v: 2 }, { alg: "none" }]) {
      await expect(openGrant({ ...sealed, ...patch }, context, keys)).rejects.toBeInstanceOf(GrantEnvelopeError);
    }
    await expect(openGrant(sealed, context, provider())).rejects.toBeInstanceOf(GrantEnvelopeError);
    await expect(keys.unwrap(sealed.wk, { ...context, tenantId: "other" }))
      .rejects.toBeInstanceOf(GrantEnvelopeError);
  });

  it.each([0, 16, 31, 33, 64])("refuses a %i-byte wrapping key", size => {
    expect(() => new LocalKeyProvider(Buffer.alloc(size))).toThrow(GrantEnvelopeError);
  });

  it("refuses oversized plaintext and ciphertext", async () => {
    const keys = provider();
    for (const input of [Buffer.alloc(65537), "é".repeat(32769)]) {
      await expect(sealGrant(input, context, keys)).rejects.toBeInstanceOf(GrantEnvelopeError);
    }
    const sealed = await sealGrant(plaintext, context, keys);
    await expect(openGrant({ ...sealed, ct: Buffer.alloc(65537).toString("base64url") }, context, keys))
      .rejects.toBeInstanceOf(GrantEnvelopeError);
  });

  it("rejects malformed envelopes with a stable typed error", async () => {
    const keys = provider();
    const sealed = await sealGrant(plaintext, context, keys);
    const malformed: unknown[] = [null, [], {}, "text", 1];
    for (const field of Object.keys(sealed)) {
      const missing: Record<string, unknown> = { ...sealed };
      delete missing[field];
      malformed.push(missing);
    }
    for (const field of ["iv", "tag", "ct", "wk"]) {
      for (const bad of ["!", "A", "AB", "Zg==", "Z g", 42, null]) {
        malformed.push({ ...sealed, [field]: bad });
      }
    }
    malformed.push({ ...sealed, iv: "" }, { ...sealed, tag: "" }, { ...sealed, wk: "" });
    for (const value of malformed) {
      await expect(openGrant(value, context, keys)).rejects.toBeInstanceOf(GrantEnvelopeError);
      await expect(openGrant(value, context, keys)).rejects.toHaveProperty("code", "grant_envelope_invalid");
    }
  });

  it("validates both context identifiers", async () => {
    const keys = provider();
    const sealed = await sealGrant(plaintext, context, keys);
    for (const field of ["tenantId", "grantId"]) {
      for (const value of ["", "x".repeat(257), "a|b"]) {
        const invalid = { ...context, [field]: value };
        await expect(sealGrant(plaintext, invalid, keys)).rejects.toBeInstanceOf(GrantEnvelopeError);
        await expect(openGrant(sealed, invalid, keys)).rejects.toBeInstanceOf(GrantEnvelopeError);
      }
    }
  });

  it("clears the data key even when wrapping fails", async () => {
    let retained: Buffer | undefined;
    const keys: KeyProvider = {
      keyId: () => "failure",
      wrap: async key => { retained = key; throw new Error("provider failure"); },
      unwrap: async () => { throw new Error("provider failure"); },
    };
    await expect(sealGrant(plaintext, context, keys)).rejects.toBeInstanceOf(GrantEnvelopeError);
    expect(retained).toEqual(Buffer.alloc(32));
  });

  it("clears unwrapped keys after successful and failed authentication", async () => {
    const local = provider();
    const sealed = await sealGrant(plaintext, context, local);
    let retained: Buffer | undefined;
    const keys: KeyProvider = {
      keyId: () => local.keyId(),
      wrap: (key, ctx) => local.wrap(key, ctx),
      unwrap: async (wrapped, ctx) => {
        retained = await local.unwrap(wrapped, ctx);
        return retained;
      },
    };
    await openGrant(sealed, context, keys);
    expect(retained).toEqual(Buffer.alloc(32));
    await expect(openGrant({ ...sealed, tag: tamper(sealed.tag) }, context, keys))
      .rejects.toBeInstanceOf(GrantEnvelopeError);
    expect(retained).toEqual(Buffer.alloc(32));
  });

  it("rejects relabeling both the envelope and the supplied context", async () => {
    const keys = provider();
    const sealed = await sealGrant(plaintext, context, keys);
    await expect(openGrant({ ...sealed, tenant: "other" }, { ...context, tenantId: "other" }, keys))
      .rejects.toBeInstanceOf(GrantEnvelopeError);
    await expect(openGrant({ ...sealed, grant: "other" }, { ...context, grantId: "other" }, keys))
      .rejects.toBeInstanceOf(GrantEnvelopeError);
  });
});
