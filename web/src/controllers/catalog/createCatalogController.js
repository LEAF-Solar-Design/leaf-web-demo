import {
  alternativeDecision,
  entitlementAllows,
  previewLane,
  runnableCatalogTools,
  slashDecision,
} from './catalogRouting.js'
import { track } from '../../telemetry.js'
import { isSecretRefused } from '../../lib/secretGuardTransport.js'

const DEFAULT_THRESHOLDS = { CHIP_ONLY: 0.8, RACE_MIN: 0.55 }

const initialState = Object.freeze({
  tools: [],
  toolsError: null,
  toolsRetryKey: 0,
  catalog: { families: [], source: null },
  catalogError: null,
  openFamilies: {},
  openTool: null,
  prompt: '',
  route: null,
  routing: false,
  routeError: null,
  agentMode: null,
  agentBanner: null,
  // The credential refusal (slice 8a fix round 2): `{id, reason, masked,
  // overridable}` or null. It carries a MASK, never the value. Every bar that
  // renders this controller reads it from here, so the notice cannot drift
  // from the decision that produced it.
  secretRefusal: null,
})

const defaultHumanize = (error) => String(error?.message || error || 'Something went wrong')
const defaultUnauthorized = (error) =>
  error?.status === 401 || / -> 401$/.test(String(error?.message || ''))

/**
 * Framework-neutral catalog and routing controller.
 *
 * API transport, deterministic preview routing, agent routing, error copy, and
 * run-intent preparation are injected. The controller never runs a tool and
 * never imports a component.
 */
export function createCatalogController({ services, adapters = {}, context = {} }) {
  if (!services?.getTools || !services?.getCapabilities || !services?.routePrompt) {
    throw new Error('Catalog controller requires getTools, getCapabilities, and routePrompt services')
  }

  let state = { ...initialState }
  let current = {
    mock: false,
    entitlements: null,
    running: false,
    agentDisabled: true,
    ...context,
  }
  let started = false
  // NO OVERRIDE STATE LIVES HERE, and that absence is the round-3 fix. Round 2
  // kept an armed-override latch on this line; both hosts short-circuit
  // ABOVE dispatch (App on `running`, ToolCast on its precondition set), so a
  // "Send anyway" click whose follow-on dispatch never arrived left the latch
  // armed and the NEXT unrelated Enter skipped the guard. The override is now a
  // parameter carried into the one call the click authorised, so a click that
  // cannot dispatch authorises exactly nothing.
  let toolsRequest = 0
  let catalogRequest = 0
  let snapshot = null
  const listeners = new Set()

  const humanizeError = adapters.humanizeError || defaultHumanize
  const isUnauthorized = adapters.isUnauthorized || defaultUnauthorized
  const thresholds = { ...DEFAULT_THRESHOLDS, ...(adapters.thresholds || {}) }

  const publish = (patch) => {
    state = { ...state, ...patch }
    snapshot = null
    for (const listener of listeners) listener()
  }

  const getSnapshot = () => {
    if (!snapshot) {
      snapshot = {
        ...state,
        canRunWrite: entitlementAllows(current.entitlements, 'run_write'),
        runnableTools: runnableCatalogTools(state.tools, current.entitlements),
        hintLane: adapters.previewRoute
          ? previewLane(state.prompt, state.tools, adapters.previewRoute)
          : null,
        capabilityCount: (state.catalog.families || [])
          .reduce((count, family) => count + (family.capabilities || []).length, 0),
      }
    }
    return snapshot
  }

  // P2 wave C-2 (shape from review #428 round 1): route.outcome fires ONLY
  // for prompt-lane routes (the NL box and slash fast-path; catalog/tour/
  // authored arms carry a source and are excluded), only at the transitions
  // that actually clear or replace the shown route, and only when the
  // replacement really happened (an adapter that refuses to arm returns
  // undefined and the route stays shown).
  const isPromptRoute = (route) => !route?.source
  const noteRouteResolved = (outcome, route) => {
    if (!route || !isPromptRoute(route)) return
    track('route.outcome', { outcome, ...(route.tool ? { tool: route.tool } : {}) })
  }

  const commitDecision = (decision, { routeOutcome = 'invalidated' } = {}) => {
    const committed = adapters.commitDecision ? adapters.commitDecision(decision) : decision
    if (committed !== undefined) {
      if (state.route && state.route !== committed) noteRouteResolved(routeOutcome, state.route)
      publish({ route: committed })
    }
    return committed
  }

  const openAuthorFlow = (text) => adapters.openAuthor?.(text)

  const loadTools = async () => {
    const request = ++toolsRequest
    publish({ toolsError: null })
    try {
      const tools = await services.getTools(current.mock)
      if (request === toolsRequest) publish({ tools })
      return tools
    } catch (error) {
      if (request === toolsRequest) publish({ toolsError: humanizeError(error) })
      return undefined
    }
  }

  const loadCatalog = async () => {
    const request = ++catalogRequest
    publish({ catalogError: null })
    try {
      const catalog = await services.getCapabilities(current.mock)
      if (request !== catalogRequest) return catalog
      const openFamilies = { ...state.openFamilies }
      for (const family of catalog.families || []) {
        if (!(family.family_id in openFamilies)) openFamilies[family.family_id] = false
      }
      publish({ catalog, openFamilies })
      return catalog
    } catch (error) {
      if (request !== catalogRequest) return undefined
      publish({
        catalog: { families: [], source: null },
        catalogError: humanizeError(error),
      })
      if (!current.mock && isUnauthorized(error)) adapters.onAuthRequired?.()
      return undefined
    }
  }

  // outcome vocabulary: accepted (the run that started IS the routed tool),
  // alternative_picked, dismissed (explicit Esc/X), invalidated (typed over,
  // replaced, or the armed confirmation died). An explicit `outcome` from the
  // caller wins over the ranTool inference.
  const dismissRoute = ({ ranTool = null, outcome = null } = {}) => {
    if (state.route) {
      noteRouteResolved(
        outcome
          || (ranTool == null ? 'dismissed' : ranTool === state.route.tool ? 'accepted' : 'invalidated'),
        state.route,
      )
    }
    adapters.dismissDecision?.()
    publish({ route: null })
  }

  const setPrompt = (value) => {
    // Typing over a shown route resolves it: the user moved on. An edit also
    // retires the credential refusal, which was about the text that WAS there;
    // a notice outliving its text reads as a stuck error.
    if (state.route) noteRouteResolved('invalidated', state.route)
    adapters.dismissDecision?.()
    publish({
      prompt: value,
      route: state.route ? null : state.route,
      routeError: state.routeError ? null : state.routeError,
      secretRefusal: state.secretRefusal ? null : state.secretRefusal,
    })
  }

  const dispatch = async (override, { allowSecretOnce = false } = {}) => {
    // --- credential refusal: THE RENDER CHANNEL, not the guard (round 3) ---
    // The GUARD now sits on the wire (lib/secretGuardTransport.js, called by
    // api.nlPrompt and converse.postMessage). This function no longer decides
    // anything about credentials; it CATCHES the transport's typed refusal and
    // publishes it as `secretRefusal` so the bars that read this controller
    // have something to render. That split is deliberate: a funnel can be
    // short-circuited by its host (both were), a transport cannot be.
    //
    // EVERY bar path still arrives here: the app bar's Enter/Run/slash pick
    // (PromptBox -> App.onDispatch), the /try bar's Enter and Run button
    // (ToolCast.dispatchRequest), App's failed-strip Retry chip and its R-key
    // twin (both call onDispatch with a non-string, so the text comes from
    // `state.prompt` — whatever sits in the bar at click time, which is how a
    // credential pasted DURING an in-flight route used to reach the wire), and
    // the tour's canned prompts. Guarding the composers one at a time is what
    // failed twice: the census was short by one both times. This is the choke
    // point, so a new bar cannot be added around it.
    //
    const text = (typeof override === 'string' ? override : state.prompt).trim()
    if (!text || state.routing || current.running) return undefined
    if (state.secretRefusal) publish({ secretRefusal: null })
    // W4f slice B: a typed CAD command word (LINE, C, MOVE ...) on a drafting
    // surface is the cockpit's business, not the router's. The adapter
    // returns true only when the whole text is exactly one known word and
    // the surface can take it; then the bar clears and nothing routes.
    if (adapters.drawingCommand?.(text)) {
      track('prompt.submitted', { input_kind: 'command', text_len: text.length })
      if (state.route) noteRouteResolved('invalidated', state.route)
      adapters.dismissDecision?.()
      publish({ prompt: '', route: null, routeError: null })
      return undefined
    }
    // P2 funnel top: THE active dispatch path (the legacy App.jsx inline
    // handler is disabled). text_len only, never text. slash vs typed only:
    // a string override is NOT a reliable canned signal (ToolCast passes
    // typed text as a string); tour attribution rides the tracker's
    // tour_step context (telemetry.setTourStep), stamped on every organic
    // event while the tour is active.
    track('prompt.submitted', {
      input_kind: text.startsWith('/') ? 'slash' : 'typed',
      text_len: text.length,
    })
    // A route still shown at re-dispatch (override strings skip setPrompt)
    // resolves as invalidated before the state below silently nulls it.
    if (state.route) noteRouteResolved('invalidated', state.route)
    adapters.dismissDecision?.()

    const slash = slashDecision(text, state.tools)
    if (slash.handled) {
      publish({ route: null, routeError: null })
      return slash.decision ? commitDecision(slash.decision) : undefined
    }

    publish({ routing: true, route: null, routeError: null })
    try {
      const decision = await services.routePrompt(current.mock, text, state.tools, { allowSecretOnce })
      const confidence = Number(decision.confidence) || 0
      const chipOnly = current.agentDisabled ||
        (decision.lane === 'run' && !!decision.tool && confidence >= thresholds.CHIP_ONLY)

      if (chipOnly) {
        commitDecision(decision)
        if (decision.lane === 'build') openAuthorFlow(text)
      } else {
        const hint = {
          lane: decision.lane,
          tool: decision.tool || null,
          confidence,
          rationale: decision.rationale || null,
        }
        publish({ agentBanner: null })
        if (decision.lane === 'run' && !!decision.tool && confidence >= thresholds.RACE_MIN) {
          commitDecision(decision)
          try {
            await adapters.startAgentTurn(text, hint, { allowSecretOnce })
            publish({ agentMode: 'race' })
          } catch (error) {
            // A credential refusal is not an agent outage. It must reach the
            // outer catch, which renders it as the refusal notice; an agent
            // banner here would say "the assistant is unavailable" about a
            // client-side decision that never left the browser.
            if (isSecretRefused(error)) throw error
            publish({ agentBanner: adapters.agentBannerFor?.(error) || null })
          }
        } else {
          try {
            await adapters.startAgentTurn(text, hint, { allowSecretOnce })
            publish({ agentMode: 'primary' })
          } catch (error) {
            if (isSecretRefused(error)) throw error
            commitDecision(decision)
            if (decision.lane === 'build') openAuthorFlow(text)
            publish({ agentBanner: adapters.agentBannerFor?.(error) || null })
          }
        }
      }
      return decision
    } catch (error) {
      // The transport refused before anything left the browser. It is not a
      // routing failure, so it renders as the refusal notice and NOT as a red
      // route error; `route` is nulled so a stale decision strip cannot sit
      // under a notice saying nothing was sent.
      if (isSecretRefused(error)) {
        publish({ secretRefusal: error.refusal, route: null, routeError: null })
        return undefined
      }
      publish({ routeError: humanizeError(error) })
      return undefined
    } finally {
      publish({ routing: false })
    }
  }

  const actions = Object.freeze({
    loadTools,
    retryTools() {
      publish({ toolsRetryKey: state.toolsRetryKey + 1 })
      return loadTools()
    },
    loadCatalog,
    upsertTool(tool) {
      if (!tool?.name) return
      publish({ tools: [...state.tools.filter((candidate) => candidate.name !== tool.name), tool] })
    },
    toggleFamily(familyId) {
      publish({ openFamilies: { ...state.openFamilies, [familyId]: !state.openFamilies[familyId] } })
    },
    setFamilyOpen(familyId, open) {
      publish({ openFamilies: { ...state.openFamilies, [familyId]: !!open } })
    },
    openTool(tool) { publish({ openTool: tool || null }) },
    closeTool() { publish({ openTool: null }) },
    resetTransient() {
      adapters.dismissDecision?.()
      publish({
        openTool: null,
        prompt: '',
        route: null,
        routing: false,
        routeError: null,
        agentMode: null,
        agentBanner: null,
        secretRefusal: null,
      })
    },
    openAgentMode() { publish({ agentMode: 'primary' }) },
    clearAgentMode() { publish({ agentMode: null }) },
    clearAgentBanner() { publish({ agentBanner: null }) },
    setPrompt,
    dismissRoute,
    commitDecision,
    dispatch,
    completeSlash(name) { setPrompt(name ? `/${name}` : '/') },
    dispatchSlash(name) { return dispatch(name ? `/${name}` : '/') },
    pickAlternative(name) {
      return commitDecision(alternativeDecision(state.route, name), { routeOutcome: 'alternative_picked' })
    },
    clearRouteError() { publish({ routeError: null }) },
    // There is deliberately NO allowSecretOnce() action. An override is a
    // parameter on the call the user authorised — `dispatch(text, {
    // allowSecretOnce: true })` — so there is no armed state for a
    // short-circuited host to strand. Round 2 had one here and it latched.
    clearSecretRefusal() {
      if (state.secretRefusal) publish({ secretRefusal: null })
    },
  })

  return Object.freeze({
    start() {
      if (started) return
      started = true
      void loadTools()
      void loadCatalog()
    },
    destroy() {
      started = false
      toolsRequest += 1
      catalogRequest += 1
      listeners.clear()
    },
    setContext(next) {
      const modeChanged = Object.prototype.hasOwnProperty.call(next, 'mock') && next.mock !== current.mock
      const derivedChanged = Object.prototype.hasOwnProperty.call(next, 'entitlements') &&
        next.entitlements !== current.entitlements
      current = { ...current, ...next }
      if (derivedChanged) {
        snapshot = null
        for (const listener of listeners) listener()
      }
      if (started && modeChanged) {
        void loadTools()
        void loadCatalog()
      }
    },
    getState: getSnapshot,
    subscribe(listener) {
      listeners.add(listener)
      return () => listeners.delete(listener)
    },
    actions,
  })
}

export default createCatalogController
