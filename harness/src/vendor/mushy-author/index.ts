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
export * from "./ports/impl/requestScheduler.js";
export * from "./ports/impl/conversationManager.js";
export * from "./ports/impl/requestJournal.js";
export * from "./ports/impl/httpUpstreamSink.js";
export * from "./ports/impl/standardServices.js";
export * from "./ports/impl/standardServicesRuntime.js";
export * from "./ports/impl/tenantBrokerStandardServiceProvider.js";
export * from "./ports/impl/runnerCapabilities.js";
export * from "./ports/impl/standardServicesFacade.js";
export * from "./browser/conversationWorkspace.js";
export * from "./browser/historyFeed.js";

// Hermetic fakes are the CONSUMER-facing test doubles and live behind the
// `mushy-author/testing` subpath (see src/testing/index.ts), NOT the production
// root barrel — a product bundle must not pull a fake in by importing the root.
// (The package's own tests + examples import them straight from ports/fakes/*.)

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
