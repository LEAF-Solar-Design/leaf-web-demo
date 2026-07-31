import { describe, expect, it, vi } from "vitest";

import {
  createEnabledProductionInstantExecutorClient,
  createProductionInstantExecutorClient,
} from "../src/server.js";

const requiredEnv = {
  LEAF_INSTANT_EXECUTOR_CA_FILE: "C:/run/secrets/executor-ca.pem",
  LEAF_INSTANT_EXECUTOR_CERT_FILE: "C:/run/secrets/executor-client-cert.pem",
  LEAF_INSTANT_EXECUTOR_KEY_FILE: "C:/run/secrets/executor-client-key.pem",
  LEAF_INSTANT_EXECUTOR_TLS_SERVER_NAME: "executor.instant.internal",
};

describe("production instant executor composition", () => {
  it("constructs the client when instant execution is enabled", () => {
    const client = { invoke: vi.fn(), cancel: vi.fn() };
    const factory = vi.fn(() => client);

    expect(createEnabledProductionInstantExecutorClient(factory, {
      ...requiredEnv,
      LEAF_INSTANT_EXECUTION_ENABLED: "1",
    })).toBe(client);
    expect(factory).toHaveBeenCalledOnce();
  });

  it("does not require credentials when instant execution is disabled", () => {
    const factory = vi.fn();
    expect(createEnabledProductionInstantExecutorClient(factory, {
      LEAF_INSTANT_EXECUTION_ENABLED: "0",
    })).toBeUndefined();
    expect(factory).not.toHaveBeenCalled();
  });

  it("constructs the direct executor client with mounted TLS file paths and a fixed server name", () => {
    const client = { invoke: vi.fn(), cancel: vi.fn() };
    const factory = vi.fn(() => client);

    expect(createProductionInstantExecutorClient(factory, requiredEnv)).toBe(client);
    expect(factory).toHaveBeenCalledExactlyOnceWith({
      requireTls: true,
      caFile: requiredEnv.LEAF_INSTANT_EXECUTOR_CA_FILE,
      clientCertificateFile: requiredEnv.LEAF_INSTANT_EXECUTOR_CERT_FILE,
      clientPrivateKeyFile: requiredEnv.LEAF_INSTANT_EXECUTOR_KEY_FILE,
      tlsServerName: requiredEnv.LEAF_INSTANT_EXECUTOR_TLS_SERVER_NAME,
    });
  });

  it.each([
    "LEAF_INSTANT_EXECUTOR_CA_FILE",
    "LEAF_INSTANT_EXECUTOR_CERT_FILE",
    "LEAF_INSTANT_EXECUTOR_KEY_FILE",
    "LEAF_INSTANT_EXECUTOR_TLS_SERVER_NAME",
  ])("fails closed when %s is absent", (name) => {
    const env = { ...requiredEnv, [name]: "" };
    const factory = vi.fn();

    expect(() => createProductionInstantExecutorClient(factory, env)).toThrow(`${name} is required`);
    expect(factory).not.toHaveBeenCalled();
  });

  it("rejects a TLS identity other than the fixed executor name", () => {
    const factory = vi.fn();
    expect(() => createProductionInstantExecutorClient(factory, {
      ...requiredEnv,
      LEAF_INSTANT_EXECUTOR_TLS_SERVER_NAME: "wrong.internal",
    })).toThrow("must be executor.instant.internal");
    expect(factory).not.toHaveBeenCalled();
  });

  it.each([
    "LEAF_INSTANT_EXECUTOR_CA",
    "LEAF_INSTANT_EXECUTOR_CERT",
    "LEAF_INSTANT_EXECUTOR_KEY",
    "LEAF_INSTANT_EXECUTOR_CA_PEM",
    "LEAF_INSTANT_EXECUTOR_CLIENT_CERT_PEM",
    "LEAF_INSTANT_EXECUTOR_CLIENT_KEY_PEM",
  ])("rejects raw executor credential configuration %s", (name) => {
    const factory = vi.fn();

    expect(() => createProductionInstantExecutorClient(factory, { ...requiredEnv, [name]: "raw pem" })).toThrow(
      `${name} is not allowed`,
    );
    expect(factory).not.toHaveBeenCalled();
  });

  it.each([
    "LEAF_INSTANT_EXECUTOR_PROXY_URL",
    "LEAF_INSTANT_EXECUTOR_PROXY_SECRET",
    "LEAF_INSTANT_EXECUTOR_PROXY_SECRET_FILE",
  ])("rejects proxy configuration %s, including an empty setting", (name) => {
    const factory = vi.fn();

    expect(() => createProductionInstantExecutorClient(factory, { ...requiredEnv, [name]: "" })).toThrow(
      `${name} is not allowed`,
    );
    expect(factory).not.toHaveBeenCalled();
  });
});
