# Catalog controller integration map

The controller is transport-injected and UI-free. It does not execute tools.

## Stable interface

`createCatalogController({ services, adapters, context })` returns:

- `start()`, `destroy()`, `setContext(next)`, `getState()`, and `subscribe(listener)`
- `actions.loadTools`, `retryTools`, `loadCatalog`, `upsertTool`, `toggleFamily`, `setFamilyOpen`
- `actions.openTool`, `closeTool`, `setPrompt`, `dismissRoute`, `dispatch`
- `actions.completeSlash`, `dispatchSlash`, `pickAlternative`, `clearRouteError`

State includes flat tools and errors, grouped catalog and errors, family and tool
disclosure, prompt and route state, agent state, `runnableTools`, `hintLane`,
`canRunWrite`, and `capabilityCount`.

## App.jsx source map

| Existing behavior | App.jsx lines | Controller member |
| --- | ---: | --- |
| Flat tools load and retained-list failure | 440-447 | `loadTools`, `state.tools`, `state.toolsError` |
| Retry counter and reload | 449-450 | `retryTools`, `state.toolsRetryKey` |
| Families load, collapsed defaults, flat fallback error | 452-474 | `loadCatalog`, `state.catalog`, `state.catalogError`, `state.openFamilies` |
| Unknown entitlement is permissive | 336-346 | `entitlementAllows`, `state.canRunWrite` |
| Plan-filtered slash catalog | 1296-1302 | `state.runnableTools` |
| Deterministic route preview | 1287-1294 | `state.hintLane` |
| Slash exact match and typo alternatives | 1304-1347 | `dispatch`, `slashDecision`, `dispatchSlash` |
| NL routing and two-tier agent fallback | 1348-1405 | `dispatch`, injected agent adapters |
| Prompt change invalidates route and failure | 1407-1413 | `setPrompt` |
| Alternative selection retains stub metadata | 1415-1425 | `pickAlternative` |
| Catalog disclosure and tool detail | 211-219, 1942-1957 | `toggleFamily`, `openTool`, `closeTool` |
| Published tool replaces its old flat entry | 1244-1254 | `upsertTool`, then `loadCatalog` |

## Wiring

Inject `getTools`, `getCapabilities`, and `nlPrompt` from `api.js` as the three
services. Inject `matchPrompt` as `previewRoute`, `humanizeError`, the shared
agent thresholds and agent adapters, and the existing run-intent `armDecision`
as `commitDecision`. The latter keeps workspace checks and immutable run-intent
staging outside this controller.

Both `/app` and `/try` can subscribe to the same controller instance through a
workspace provider. This avoids a second catalog or routing state machine.
