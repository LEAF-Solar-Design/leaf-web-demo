// Lane C gate (obligation O1, model half): the operator MODEL child env is
// built from an explicit allowlist, so no production deploy credential — under
// ANY name, including one the tenant scrub's name-denylist would miss — reaches
// the operator model process. This is the BEHAVIORAL mutation check: it runs the
// real builder. If a future edit widens the env source (falls back to the
// vendored scrub, or passes the parent env through), the planted credentials
// below stop being stripped and this test FAILS.

import { describe, expect, it } from "vitest";
import {
  OPERATOR_MODEL_ENV_ALLOWLIST,
  buildOperatorModelEnv,
  type OperatorModelGrant,
} from "../src/operatorModel/operatorModelEnv.js";

const GRANT: OperatorModelGrant = {
  credentialKey: "OPERATOR_MODEL_API_KEY",
  credentialValue: "sk-operator-model-canary",
};

describe("operator model child env (O1 model half)", () => {
  it("strips unknown-named production deploy credentials from the parent env", () => {
    const parent: NodeJS.ProcessEnv = {
      PATH: "/usr/bin",
      HOMEDRIVE: "C:",
      // Production deploy credentials the tenant NAME-denylist would NOT catch
      // (no SECRET/TOKEN/KEY/AUTH substring). These MUST NOT cross.
      LEAF_LIVE_ACCESS: "prod-deploy-grant-canary",
      PROD_AUTHZ: "prod-authz-canary",
      LEAF_LIVE_ENDPOINT: "https://api.leafdesign.ai",
      // An arbitrary unknown-named credential.
      DEPLOYER_9F3: "unknown-name-canary",
    };

    const env = buildOperatorModelEnv(parent, GRANT);
    const serialized = JSON.stringify(env);

    // None of the planted production credentials or their values cross.
    expect(env).not.toHaveProperty("LEAF_LIVE_ACCESS");
    expect(env).not.toHaveProperty("PROD_AUTHZ");
    expect(env).not.toHaveProperty("LEAF_LIVE_ENDPOINT");
    expect(env).not.toHaveProperty("DEPLOYER_9F3");
    expect(serialized).not.toContain("prod-deploy-grant-canary");
    expect(serialized).not.toContain("prod-authz-canary");
    expect(serialized).not.toContain("api.leafdesign.ai");
    expect(serialized).not.toContain("unknown-name-canary");
  });

  it("passes ONLY allowlisted OS keys plus the one injected model credential", () => {
    const parent: NodeJS.ProcessEnv = {
      PATH: "/usr/bin",
      HOMEDRIVE: "C:",
      LEAF_LIVE_ACCESS: "prod-deploy-grant-canary",
    };

    const env = buildOperatorModelEnv(parent, GRANT);

    // The allowlisted keys present in the parent survive.
    expect(env.PATH).toBe("/usr/bin");
    expect(env.HOMEDRIVE).toBe("C:");
    // The single grant credential is injected.
    expect(env.OPERATOR_MODEL_API_KEY).toBe("sk-operator-model-canary");
    // Marker only; no secret.
    expect(env.LEAF_OPERATOR_MODEL).toBe("1");

    // Every key is either allowlisted, the marker, or the grant credential —
    // nothing else. This is what makes an unknown-named credential impossible.
    const permitted = new Set<string>([
      ...OPERATOR_MODEL_ENV_ALLOWLIST,
      "LEAF_OPERATOR_MODEL",
      GRANT.credentialKey,
    ]);
    for (const key of Object.keys(env)) {
      expect(permitted.has(key)).toBe(true);
    }
  });

  it("never reads the credential from the parent env (no credential key is allowlisted)", () => {
    // The grant credential is injected explicitly; a same-named value in the
    // parent env is irrelevant. Prove no credential key is on the allowlist.
    const CRED_NAME_RE =
      /(SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL|APIKEY|API_KEY|_KEY$|AUTH|JWT|ACCESS|LIVE|PROD|DEPLOY)/i;
    for (const key of OPERATOR_MODEL_ENV_ALLOWLIST) {
      expect(CRED_NAME_RE.test(key)).toBe(false);
    }
  });
});
