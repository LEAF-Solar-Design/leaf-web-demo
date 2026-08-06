/**
 * Fake OAuthGrantProvider - returns a scripted, obviously-non-real grant. It
 * NEVER reads a real credential store, ambient env, or the platform JWT. Lets the
 * whole author pipeline run hermetically with no Anthropic auth.
 */

import type { AgentGrant, OAuthGrantProvider } from "../index.js";

export class FakeOAuthGrantProvider implements OAuthGrantProvider {
  constructor(private readonly grant: AgentGrant = { kind: "oauth", oauthToken: "FAKE-OAUTH-GRANT-not-a-real-token" }) {}

  async getGrant(_tenantId: string): Promise<AgentGrant> {
    return this.grant;
  }
}
