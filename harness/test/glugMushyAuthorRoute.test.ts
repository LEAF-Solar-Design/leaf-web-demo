import type { Server } from "node:http";
import type { AddressInfo } from "node:net";
import { afterEach, describe, expect, it } from "vitest";

import { createHarness } from "../src/server.js";
import type { GlugMushyAuthor } from "../src/glugMushyAuthor.js";
import { FakeAgentRunner } from "../src/ports/fakes/fakeAgentRunner.js";
import { FakeBrokerApsClient } from "../src/ports/fakes/fakeBrokerApsClient.js";
import { FakeOAuthGrantProvider } from "../src/ports/fakes/fakeOAuthGrant.js";
import { FakeTenantRepoProvider } from "../src/ports/fakes/fakeTenantRepo.js";

const SECRET = "test-harness-secret-with-enough-entropy";
const SOURCE = "c3fdc0869692c804ae69fe00b5b6f0722c80943a";

describe("Glug Mushy harness route", () => {
  const servers: Server[] = [];
  afterEach(() => {
    for (const server of servers.splice(0)) server.close();
  });

  function listen(author?: GlugMushyAuthor): string {
    const server = createHarness(
      {
        oauth: new FakeOAuthGrantProvider(),
        tenantRepo: new FakeTenantRepoProvider(process.cwd()),
        broker: new FakeBrokerApsClient(),
        agentRunner: new FakeAgentRunner(),
      },
      {
        auth: { enabled: true, secret: SECRET },
        ...(author ? { glugMushyAuthor: author } : {}),
      },
    ).listen(0);
    servers.push(server);
    return `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
  }

  it("forwards the closed request and pin headers only after harness authentication", async () => {
    const calls: unknown[][] = [];
    const baseUrl = listen({
      async run(...args) {
        calls.push(args);
        return { text: "answer" };
      },
    });
    const body = {
      contract: "glug.mushy-author-request.v1",
      workspace: "glug",
      power: "code_question",
      instruction: "Where is the weekend title configured?",
      base_commit: "a".repeat(40),
      claim_id: "claim-123",
    };
    const denied = await fetch(`${baseUrl}/internal/glug/mushy/author`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    });
    expect(denied.status).toBe(401);
    expect(calls).toHaveLength(0);

    const response = await fetch(`${baseUrl}/internal/glug/mushy/author`, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        "x-harness-secret": SECRET,
        "x-glug-mushy-source-commit": SOURCE,
        "x-glug-mushy-author-timeout-seconds": "240",
      },
      body: JSON.stringify(body),
    });
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({ text: "answer" });
    expect(calls).toEqual([[body, SOURCE, 240]]);
  });

  it("fails closed when the isolated author is not mounted", async () => {
    const baseUrl = listen();
    const response = await fetch(`${baseUrl}/internal/glug/mushy/author`, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        "x-harness-secret": SECRET,
        "x-glug-mushy-source-commit": SOURCE,
        "x-glug-mushy-author-timeout-seconds": "240",
      },
      body: "{}",
    });
    expect(response.status).toBe(501);
  });

  it("rejects a non-object body before invoking the author", async () => {
    let invoked = false;
    const baseUrl = listen({
      async run() {
        invoked = true;
        return {};
      },
    });
    const response = await fetch(`${baseUrl}/internal/glug/mushy/author`, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        "x-harness-secret": SECRET,
        "x-glug-mushy-source-commit": SOURCE,
        "x-glug-mushy-author-timeout-seconds": "240",
      },
      body: "null",
    });
    expect(response.status).toBe(422);
    expect(invoked).toBe(false);
  });
});
