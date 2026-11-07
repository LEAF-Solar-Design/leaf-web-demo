import { DecryptCommand, EncryptCommand, KMSClient } from "@aws-sdk/client-kms";
import { GrantEnvelopeError } from "./keyProvider.js";
import type { KmsGrantTransport } from "./kmsKeyProvider.js";

/** The only part of KMSClient this transport uses; tests inject a fake. */
export interface KmsSendClient {
  send(command: EncryptCommand | DecryptCommand, options: { abortSignal: AbortSignal }): Promise<unknown>;
}

export interface KmsGrantTransportOptions {
  region: string;
  client?: KmsSendClient;
}

/** Client config for the real KMSClient: region only. */
export function kmsClientConfig(region: string): { region: string } {
  return { region };
}

export function createKmsGrantTransport(options: KmsGrantTransportOptions): KmsGrantTransport {
  let client: KmsSendClient;
  try {
    if (typeof options.region !== "string" || /\s/.test(options.region)
      || !/^[a-z]{2}(?:-[a-z0-9]+)+-\d+$/.test(options.region)
      || (options.client !== undefined && (!options.client || typeof options.client.send !== "function"))) {
      throw new GrantEnvelopeError();
    }
    client = options.client === undefined ? new KMSClient(kmsClientConfig(options.region)) : options.client;
  } catch {
    throw new GrantEnvelopeError();
  }

  return {
    async encrypt(request, { abortSignal }) {
      try {
        const output = await client.send(new EncryptCommand({
          KeyId: request.KeyId,
          EncryptionAlgorithm: request.EncryptionAlgorithm,
          EncryptionContext: { ...request.EncryptionContext },
          Plaintext: request.Plaintext,
        }), { abortSignal });
        if (output === null || typeof output !== "object" || Array.isArray(output)) throw new GrantEnvelopeError();
        const response = output as { KeyId?: string; CiphertextBlob?: Uint8Array };
        return { KeyId: response.KeyId, CiphertextBlob: response.CiphertextBlob };
      } catch {
        throw new GrantEnvelopeError();
      }
    },
    async decrypt(request, { abortSignal }) {
      try {
        const output = await client.send(new DecryptCommand({
          KeyId: request.KeyId,
          EncryptionAlgorithm: request.EncryptionAlgorithm,
          EncryptionContext: { ...request.EncryptionContext },
          CiphertextBlob: request.CiphertextBlob,
        }), { abortSignal });
        if (output === null || typeof output !== "object" || Array.isArray(output)) throw new GrantEnvelopeError();
        const response = output as { KeyId?: string; Plaintext?: Uint8Array };
        return { KeyId: response.KeyId, Plaintext: response.Plaintext };
      } catch {
        throw new GrantEnvelopeError();
      }
    },
  };
}
