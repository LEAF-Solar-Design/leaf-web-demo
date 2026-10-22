import { contextAad, decodeBase64url, GrantEnvelopeError } from "./keyProvider.js";
import type { EnvelopeContext, KeyProvider, WrappedKey } from "./keyProvider.js";

export type KmsEncryptionContext = Readonly<{
  purpose: "leaf.grant-envelope.v1";
  tenant: string;
  grant: string;
}>;

export interface KmsGrantTransport {
  encrypt(request: {
    KeyId: string;
    EncryptionAlgorithm: "SYMMETRIC_DEFAULT";
    EncryptionContext: KmsEncryptionContext;
    Plaintext: Uint8Array;
  }, options: { abortSignal: AbortSignal }):
    Promise<{ KeyId?: string; CiphertextBlob?: Uint8Array }>;
  decrypt(request: {
    KeyId: string;
    EncryptionAlgorithm: "SYMMETRIC_DEFAULT";
    EncryptionContext: KmsEncryptionContext;
    CiphertextBlob: Uint8Array;
  }, options: { abortSignal: AbortSignal }):
    Promise<{ KeyId?: string; Plaintext?: Uint8Array }>;
}

export interface KmsKeyProviderOptions {
  keyArn: string;
  transport: KmsGrantTransport;
  timeoutMs?: number;
}

const MAX_WRAPPED_BYTES = 64 * 1024;
const KEY_ARN = /^arn:aws(?:-cn|-us-gov|-iso|-iso-b|-iso-e|-iso-f|-eusc)?:kms:[a-z]{2}(?:-[a-z0-9]+)+-\d+:\d{12}:key\/(?:[a-fA-F0-9]{8}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{12}|mrk-[a-fA-F0-9]{32})$/;

function encryptionContext(context: EnvelopeContext): KmsEncryptionContext {
  const bound = { tenantId: context?.tenantId, grantId: context?.grantId };
  contextAad(bound);
  return Object.freeze({ purpose: "leaf.grant-envelope.v1", tenant: bound.tenantId, grant: bound.grantId });
}

function clearPlaintext(response: unknown): void {
  try {
    if (response !== null && typeof response === "object" && "Plaintext" in response) {
      const plaintext = response.Plaintext;
      if (plaintext instanceof Uint8Array) plaintext.fill(0);
    }
  } catch {
    // Cleanup must never expose a transport error or reject a late-result handler.
  }
}

/** Injected transport only. This adapter performs no client or credential discovery. */
export class KmsKeyProvider implements KeyProvider {
  private readonly arn: string;
  private readonly transport: KmsGrantTransport;
  private readonly timeoutMs: number;

  constructor(options: KmsKeyProviderOptions) {
    try {
      const timeoutMs = options.timeoutMs === undefined ? 1500 : options.timeoutMs;
      if (typeof options.keyArn !== "string" || /\s/.test(options.keyArn) || !KEY_ARN.test(options.keyArn)
        || !Number.isInteger(timeoutMs) || timeoutMs < 1 || timeoutMs > 30000
        || !options.transport || typeof options.transport.encrypt !== "function"
        || typeof options.transport.decrypt !== "function") throw new GrantEnvelopeError();
      this.arn = options.keyArn;
      this.transport = options.transport;
      this.timeoutMs = timeoutMs;
    } catch {
      throw new GrantEnvelopeError();
    }
  }

  keyId(): string {
    return this.arn;
  }

  private operation<T, R>(
    invoke: (signal: AbortSignal) => Promise<T>,
    accept: (response: T) => R,
    clearOwned: () => void = () => {},
    clearResponse: (response: T) => void = () => {},
  ): Promise<R> {
    return new Promise((resolve, reject) => {
      const controller = new AbortController();
      let settled = false;
      const fail = () => reject(new GrantEnvelopeError());
      const timer = setTimeout(() => {
        settled = true;
        clearOwned();
        fail();
        try { controller.abort(); } catch { /* Public failure is already settled. */ }
      }, this.timeoutMs);
      const finish = () => {
        settled = true;
        clearTimeout(timer);
        clearOwned();
      };
      try {
        Promise.resolve(invoke(controller.signal)).then(response => {
          try {
            if (!settled) resolve(accept(response));
          } catch {
            if (!settled) fail();
          } finally {
            clearResponse(response);
            finish();
          }
        }, () => {
          if (!settled) fail();
          finish();
        });
      } catch {
        fail();
        finish();
      }
    });
  }

  async wrap(dataKey: Buffer, context: EnvelopeContext): Promise<WrappedKey> {
    try {
      const EncryptionContext = encryptionContext(context);
      if (!Buffer.isBuffer(dataKey) || dataKey.length !== 32) throw new GrantEnvelopeError();
      const owned = Buffer.from(dataKey);
      return await this.operation(signal => this.transport.encrypt({
        KeyId: this.arn, EncryptionAlgorithm: "SYMMETRIC_DEFAULT", EncryptionContext, Plaintext: owned,
      }, { abortSignal: signal }), response => {
        if (!response || typeof response !== "object" || Array.isArray(response)
          || response.KeyId !== this.arn || !(response.CiphertextBlob instanceof Uint8Array)
          || response.CiphertextBlob.length === 0 || response.CiphertextBlob.length > MAX_WRAPPED_BYTES) {
          throw new GrantEnvelopeError();
        }
        return Buffer.from(response.CiphertextBlob).toString("base64url");
      }, () => { owned.fill(0); });
    } catch {
      throw new GrantEnvelopeError();
    }
  }

  async unwrap(wrapped: WrappedKey, context: EnvelopeContext): Promise<Buffer> {
    try {
      const EncryptionContext = encryptionContext(context);
      const bytes = decodeBase64url(wrapped, MAX_WRAPPED_BYTES);
      if (bytes.length === 0) throw new GrantEnvelopeError();
      return await this.operation(signal => this.transport.decrypt({
        KeyId: this.arn, EncryptionAlgorithm: "SYMMETRIC_DEFAULT", EncryptionContext, CiphertextBlob: bytes,
      }, { abortSignal: signal }), response => {
        if (!response || typeof response !== "object" || Array.isArray(response)
          || response.KeyId !== this.arn || !(response.Plaintext instanceof Uint8Array)
          || response.Plaintext.length !== 32) throw new GrantEnvelopeError();
        return Buffer.from(response.Plaintext);
      }, undefined, clearPlaintext);
    } catch {
      throw new GrantEnvelopeError();
    }
  }
}
