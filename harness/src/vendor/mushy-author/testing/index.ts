/**
 * mushy-author/testing — the hermetic test doubles a CONSUMER uses to exercise
 * its own wiring against the port contracts, without standing up real Auth0, a
 * broker, Postgres, or an agent SDK.
 *
 * These are the same in-memory fakes the package's own tests run against, so a
 * consumer that wires its real adapters can swap a fake for a real impl one port
 * at a time and keep the rest hermetic. Exposed as a SEPARATE subpath (never from
 * the root barrel) so the fakes never reach a production bundle by accident.
 *
 * This is the seed of the adapter CONFORMANCE suite (a reusable set of contract
 * assertions a consumer runs against its real impls); that suite lands with the
 * first cutover. For now the doubles + the exported port interfaces from the root
 * are enough for a consumer to compile and hermetically test against the seam.
 */

// Reusable adapter conformance assertions a consumer runs against its REAL impls.
export * from "./conformance.js";

export * from "../ports/fakes/fakeAgentRunner.js";
export * from "../ports/fakes/fakeConverseRunner.js";
export * from "../ports/fakes/fakeIntentSynthesizer.js";
export * from "../ports/fakes/fakeOAuthGrant.js";
export * from "../ports/fakes/fakeRepoEditor.js";
export * from "../ports/fakes/fakeSessionStore.js";
export * from "../ports/fakes/fakeStandardServiceProvider.js";
export * from "../ports/fakes/fakeTenantRepo.js";
export * from "../ports/fakes/fakeTurnRunner.js";
export * from "../ports/fakes/fakeUpstreamSink.js";
