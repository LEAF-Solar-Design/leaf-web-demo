import { createCipheriv, createDecipheriv, randomBytes } from "node:crypto";

export interface EnvelopeContext {
  tenantId: string;
  grantId: string;
}

/** Base64url encoding of the provider's opaque wrapped data key. */
export type WrappedKey = string;

export interface KeyProvider {
  keyId(): string;
  wrap(dataKey: Buffer, context: EnvelopeContext): Promise<WrappedKey>;
  unwrap(wrapped: WrappedKey, context: EnvelopeContext): Promise<Buffer>;
}

export class GrantEnvelopeError extends Error {
  readonly code = "grant_envelope_invalid";

  constructor() {
    super("Grant envelope operation failed");
    this.name = "GrantEnvelopeError";
  }
}

export function contextAad(context: EnvelopeContext): Buffer {
  if (!context || [context.tenantId, context.grantId].some(value =>
    typeof value !== "string" || value.length === 0 || value.length > 256 || value.includes("|"))) {
    throw new GrantEnvelopeError();
  }
  return Buffer.from(`leaf.grant-envelope.v1|${context.tenantId}|${context.grantId}`, "utf8");
}

/** Buffer.from is permissive; require an unpadded, canonical base64url value. */
export function decodeBase64url(value: unknown, maxBytes: number): Buffer {
  if (typeof value !== "string" || value.length > Math.ceil(maxBytes * 4 / 3)
    || !/^[A-Za-z0-9_-]*$/.test(value)) throw new GrantEnvelopeError();
  const decoded = Buffer.from(value, "base64url");
  if (decoded.length > maxBytes || decoded.toString("base64url") !== value) {
    throw new GrantEnvelopeError();
  }
  return decoded;
}

// Activation requires a KMS KeyProvider, IAM permissions, and a grant-store migration.
export class LocalKeyProvider implements KeyProvider {
  private readonly kek: Buffer;
  private readonly id: string;

  constructor(key: Buffer, keyId = "local-v1") {
    if (!Buffer.isBuffer(key) || key.length !== 32 || typeof keyId !== "string" || !keyId) {
      throw new GrantEnvelopeError();
    }
    this.kek = Buffer.from(key);
    this.id = keyId;
  }

  keyId(): string {
    return this.id;
  }

  async wrap(dataKey: Buffer, context: EnvelopeContext): Promise<WrappedKey> {
    try {
      const aad = contextAad(context);
      if (!Buffer.isBuffer(dataKey) || dataKey.length !== 32) throw new GrantEnvelopeError();
      const iv = randomBytes(12);
      const cipher = createCipheriv("aes-256-gcm", this.kek, iv);
      cipher.setAAD(aad);
      const ct = Buffer.concat([cipher.update(dataKey), cipher.final()]);
      return Buffer.concat([iv, cipher.getAuthTag(), ct]).toString("base64url");
    } catch {
      throw new GrantEnvelopeError();
    }
  }

  async unwrap(wrapped: WrappedKey, context: EnvelopeContext): Promise<Buffer> {
    let pending: Buffer | undefined;
    try {
      const aad = contextAad(context);
      const bytes = decodeBase64url(wrapped, 60);
      if (bytes.length !== 60) throw new GrantEnvelopeError();
      const decipher = createDecipheriv("aes-256-gcm", this.kek, bytes.subarray(0, 12));
      decipher.setAAD(aad);
      decipher.setAuthTag(bytes.subarray(12, 28));
      pending = decipher.update(bytes.subarray(28));
      return Buffer.concat([pending, decipher.final()]);
    } catch {
      throw new GrantEnvelopeError();
    } finally {
      pending?.fill(0);
    }
  }
}
