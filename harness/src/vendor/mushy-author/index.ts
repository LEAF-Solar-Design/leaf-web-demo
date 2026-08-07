/**
 * mushy-author — public surface.
 *
 * The mushy-codebase pattern, author side: a per-consumer git repo of
 * AI-authored deterministic artifacts; an LLM author loop at design time;
 * validated commit; zero LLM at execution. The runtime fold lives in the
 * sibling package (packages/fold-py).
 *
 * Ports and frozen data contracts come from ./ports/index.js; generic
 * implementations from ./ports/impl/*; hermetic fakes from ./ports/fakes/*.
 * Platform adapters (broker/APS clients, app back-edges, gates) belong to the
 * CONSUMER, wired in through the ports — they are deliberately not here.
 */

export * from "./ports/index.js";
export * from "./ports/wireGrant.js";
export * from "./ports/modelAllowlist.js";

// Generic implementations
export * from "./ports/impl/envScrub.js";
export * from "./ports/impl/gitWorker.js";
export * from "./ports/impl/tenantRepoProvider.js";
export * from "./ports/impl/tenantChangeRepo.js";
export * from "./ports/impl/oauthGrantProvider.js";
export * from "./ports/impl/sessionStore.js";
export * from "./ports/impl/pgSessionStore.js";
export * from "./ports/impl/sessionStoreFactory.js";
export * from "./ports/impl/harnessSchema.js";
export * from "./ports/impl/skillBundle.js";
export * from "./ports/impl/agentSdkRunner.js";
export * from "./ports/impl/agentSdkTurnRunner.js";
export * from "./ports/impl/e2bAgentRunner.js";
export * from "./ports/impl/converseSdkRunner.js";
export * from "./ports/impl/mcpProxy.js";
export * from "./ports/impl/repoEditRunner.js";
export * from "./ports/impl/httpUpstreamSink.js";
export * from "./ports/impl/standardServices.js";
export * from "./ports/impl/standardServicesRuntime.js";
export * from "./ports/impl/tenantBrokerStandardServiceProvider.js";
export * from "./ports/impl/runnerCapabilities.js";
export * from "./ports/impl/standardServicesFacade.js";
export * from "./ports/impl/gatewayStandardServiceProvider.js";

// Hermetic fakes (contract-test doubles; also the CI path for consumers)
export * from "./ports/fakes/fakeAgentRunner.js";
export * from "./ports/fakes/fakeConverseRunner.js";
export * from "./ports/fakes/fakeIntentSynthesizer.js";
export * from "./ports/fakes/fakeOAuthGrant.js";
export * from "./ports/fakes/fakeSessionStore.js";
export * from "./ports/fakes/fakeTenantRepo.js";
export * from "./ports/fakes/fakeTurnRunner.js";
export * from "./ports/fakes/fakeRepoEditor.js";
export * from "./ports/fakes/fakeUpstreamSink.js";
export * from "./ports/fakes/fakeStandardServiceProvider.js";

// Registry + author loop
export * from "./registry/registerTool.js";
export * from "./registry/toolPackageSchema.js";
export * from "./agent/authorLoop.js";
export * from "./agent/systemPrompt.js";
export * from "./agent/tools/fsTenantRepo.js";
export * from "./agent/tools/apsTestRun.js";
export * from "./agent/tools/submitToolProposal.js";
export * from "./agent/tools/validateTool.js";
export * from "./agent/tools/toolExecutionReceipt.js";
export * from "./redact.js";
