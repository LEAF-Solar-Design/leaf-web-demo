import {
  alternativeDecision,
  entitlementAllows,
  previewLane,
  runnableCatalogTools,
  slashDecision,
} from './catalogRouting.js'
import { track } from '../../telemetry.js'

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

  const commitDecision = (decision) => {
    const committed = adapters.commitDecision ? adapters.commitDecision(decision) : decision
    if (committed !== undefined) publish({ route: committed })
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

  const dismissRoute = () => {
    adapters.dismissDecision?.()
    publish({ route: null })
  }

  const setPrompt = (value) => {
    adapters.dismissDecision?.()
    publish({
      prompt: value,
      route: state.route ? null : state.route,
      routeError: state.routeError ? null : state.routeError,
    })
  }

  const dispatch = async (override) => {
    const text = (typeof override === 'string' ? override : state.prompt).trim()
    if (!text || state.routing || current.running) return undefined
    // P2 funnel top: THE active dispatch path (the legacy App.jsx inline
    // handler is disabled). text_len only, never text.
    track('prompt.submitted', {
      input_kind: text.startsWith('/') ? 'slash'
        : typeof override === 'string' ? 'canned' : 'typed',
      text_len: text.length,
    })
    adapters.dismissDecision?.()

    const slash = slashDecision(text, state.tools)
    if (slash.handled) {
      publish({ route: null, routeError: null })
      return slash.decision ? commitDecision(slash.decision) : undefined
    }

    publish({ routing: true, route: null, routeError: null })
    try {
      const decision = await services.routePrompt(current.mock, text, state.tools)
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
            await adapters.startAgentTurn(text, hint)
            publish({ agentMode: 'race' })
          } catch (error) {
            publish({ agentBanner: adapters.agentBannerFor?.(error) || null })
          }
        } else {
          try {
            await adapters.startAgentTurn(text, hint)
            publish({ agentMode: 'primary' })
          } catch (error) {
            commitDecision(decision)
            if (decision.lane === 'build') openAuthorFlow(text)
            publish({ agentBanner: adapters.agentBannerFor?.(error) || null })
          }
        }
      }
      return decision
    } catch (error) {
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
      })
    },
    clearAgentMode() { publish({ agentMode: null }) },
    clearAgentBanner() { publish({ agentBanner: null }) },
    setPrompt,
    dismissRoute,
    commitDecision,
    dispatch,
    completeSlash(name) { setPrompt(name ? `/${name}` : '/') },
    dispatchSlash(name) { return dispatch(name ? `/${name}` : '/') },
    pickAlternative(name) {
      return commitDecision(alternativeDecision(state.route, name))
    },
    clearRouteError() { publish({ routeError: null }) },
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
