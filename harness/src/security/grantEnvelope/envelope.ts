import { createCipheriv, createDecipheriv, randomBytes } from "node:crypto";
import { contextAad, decodeBase64url, GrantEnvelopeError } from "./keyProvider.js";
import type { EnvelopeContext, KeyProvider, WrappedKey } from "./keyProvider.js";

export { GrantEnvelopeError } from "./keyProvider.js";
export type { EnvelopeContext } from "./keyProvider.js";

const MAX_PLAINTEXT_BYTES = 64 * 1024;

export interface SealedGrant {
  v: 1;
  alg: "A256GCM";
  kid: string;
  tenant: string;
  grant: string;
  iv: string;
  ct: string;
  tag: string;
  wk: WrappedKey;
}

export async function sealGrant(
  plaintext: Buffer | string,
  context: EnvelopeContext,
  keys: KeyProvider,
): Promise<SealedGrant> {
  let dataKey: Buffer | undefined;
  try {
    const aad = contextAad(context);
    const boundContext = { tenantId: context.tenantId, grantId: context.grantId };
    if (typeof plaintext !== "string" && !Buffer.isBuffer(plaintext)) throw new GrantEnvelopeError();
    const size = typeof plaintext === "string" ? Buffer.byteLength(plaintext, "utf8") : plaintext.length;
    if (size > MAX_PLAINTEXT_BYTES) throw new GrantEnvelopeError();
    const kid = keys.keyId();
    if (typeof kid !== "string" || !kid) throw new GrantEnvelopeError();
    dataKey = randomBytes(32);
    const iv = randomBytes(12);
    const cipher = createCipheriv("aes-256-gcm", dataKey, iv);
    cipher.setAAD(aad);
    const input = typeof plaintext === "string" ? Buffer.from(plaintext, "utf8") : plaintext;
    const ct = Buffer.concat([cipher.update(input), cipher.final()]);
    const wk = await keys.wrap(dataKey, { ...boundContext });
    decodeBase64url(wk, MAX_PLAINTEXT_BYTES);
    if (!wk) throw new GrantEnvelopeError();
    return {
      v: 1, alg: "A256GCM", kid, tenant: boundContext.tenantId, grant: boundContext.grantId,
      iv: iv.toString("base64url"), ct: ct.toString("base64url"),
      tag: cipher.getAuthTag().toString("base64url"), wk,
    };
  } catch {
    throw new GrantEnvelopeError();
  } finally {
    dataKey?.fill(0);
  }
}

export async function openGrant(
  sealed: unknown,
  context: EnvelopeContext,
  keys: KeyProvider,
): Promise<Buffer> {
  let dataKey: Buffer | undefined;
  let pending: Buffer | undefined;
  try {
    const aad = contextAad(context);
    const boundContext = { tenantId: context.tenantId, grantId: context.grantId };
    if (sealed === null || typeof sealed !== "object" || Array.isArray(sealed)) {
      throw new GrantEnvelopeError();
    }
    const value = sealed as Record<string, unknown>;
    const fields = ["v", "alg", "kid", "tenant", "grant", "iv", "ct", "tag", "wk"];
    if (fields.some(field => !Object.prototype.hasOwnProperty.call(value, field))
      || value.v !== 1 || value.alg !== "A256GCM"
      || typeof value.kid !== "string" || !value.kid || value.kid !== keys.keyId()
      || value.tenant !== boundContext.tenantId || value.grant !== boundContext.grantId) {
      throw new GrantEnvelopeError();
    }
    const iv = decodeBase64url(value.iv, 12);
    const tag = decodeBase64url(value.tag, 16);
    const ct = decodeBase64url(value.ct, MAX_PLAINTEXT_BYTES);
    const wk = value.wk;
    if (typeof wk !== "string" || !wk) throw new GrantEnvelopeError();
    decodeBase64url(wk, MAX_PLAINTEXT_BYTES);
    if (iv.length !== 12 || tag.length !== 16) throw new GrantEnvelopeError();
    dataKey = await keys.unwrap(wk, boundContext);
    if (!Buffer.isBuffer(dataKey) || dataKey.length !== 32) throw new GrantEnvelopeError();
    const decipher = createDecipheriv("aes-256-gcm", dataKey, iv);
    decipher.setAAD(aad);
    decipher.setAuthTag(tag);
    pending = decipher.update(ct);
    return Buffer.concat([pending, decipher.final()]);
  } catch {
    throw new GrantEnvelopeError();
  } finally {
    if (Buffer.isBuffer(dataKey)) dataKey.fill(0);
    pending?.fill(0);
  }
}
