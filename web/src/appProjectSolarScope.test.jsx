import React, { StrictMode } from 'react'
import { afterEach, describe, it, expect, vi } from 'vitest'
import { act, cleanup, render, renderHook, waitFor } from '@testing-library/react'
import { readFileSync } from 'node:fs'
import { parse } from '@babel/parser'
import { createCatalogRunContext } from './runIntent.js'
import { composerDraftAccountScope } from './lib/composerDraft.js'
import { createProjectSolarClient, getCapabilities } from './api.js'
import { useProjectSolarScope } from './solar/useProjectSolarScope.js'
import useCatalogController from './controllers/catalog/useCatalogController.js'
import { useProjectCheckoutController } from './controllers/useProjectCheckoutController.js'
import { createSessionController } from './controllers/session/createSessionController.js'
import { canOpenSolarSettingsForm, solarSettingsFormChoice, solarSettingsRunFeedback, solarSettingsScope } from './solar/solarSettingsWire.js'
import { solarFlowRunsFor, solarFlowRunStatus } from './solar/solarFlowModel.js'
import { projectSolarAppReason, projectSolarCheckoutState, projectSolarScopeSnapshot } from './solar/solarProjectAppModel.js'

vi.mock('./telemetry.js', () => ({ track: vi.fn() }))

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  localStorage.clear()
})

const source = readFileSync(`${process.cwd()}/src/App.jsx`, 'utf8').replace(/\r\n/g, '\n')
const tree = parse(source, { sourceType: 'module', plugins: ['jsx'] })
function nodes(predicate) {
  const found = []
  const walk = (node, parent) => {
    if (!node || typeof node.type !== 'string') return
    if (predicate(node)) found.push({ node, parent })
    for (const value of Object.values(node)) {
      if (Array.isArray(value)) value.forEach((child) => walk(child, node))
      else if (value && typeof value.type === 'string') walk(value, node)
    }
  }
  walk(tree, null)
  return found
}
const text = (node) => source.slice(node.start, node.end)
const evaluate = (node, inputs) => Function(...Object.keys(inputs),
  `return (${text(node)})`)(...Object.values(inputs))
// Interpret the actual App AST, selecting branches before reading their JSX
// or props. Component bindings are supplied explicitly by each rendered row.
function element(node, inputs, components = {}) {
  const bindings = { ...inputs, ...components }
  if (node.type === 'LogicalExpression') {
    const left = element(node.left, inputs, components)
    if (node.operator === '&&') return left && element(node.right, inputs, components)
    if (node.operator === '||') return left || element(node.right, inputs, components)
    if (node.operator === '??') return left ?? element(node.right, inputs, components)
    throw new Error('Unsupported logical operator: ' + node.operator)
  }
  if (node.type === 'ConditionalExpression') {
    return element(evaluate(node.test, bindings) ? node.consequent : node.alternate, inputs, components)
  }
  if (node.type === 'JSXExpressionContainer') {
    if (node.expression.type === 'JSXEmptyExpression') return null
    return element(node.expression, inputs, components)
  }
  if (node.type === 'JSXText') return node.value.trim() || null
  if (node.type === 'JSXFragment') {
    return React.createElement(React.Fragment, null, ...node.children.map((child) => element(child, inputs, components)))
  }
  if (node.type !== 'JSXElement') {
    if (node.type.startsWith('JSX')) throw new Error('Unsupported JSX form: ' + node.type)
    return evaluate(node, bindings)
  }
  if (node.openingElement.name.type !== 'JSXIdentifier') throw new Error('Unsupported JSX name')
  const name = node.openingElement.name.name
  const component = components[name] ?? (name[0] === name[0].toLowerCase() ? name : null)
  if (!component) throw new Error('Missing component binding: ' + name)
  const props = {}
  for (const attribute of node.openingElement.attributes) {
    if (attribute.type !== 'JSXAttribute' || attribute.name.type !== 'JSXIdentifier') throw new Error('Unsupported JSX attribute')
    if (attribute.value !== null && !['JSXExpressionContainer', 'StringLiteral'].includes(attribute.value.type)) {
      throw new Error('Unsupported JSX attribute value: ' + attribute.value.type)
    }
    props[attribute.name.name] = attribute.value === null ? true :
      attribute.value.type === 'JSXExpressionContainer' ? evaluate(attribute.value.expression, bindings) : attribute.value.value
  }
  return React.createElement(component, props, ...node.children.map((child) => element(child, inputs, components)).filter((child) => child !== null))
}
const declaration = (name) => nodes((node) => node.type === 'VariableDeclarator' && node.id.name === name)[0]
const catalogCall = nodes((node) => node.type === 'CallExpression' && node.callee.name === 'useCatalogController')[0]
const initialContext = catalogCall.node.arguments[0].properties.find((prop) => prop.key.name === 'context').value
const catalogDeclaration = nodes((node) => node.type === 'VariableDeclarator' && node.init === catalogCall.node)[0].parent
const contextEffect = nodes((node) => node.type === 'CallExpression' && node.callee.name === 'useEffect'
  && text(node.arguments[0]).includes('...(ENV_SOLAR_SETTINGS_FORM ? solarSettingsScope({'))[0].node
const ownerSource = source.slice(declaration('catalogRunContext').parent.start, declaration('catalogServices').parent.end)
const intents = Function(`return ${text(declaration('PROJECT_SOLAR_INTENTS').node.init)}`)()

// Execute the actual App declarations, initial context and context effect. Only
// unrelated App inputs are supplied by this fixture; the owner, session, client
// and catalog implementations below are production modules.
const useAppScope = Function('React', 'createCatalogRunContext', 'composerDraftAccountScope',
  'useProjectSolarScope', 'useCatalogController', 'solarSettingsScope', 'PROJECT_SOLAR_INTENTS', `
  return function useAppScope(input, services) {
    const { useRef, useMemo, useCallback, useEffect } = React
    const { mock, tenant, orgId, openProjectId, workspace, canonicalVersionId, drawingState,
      surfaceSlots, ENV_SOLAR_SETTINGS_FORM } = input
    const session = { ...React.useSyncExternalStore(input.controller.subscribe,
      input.controller.getSnapshot, input.controller.getSnapshot), controller: input.controller }
    const { getTools, getCapabilities, routePrompt: nlPrompt, createClient: createProjectSolarClient } = services
    const config = { tenant: 'fallback-tenant' }
    const REQUESTED_DRAWING_ID = 'standalone-drawing'
    const entitlements = input.entitlements === undefined ? null : input.entitlements
    const agentDisabled = true
    const catalogAdapters = { draftStorage: null }
    ${text(declaration('projectModeRef').parent)}
    ${ownerSource}
    const initialContext = ${text(initialContext)}
    ${text(catalogDeclaration)}
    ${text(contextEffect)}
    return { projectSolar, projectMode, projectModeRef, projectSolarSession, catalogRunContext,
      catalogServices, catalogController, catalogState, catalogActions, initialContext }
  }
`)(React, createCatalogRunContext, composerDraftAccountScope, useProjectSolarScope,
  useCatalogController, solarSettingsScope, intents)

const O = '11111111-1111-1111-1111-111111111111'
const P = '22222222-2222-2222-2222-222222222222'
const D = '33333333-3333-3333-3333-333333333333'
const V = '44444444-4444-4444-4444-444444444444'
const V2 = '55555555-5555-5555-5555-555555555555'
const binding = { kind: 'project', organization_id: O, project_id: P, drawing_id: D, input_version_id: V }
const catalogData = (id = 'original') => ({ families: [{ family_id: id, capabilities: [] }] })
const response = (data) => ({ ok: true, status: 200, json: async () => data })
const deferred = () => {
  let resolve
  const promise = new Promise((yes) => { resolve = yes })
  return { promise, resolve }
}
function account(subject = 'alice') {
  if (subject === null) localStorage.removeItem('leaf.jwt')
  else if (subject === 'invalid') localStorage.setItem('leaf.jwt', 'invalid')
  else localStorage.setItem('leaf.jwt', `fixture.${btoa(JSON.stringify({ iss: 'fixture-issuer', sub: subject }))}.fixture`)
}
function setup(overrides = {}, endSession = vi.fn(async () => {})) {
  account()
  // A missing sentinel would enter api.js's legacy transport. Keep that local
  // and observable so the regression never issues an external request.
  const legacyFetch = vi.fn(async () => response(catalogData('legacy')))
  vi.stubGlobal('fetch', legacyFetch)
  const controller = createSessionController({ storage: localStorage, endSession })
  controller.actions.activate({ tenant: 'tenant', org: O })
  let fence = 0
  const fetchImpl = vi.fn(async (url) => {
    if (String(url).includes('/api/capabilities?')) return response(catalogData())
    return response({ organization_id: O, project_id: P, drawing_id: D, acquired: true,
      checkout: { holder: 'fixture holder', fence: String(++fence), acquired: new Date().toISOString(),
        expires: new Date(Date.now() + 3600000).toISOString() },
      checkout_capability: 'lco1.' + 'a'.repeat(64) })
  })
  const clients = []
  const createClient = vi.fn(() => {
    expect(clients.filter((client) => client.dispose.mock.calls.length === 0)).toHaveLength(0)
    const raw = createProjectSolarClient({ fetchImpl, authHeaders: () => ({}) })
    const listeners = new Set()
    const retained = []
    const unsubscribes = []
    const client = { ...raw, listeners, retained, unsubscribes,
      dispose: vi.fn(raw.dispose), setScope: vi.fn(raw.setScope),
      acquireCheckout: vi.fn(raw.acquireCheckout), readCheckout: vi.fn(raw.readCheckout),
      subscribe: vi.fn((listener) => {
        listeners.add(listener)
        retained.push(listener)
        const stop = raw.subscribe(listener)
        const unsubscribe = vi.fn(() => { listeners.delete(listener); stop() })
        unsubscribes.push(unsubscribe)
        return unsubscribe
      }) }
    clients.push(client)
    return client
  })
  const services = { createClient, getTools: vi.fn(async () => []), getCapabilities: vi.fn(getCapabilities), routePrompt: vi.fn() }
  const input = { mock: false, tenant: 'tenant', orgId: O, openProjectId: P,
    workspace: { project: { project_id: P }, drawing_versions: [V, V2].map((version_id) =>
      ({ version_id, org_id: O, project_id: P, drawing_id: D })) }, canonicalVersionId: V,
    drawingState: null, surfaceSlots: { toolbar: { profile: 'solar' } }, ENV_SOLAR_SETTINGS_FORM: true,
    controller, ...overrides }
  return { input, services, clients, fetchImpl, legacyFetch, controller, endSession }
}
function mount(run, wrapper) {
  return renderHook((input) => useAppScope(input, run.services), { initialProps: run.input, wrapper })
}
async function receipt(hook) {
  await waitFor(() => expect(hook.result.current.catalogState.projectCatalog).toBeTruthy())
  return hook.result.current.catalogState.projectCatalog
}

describe('App project Solar scope ownership', () => {
  it('R13F-15b project mode admits the workspace under the complete Solar fence', () => {
    const seats = nodes((node) => node.type === 'JSXElement' && node.openingElement.name.name === 'SolarFlowSeat')
    expect(seats).toHaveLength(1)
    const expressions = seats[0].node.children.filter((node) => node.type === 'JSXExpressionContainer')
    expect(expressions).toHaveLength(2)
    expect(text(expressions[0].expression.left)).toBe('solarOverlayStatus')
    const branch = expressions[1].expression
    expect(branch.type).toBe('ConditionalExpression')
    expect(branch.alternate.type).toBe('JSXFragment')
    expect(expressions[0].end).toBeLessThan(branch.start)
    const loaders = { readIntake: vi.fn(), readVersions: vi.fn() }
    const components = Object.fromEntries(['SolarSettingsForm', 'SolarToolForm', 'SolarStepEditor', 'SolarFlowRail', 'SolarWorkspaceTools'].map((name) => [name,
      vi.fn((props) => {
        props.readIntake?.(props.drawingId ?? props.context?.drawingId, props.drawingVersion ?? props.context?.drawingVersion)
        props.readVersions?.(props.context?.drawingId)
        return React.createElement('div', { 'data-testid': name })
      })]))
    const workspaceProbe = vi.fn(() => React.createElement('div', { 'data-testid': 'project-workspace' }))
    const projectComponents = { ...components, ProjectSolarWorkspace: workspaceProbe, ProjectSolarGraphForm: () => null }
    const registry = { panels: 'registry' }
    components.SolarWorkspaceTools.workspacePanelsByFlow = registry
    const context = { drawingId: 'standalone-drawing', drawingVersion: 7, projectId: null }
    const form = { name: 'solar-settings' }, flowEditor = { name: 'solar-size-strings' }
    const families = [{ family_id: 'family' }], retained = { spacing: 12 }
    const runs = { [flowEditor.name]: { ok: false, code: 'fixture-failure' } }
    const inputs = {
      projectMode: false, projectSolar: { ready: true }, running: false, routeErr: null,
      ENV_CAD_EDIT: true, ENV_SOLAR_SETTINGS_FORM: true, ENV_SOLAR_FLOW_RAIL: false,
      drafting: true, surfaceSlots: { toolbar: { profile: 'solar' } }, mock: false,
      solarFormTool: form, solarFlowEditor: flowEditor, catalogRunContext: context,
      solarSettingsFormChoice, solarSettingsRunFeedback, SOLAR_SETTINGS_LOADERS: loaders,
      solarSettingsFormRef: { current: null }, heldByUs: { held: true }, settingsRunResult: null,
      settingsRunRef: { current: null }, onRequestCatalogRun: vi.fn(() => ({ runIntent: { intentId: 'intent' } })),
      RIBBON_RATIONALE: 'Ribbon', setSettingsRunResult: vi.fn(), setSolarFormTool: vi.fn(),
      catalog: { families }, solarFlowFamiliesDrawingId: 'families-drawing', canOpenSolarSettingsForm,
      solarFlowPending: 'other-tool', solarFlowRuns: { drawingId: context.drawingId, runs },
      solarFlowRunsFor, solarFlowRunStatus, onOpenSolarFlowStep: vi.fn(), onSolarFlowChange: vi.fn(),
      solarFlow: { flow: 'layout' }, checkoutCapabilityRef: { current: vi.fn(() => 'capability') },
      refuseProjectSolar: vi.fn(() => false), SOLAR_SETTINGS_TOOL_NAME: 'solar-settings',
      loadCatalog: vi.fn(), seatCompletedVersion: vi.fn(), onRunCombinerPlacement: vi.fn(), catalogDigestOf: vi.fn(),
      solarFlowRetainedRef: { current: new Map([[flowEditor.name, { drawingId: context.drawingId, values: retained }]]) },
      onSubmitSolarFlowStep: vi.fn(), onCloseSolarFlowStep: vi.fn(), projectSolarAppReason,
    }
    const reset = () => {
      workspaceProbe.mockClear()
      Object.values(components).forEach((probe) => probe.mockClear())
      Object.values(loaders).forEach((loader) => loader.mockClear())
    }
    // Scope readiness is owned by the workspace; App owns the complete host fence.
    for (const ready of [true, false]) {
      for (const running of [true, false]) {
        for (const routeErr of [null, projectSolarAppReason('project_unavailable')]) {
          for (const profile of ['solar', 'cad']) {
            for (const ENV_CAD_EDIT of [true, false]) {
              for (const ENV_SOLAR_SETTINGS_FORM of [true, false]) {
                for (const ENV_SOLAR_FLOW_RAIL of [true, false]) {
                  reset()
                  const solarFlowSeated = ENV_CAD_EDIT && ENV_SOLAR_SETTINGS_FORM && ENV_SOLAR_FLOW_RAIL && profile === 'solar'
                  const variant = { ...inputs, projectMode: true, solarFlowSeated, projectSolar: { ready, scope: { authorityKey: 'test', binding: null } },
                    projectSolarWorkspaceRef: { current: null }, phoneViewport: false, catalogState: {}, projectSolarRuntime: {},
                    mintCorrelationId: vi.fn(), onOpenProjectSolarTool: vi.fn(), onCloseProjectSolarTool: vi.fn(), running, routeErr,
                    surfaceSlots: { toolbar: { profile } }, ENV_CAD_EDIT, ENV_SOLAR_SETTINGS_FORM, ENV_SOLAR_FLOW_RAIL }
                  const view = render(React.createElement(React.Fragment, null,
                    React.createElement('p', { role: 'status' }, routeErr),
                    React.createElement('section', { 'data-testid': 'project-host-branch' }, element(branch, variant, projectComponents))))
                  const subtree = view.getByTestId('project-host-branch')
                  const statuses = subtree.querySelectorAll('p[role="status"]')
                  expect(statuses).toHaveLength(solarFlowSeated ? 0 : 1)
                  if (solarFlowSeated) {
                    expect(workspaceProbe).toHaveBeenCalledOnce()
                    expect(subtree.querySelector('[data-testid="project-workspace"]')).not.toBeNull()
                  } else {
                    expect(workspaceProbe).not.toHaveBeenCalled()
                    expect(statuses[0].textContent).toBe(projectSolarAppReason('project_unavailable'))
                    expect(statuses[0].getAttribute('aria-live')).toBe('polite')
                  }
                  Object.values(components).forEach((probe) => expect(probe).not.toHaveBeenCalled())
                  Object.values(loaders).forEach((loader) => expect(loader).not.toHaveBeenCalled())
                  view.unmount()
                }
              }
            }
          }
        }
      }
    }
    expect(text(branch.test)).toBe('projectMode')
    for (const [name, expected] of [['solar-settings', 'SolarSettingsForm'], ['solar-size-strings', 'SolarStepEditor'], ['solar-other', 'SolarToolForm']]) {
      reset()
      const tool = { name }
      const view = render(element(branch, { ...inputs, running: true, solarFormTool: tool }, components))
      expect(view.queryByRole('status')).toBeNull()
      expect(components[expected]).toHaveBeenCalledOnce()
      for (const [component, probe] of Object.entries(components)) if (component !== expected) expect(probe).not.toHaveBeenCalled()
      const props = components[expected].mock.calls[0][0]
      expect(props.readIntake).toBe(loaders.readIntake)
      if (expected === 'SolarSettingsForm') {
        expect(props.context).toBe(context)
        expect(props.readVersions).toBe(loaders.readVersions)
        expect(props.checkoutHeld).toBe(inputs.heldByUs)
        expect(props.busy).toBe(true)
        expect(props.runMessage).toBeNull()
        const params = { params: 'typed' }
        props.onSubmit(params)
        expect(inputs.onRequestCatalogRun).toHaveBeenLastCalledWith(tool, params, 'Ribbon', 'ribbon', { complete: true })
        expect(inputs.settingsRunRef.current).toEqual({ intentId: 'intent', drawingId: context.drawingId, drawingVersion: context.drawingVersion })
        props.onClose()
        expect(inputs.settingsRunRef.current).toBeNull()
        expect(inputs.setSettingsRunResult).toHaveBeenLastCalledWith(null)
      } else {
        expect(props[expected === 'SolarStepEditor' ? 'row' : 'tool']).toBe(tool)
        expect(props.drawingId).toBe(context.drawingId)
        expect(props.drawingVersion).toBe(context.drawingVersion)
        if (expected === 'SolarStepEditor') expect(props.projectId).toBeNull()
        const params = { params: 'editor' }
        props.onSubmit(tool, params)
        expect(inputs.onRequestCatalogRun).toHaveBeenLastCalledWith(tool, params, 'Ribbon', 'ribbon', { complete: true })
        props.onClose()
      }
      expect(inputs.setSolarFormTool).toHaveBeenLastCalledWith(null)
      expect(loaders.readIntake).toHaveBeenCalledOnce()
      expect(loaders.readVersions.mock.calls.length).toBe(expected === 'SolarSettingsForm' ? 1 : 0)
      view.unmount()
    }
    for (const mock of [false, true]) {
      reset()
      const view = render(element(branch, { ...inputs, mock, running: true, ENV_SOLAR_FLOW_RAIL: true, solarFormTool: null }, components))
      expect(components.SolarFlowRail).toHaveBeenCalledOnce()
      expect(components.SolarStepEditor).toHaveBeenCalledOnce()
      expect(components.SolarSettingsForm).not.toHaveBeenCalled()
      expect(components.SolarToolForm).not.toHaveBeenCalled()
      const rail = components.SolarFlowRail.mock.calls[0][0]
      expect(rail.families).toBe(families)
      expect(rail.familiesDrawingId).toBe(inputs.solarFlowFamiliesDrawingId)
      expect(rail.workspacePanelsByFlow).toBe(mock ? undefined : registry)
      expect(rail.drawingId).toBe(context.drawingId)
      expect(rail.openSettingsForm).toBe(mock ? undefined : canOpenSolarSettingsForm)
      expect(rail.pendingTool).toBe(inputs.solarFlowPending)
      expect(rail.runs).toBe(runs)
      expect(rail.openName).toBe(flowEditor.name)
      expect(rail.onOpenStep).toBe(inputs.onOpenSolarFlowStep)
      expect(rail.onFlowChange).toBe(inputs.onSolarFlowChange)
      const editor = components.SolarStepEditor.mock.calls[0][0]
      expect(editor.row).toBe(flowEditor)
      expect(editor.drawingId).toBe(context.drawingId)
      expect(editor.drawingVersion).toBe(context.drawingVersion)
      expect(editor.projectId).toBeNull()
      expect(editor.readIntake).toBe(loaders.readIntake)
      expect(editor.retained).toBe(retained)
      expect(editor.status).toBe('failed')
      expect(editor.failureCode).toBe('fixture-failure')
      expect(editor.onSubmit).toBe(inputs.onSubmitSolarFlowStep)
      expect(editor.onClose).toBe(inputs.onCloseSolarFlowStep)
      expect(components.SolarWorkspaceTools.mock.calls.length).toBe(mock ? 0 : 1)
      if (!mock) {
        const workspace = components.SolarWorkspaceTools.mock.calls[0][0]
        for (const [key, value] of Object.entries({ drawingId: context.drawingId, projectId: null,
          drawingVersion: context.drawingVersion, flow: inputs.solarFlow, checkoutHeld: inputs.heldByUs,
          busy: true, onRunPlacement: inputs.onRunCombinerPlacement, catalogDigestOf: inputs.catalogDigestOf })) expect(workspace[key]).toBe(value)
        expect(workspace.getCheckoutCapability()).toBe('capability')
        expect(inputs.checkoutCapabilityRef.current).toHaveBeenCalledOnce()
        workspace.onPhysicalHeadChanged()
        expect(inputs.loadCatalog).toHaveBeenCalledOnce()
        const options = { announce: false }
        workspace.onDrawingVersionChanged(context, runs, options)
        expect(inputs.seatCompletedVersion).toHaveBeenCalledExactlyOnceWith(context, runs, options)
      }
      view.unmount()
    }
    for (const overrides of [{ ENV_CAD_EDIT: false }, { drafting: false }, { surfaceSlots: { toolbar: { profile: 'cad' } } }]) {
      reset()
      const view = render(element(branch, { ...inputs, ENV_SOLAR_FLOW_RAIL: true, ...overrides }, components))
      expect(view.container.textContent).toBe('')
      Object.values(components).forEach((probe) => expect(probe).not.toHaveBeenCalled())
      Object.values(loaders).forEach((loader) => expect(loader).not.toHaveBeenCalled())
      view.unmount()
    }
    reset()
    const genericView = render(element(branch, { ...inputs, ENV_SOLAR_SETTINGS_FORM: false, ENV_SOLAR_FLOW_RAIL: true }, components))
    expect(components.SolarToolForm).toHaveBeenCalledOnce()
    expect(components.SolarFlowRail).not.toHaveBeenCalled()
    expect(components.SolarWorkspaceTools).not.toHaveBeenCalled()
    expect(components.SolarStepEditor).not.toHaveBeenCalled()
    genericView.unmount()
  })

  it('R13F-08 App mounts canonical checkout controls only for a ready project and uses sanitized eligibility', async () => {
    const components = nodes((node) => node.type === 'FunctionDeclaration' && node.id.name === 'ProjectSolarCheckoutControls')
    expect(components).toHaveLength(1)
    const projectHook = vi.fn((client) => useProjectCheckoutController(client))
    const Controls = Function('React', 'useEffect', 'useProjectCheckoutController', 'projectSolarAppReason',
      `${text(components[0].node)}; return ProjectSolarCheckoutControls`)(
      React, React.useEffect, projectHook, projectSolarAppReason)
    const branches = nodes((node) => node.type === 'LogicalExpression' && node.operator === '&&'
      && text(node.left) === '!mock' && text(node.right).includes('<CheckoutControls'))
    expect(branches).toHaveLength(1)
    const standalone = vi.fn(() => React.createElement('div', { 'data-testid': 'standalone-checkout' }))
    let controlProps
    let handlers
    function ProjectProbe(props) {
      controlProps = props
      // Call the extracted production component with real React hooks; keep
      // its element's callbacks to exercise both disabled and retained handlers.
      const element = Controls(props)
      handlers = Object.fromEntries(element.props.children.filter((child) => child.type === 'button')
        .map((child) => [child.props.children, child.props.onClick]))
      return element
    }
    const legacyActions = { take: vi.fn(), release: vi.fn(), refresh: vi.fn() }
    const lock = { staleByOther: { stale: true }, legacyByOther: { legacy: true },
      canTake: true, unknown: { unknown: true } }
    const legacy = { actions: legacyActions, readFailed: { failed: true },
      failure: { code: 'legacy failure' }, busy: { busy: true } }
    const legacyInputs = { lock, checkout: legacy, otherHeldCheckout: { holder: 'other' },
      heldByUs: { held: true }, projectSolarAppReason }
    const renderBranch = (inputs) => element(branches[0].node, { ...legacyInputs, ...inputs },
      { ProjectSolarCheckoutControls: ProjectProbe, CheckoutControls: standalone })
    const variants = [
      { name: 'available', fields: { checkout: null, ownsCheckout: false }, take: true, release: false, retry: true, reason: 'checkout_required' },
      { name: 'owned', fields: { ownsCheckout: true }, take: false, release: true, retry: true, reason: null },
      { name: 'foreign', fields: { ownsCheckout: false }, take: false, release: false, retry: true, reason: 'checkout_held' },
      { name: 'expired', expired: true, fields: { ownsCheckout: true }, take: true, release: false, retry: true, reason: 'checkout_expired' },
      { name: 'pending', fields: { ownsCheckout: true, pending: true }, take: false, release: false, retry: false, reason: 'checkout_pending' },
      { name: 'unavailable', fields: { error: 'fixture_error' }, take: false, release: false, retry: true, reason: 'checkout_unavailable' },
    ]
    for (const variant of variants) {
      const run = setup()
      const now = Date.now()
      const lease = { holder: 'fixture holder', fence: '1',
        acquired: new Date(now - 3600000).toISOString(), expires: new Date(now + 3600000).toISOString() }
      let serverLease = lease
      run.fetchImpl.mockImplementation(async (url, options = {}) => {
        if (String(url).includes('/api/capabilities?')) return response(catalogData())
        const data = { organization_id: O, project_id: P, drawing_id: D }
        if (options.method === 'DELETE') {
          serverLease = null
          return response({ ...data, released: true, checkout: null })
        }
        if (options.method === 'POST') {
          serverLease = lease
          return response({ ...data, acquired: true, checkout: lease,
            checkout_capability: 'lco1.' + 'a'.repeat(64) })
        }
        return response({ ...data, checkout: serverLease })
      })
      const hook = mount(run)
      await receipt(hook)
      const client = hook.result.current.projectSolar.projectClient
      const release = vi.spyOn(client, 'releaseCheckout')
      await act(async () => { await client.acquireCheckout({ holder: 'fixture holder' }) })
      expect(client.getSnapshot().ownsCheckout).toBe(true)
      const owner = hook.result.current.projectSolar
      const projectedLease = variant.expired ? { ...lease, expires: new Date(now - 1).toISOString() } : lease
      const projection = projectSolarCheckoutState({ scope: owner.scope, now,
        snapshot: projectSolarScopeSnapshot({ ...client.getSnapshot(), checkout: projectedLease, ...variant.fields }) })
      expect(projection).toMatchObject({ canTake: variant.take, canRelease: variant.release, reason: variant.reason })
      if (variant.name === 'foreign') {
        expect(projection.checkout.holder).toBe(client.getSnapshot().checkout.holder)
        expect(projection.ownsCheckout).toBe(false)
      }
      client.readCheckout.mockClear()
      client.acquireCheckout.mockClear()
      projectHook.mockClear()
      const inputs = () => ({ mock: false, projectMode: true,
        projectSolar: { ...hook.result.current.projectSolar, checkout: projection } })
      const view = render(renderBranch(inputs()))
      await act(async () => {})
      expect(client.readCheckout).toHaveBeenCalledExactlyOnceWith(undefined)
      expect(controlProps.projectClient).toBe(client)
      expect(controlProps.checkout).toBe(projection)
      expect(projectHook.mock.calls.length).toBeGreaterThan(0)
      expect(projectHook.mock.calls.every(([value]) => value === client)).toBe(true)
      expect(view.getByRole('group', { name: 'Project checkout' })).toBeTruthy()
      expect(view.getByRole('status').textContent).toBe(projectSolarAppReason(variant.reason) || '')
      expect(standalone).not.toHaveBeenCalled()
      const event = { type: 'click', currentTarget: { sentinel: 'event' } }
      for (const [label, action, enabled] of [
        ['Take', client.acquireCheckout, variant.take],
        ['Release', release, variant.release],
        ['Retry', client.readCheckout, variant.retry],
      ]) {
        view.rerender(renderBranch(inputs()))
        expect(controlProps.isCurrent).toBe(hook.result.current.projectSolar.isCurrent)
        expect(controlProps.isCurrent()).toBe(true)
        expect(view.getByRole('button', { name: label }).disabled).toBe(!enabled)
        const counts = [client.acquireCheckout.mock.calls.length, release.mock.calls.length, client.readCheckout.mock.calls.length]
        const before = action.mock.calls.length
        await act(async () => { await handlers[label](event) })
        expect(action.mock.calls.length, variant.name + ' ' + label).toBe(before + (enabled ? 1 : 0))
        if (enabled) expect(action.mock.calls.at(-1)).toEqual([undefined])
        const after = [client.acquireCheckout.mock.calls.length, release.mock.calls.length, client.readCheckout.mock.calls.length]
        const selected = label === 'Take' ? 0 : label === 'Release' ? 1 : 2
        expect(after).toEqual(counts.map((count, index) => count + (index === selected && enabled ? 1 : 0)))
      }
      view.rerender(renderBranch(inputs()))
      const retained = { ...handlers }
      act(() => { client.setScope(null, null) })
      const requests = run.fetchImpl.mock.calls.length
      const counts = [client.acquireCheckout.mock.calls.length, release.mock.calls.length, client.readCheckout.mock.calls.length]
      await act(async () => {
        await retained.Take(event)
        await retained.Release(event)
        await retained.Retry(event)
      })
      expect(run.fetchImpl.mock.calls.length).toBe(requests)
      expect([client.acquireCheckout.mock.calls.length, release.mock.calls.length, client.readCheckout.mock.calls.length]).toEqual(counts)
      expect(legacyActions.take).not.toHaveBeenCalled()
      expect(legacyActions.release).not.toHaveBeenCalled()
      expect(legacyActions.refresh).not.toHaveBeenCalled()
      expect(run.legacyFetch).not.toHaveBeenCalled()
      view.unmount()
      hook.unmount()
      release.mockRestore()
    }

    const run = setup({ canonicalVersionId: null })
    const hook = mount(run)
    expect(hook.result.current.projectSolar.ready).toBe(false)
    const reads = run.fetchImpl.mock.calls.length
    for (const reason of ['sign_in_required', 'binding_invalid', null, undefined]) {
      projectHook.mockClear()
      const view = render(renderBranch({ mock: false, projectMode: true,
        projectSolar: { ...hook.result.current.projectSolar, scope: { ...hook.result.current.projectSolar.scope, reason } } }))
      expect(view.getByRole('status').textContent).toBe(projectSolarAppReason(reason || 'scope_pending'))
      expect(view.queryByRole('group', { name: 'Project checkout' })).toBeNull()
      expect(projectHook).not.toHaveBeenCalled()
      expect(standalone).not.toHaveBeenCalled()
      expect(run.fetchImpl.mock.calls.length).toBe(reads)
      view.unmount()
    }
    hook.unmount()
    projectHook.mockClear()
    const view = render(renderBranch({ mock: false, projectMode: false, projectSolar: {} }))
    expect(standalone).toHaveBeenCalledOnce()
    const props = standalone.mock.calls[0][0]
    const expected = { lockedByOther: legacyInputs.otherHeldCheckout, staleByOther: lock.staleByOther,
      legacyByOther: lock.legacyByOther, canTake: lock.canTake, heldByUs: legacyInputs.heldByUs,
      unknown: lock.unknown, readFailed: legacy.readFailed, failure: legacy.failure, busy: legacy.busy,
      onTake: legacyActions.take, onRelease: legacyActions.release, onRetry: legacyActions.refresh }
    expect(props).toEqual(expected)
    for (const key of Object.keys(expected)) expect(props[key]).toBe(expected[key])
    expect(projectHook).not.toHaveBeenCalled()
    view.unmount()
    standalone.mockClear()
    for (const projectMode of [true, false]) {
      const mockView = render(renderBranch({ mock: true, projectMode, projectSolar: { ready: true } }))
      expect(mockView.container.textContent).toBe('')
      expect(projectHook).not.toHaveBeenCalled()
      expect(standalone).not.toHaveBeenCalled()
      mockView.unmount()
    }
  })

  it('R13F-02 StrictMode gives catalog and checkout one live owner and disposes every subscription', async () => {
    const run = setup()
    const observations = []
    let checkout
    function CheckoutProbe({ client }) {
      checkout = { client, ...useProjectCheckoutController(client) }
      return null
    }
    function Probe() {
      const value = useAppScope(run.input, run.services)
      observations.push(value)
      return value.projectSolar.ready ? <CheckoutProbe client={value.projectSolar.projectClient} /> : null
    }
    const view = render(<StrictMode><Probe /></StrictMode>)
    await waitFor(() => expect(observations.at(-1).catalogState.projectCatalog).toBeTruthy())
    expect(run.clients).toHaveLength(2)
    const [retired, live] = run.clients
    expect(retired.dispose).toHaveBeenCalledOnce()
    expect(retired.listeners.size).toBe(0)
    expect(observations.at(-1).projectSolar.projectClient).toBe(live)
    expect(observations.at(-1).catalogServices.projectSolarClient).toBe(live)
    expect(observations.at(-1).catalogState.projectCatalog.client).toBe(live)
    expect(checkout.client).toBe(live)
    expect(checkout.state.binding).toEqual(binding)
    await act(async () => { await checkout.actions.refresh() })
    expect(live.readCheckout).toHaveBeenCalledOnce()
    expect(retired.readCheckout).not.toHaveBeenCalled()
    const count = observations.length
    expect(retired.listeners.size).toBe(0)
    act(() => { retired.listeners.forEach((publish) => publish()) })
    expect(observations).toHaveLength(count)
    const capture = observations.at(-1).projectSolar.isCurrent
    view.unmount()
    for (const client of run.clients) {
      expect(client.dispose).toHaveBeenCalledOnce()
      expect(client.listeners.size).toBe(0)
      client.unsubscribes.forEach((stop) => expect(stop).toHaveBeenCalledOnce())
      expect(client.getSnapshot().binding).toBeNull()
    }
    const finalCount = observations.length
    act(() => { live.retained.forEach((publish) => publish()) })
    expect(observations).toHaveLength(finalCount)
    expect(capture()).toBe(false)
    expect(run.legacyFetch).not.toHaveBeenCalled()
  })

  it('R13F-03 sign-out clears scope before logout and actor or generation changes rotate authority', async () => {
    const logout = deferred()
    let scopeAtLogout
    const run = setup({}, vi.fn(() => {
      scopeAtLogout = run.clients[0].getSnapshot()
      return logout.promise
    }))
    const hook = mount(run)
    await receipt(hook)
    const first = hook.result.current.projectSolar
    expect(hook.result.current.projectSolarSession.actorId).toBe(composerDraftAccountScope())
    let leaving
    act(() => { leaving = run.controller.actions.signOut() })
    expect(scopeAtLogout).toMatchObject({ binding: null, authorityKey: null })
    expect(first.isCurrent()).toBe(false)
    expect(hook.result.current.projectSolar.ready).toBe(false)
    await act(async () => { logout.resolve(); await leaving })
    account()
    // Explicit sign-out latches this controller. A new authenticated session
    // supplies a fresh controller, as a remount after login would.
    const reauthenticated = createSessionController({ storage: localStorage })
    reauthenticated.actions.activate({ tenant: 'tenant' })
    const input = { ...run.input, controller: reauthenticated }
    hook.rerender(input)
    expect(hook.result.current.projectSolar.ready).toBe(true)
    expect(hook.result.current.projectSolar.scope.authorityKey).not.toBe(first.scope.authorityKey)
    const sameActor = hook.result.current.projectSolar
    const generationSnapshots = []
    const stop = reauthenticated.subscribe(() => generationSnapshots.push(run.clients[0].getSnapshot()))
    act(() => { reauthenticated.actions.activate({ tenant: 'tenant' }) })
    stop()
    expect(generationSnapshots[0]).toMatchObject({ binding: null, authorityKey: null })
    expect(sameActor.isCurrent()).toBe(false)
    expect(hook.result.current.projectSolar.scope.authorityKey).not.toBe(sameActor.scope.authorityKey)
    const previous = hook.result.current.projectSolar
    account('bob')
    hook.rerender({ ...input })
    expect(previous.isCurrent()).toBe(false)
    expect(hook.result.current.projectSolar.scope.authorityKey).not.toBe(previous.scope.authorityKey)
    expect(hook.result.current.projectSolarSession.generation).toBe(reauthenticated.getSnapshot().generation)
    for (const subject of [null, 'invalid']) {
      account(subject)
      hook.rerender({ ...input })
      expect(hook.result.current.projectSolarSession.actorId).toBeNull()
      expect(hook.result.current.projectSolar.ready).toBe(false)
      expect(run.clients[0].getSnapshot()).toMatchObject({ binding: null, authorityKey: null })
    }
    account()
    run.services.getCapabilities.mockResolvedValue(catalogData('mock'))
    hook.rerender({ ...input, mock: true })
    expect(hook.result.current.projectMode).toBe(false)
    expect(hook.result.current.projectSolarSession.authenticated).toBe(false)
    expect(hook.result.current.projectSolar.ready).toBe(false)
    hook.unmount()
  })

  it('R13F-04 an unready project starts with a null sentinel then requests its actual owner', async () => {
    const run = setup({ canonicalVersionId: null, ENV_SOLAR_SETTINGS_FORM: false,
      surfaceSlots: { toolbar: { profile: 'cad' } } })
    const seen = []
    const hook = renderHook((input) => {
      const value = useAppScope(input, run.services)
      seen.push(value)
      return value
    }, { initialProps: run.input })
    expect(seen[0].initialContext).toMatchObject({ solarBinding: null, authorityKey: null, projectIntents: [] })
    expect(seen[0].initialContext.projectIntents).toBe(intents)
    expect(hook.result.current.projectMode).toBe(true)
    expect(hook.result.current.catalogRunContext).toBeNull()
    expect(hook.result.current.projectSolar.ready).toBe(false)
    await act(async () => {})
    expect(run.services.getCapabilities).not.toHaveBeenCalled()
    expect(run.fetchImpl).not.toHaveBeenCalled()
    expect(run.legacyFetch).not.toHaveBeenCalled()
    const stableServices = hook.result.current.catalogServices
    hook.rerender({ ...run.input, canonicalVersionId: V })
    const accepted = await receipt(hook)
    expect(hook.result.current.catalogServices).toBe(stableServices)
    const owner = hook.result.current.projectSolar
    expect(owner.ready).toBe(true)
    expect(run.services.getCapabilities).toHaveBeenCalledWith(false, {
      solarBinding: binding, authorityKey: owner.scope.authorityKey, projectIntents: intents,
    }, { projectClient: owner.projectClient, signal: expect.any(AbortSignal) })
    expect(accepted.client).toBe(run.clients[0])
    expect(accepted.binding).toEqual(binding)
    expect(accepted.authorityKey).toBe(owner.scope.authorityKey)
    expect(Object.isFrozen(intents)).toBe(true)
    expect(run.fetchImpl.mock.calls.every(([url]) => String(url).includes(`input_version_id=${V}`))).toBe(true)
    expect(run.legacyFetch).not.toHaveBeenCalled()
  })

  it('R13F-21 an entitlement change keeps a ready project catalog bound', async () => {
    const run = setup()
    const hook = mount(run)
    await receipt(hook)
    hook.rerender({ ...run.input, entitlements: { plan: 'pro' } })
    await act(async () => {})
    await waitFor(() => {
      expect(hook.result.current.projectSolar.ready).toBe(true)
      expect(hook.result.current.catalogState.projectCatalog).toBeTruthy()
      expect(run.legacyFetch).not.toHaveBeenCalled()
    })
  })

  it('R13F-22 leaving a project restores standalone scope in one write and never requests the previous drawing', async () => {
    const run = setup({ openProjectId: null, drawingState: { drawing_id: 'A', version: 1 } })
    const hook = mount(run)
    await waitFor(() => expect(hook.result.current.catalogState.catalog.source).toBe('endpoint'))
    await act(async () => {})
    expect(run.legacyFetch.mock.calls.some(([url]) => {
      const query = new URL(String(url), 'http://localhost').searchParams
      return query.get('drawing_id') === 'A' && query.get('drawing_version') === '1'
    })).toBe(true)
    hook.rerender({ ...run.input, openProjectId: P, drawingState: null })
    await receipt(hook)
    const closedAt = run.legacyFetch.mock.calls.length
    hook.rerender({ ...run.input, drawingState: { drawing_id: 'C', version: 2 } })
    await act(async () => {})
    await waitFor(() => expect(hook.result.current.catalogState.catalog.source).toBe('endpoint'))
    const requests = run.legacyFetch.mock.calls.slice(closedAt)
    expect(requests).toHaveLength(1)
    const request = new URL(String(requests[0][0]), 'http://localhost')
    expect(request.pathname).toBe('/api/capabilities')
    expect(request.searchParams.get('drawing_id')).toBe('C')
    expect(request.searchParams.get('drawing_version')).toBe('2')
    expect(requests.some(([url]) => {
      const query = new URL(String(url), 'http://localhost').searchParams
      return query.get('drawing_id') === 'A' && query.get('drawing_version') === '1'
    })).toBe(false)
  })

  it('R13F-23 the catalog context effect is one top-level statement of App beside the catalog hook', () => {
    const matches = nodes((node) => node.type === 'CallExpression' && node.callee.name === 'useEffect'
      && text(node.arguments[0]).includes('...(ENV_SOLAR_SETTINGS_FORM ? solarSettingsScope({'))
    expect(matches).toHaveLength(1)
    expect(matches[0].node).toBe(contextEffect)
    expect(matches[0].parent.type).toBe('ExpressionStatement')
    const blocks = nodes((node) => node.type === 'BlockStatement' && node.body.includes(catalogDeclaration))
    expect(blocks).toHaveLength(1)
    expect(blocks[0].node.body.includes(matches[0].parent)).toBe(true)
  })

  it('R13F-05 scope switches and lease rotation reject deferred receipts and expire accepted captures', async () => {
    const run = setup()
    const hook = mount(run)
    const original = await receipt(hook)
    expect(original).toMatchObject({ client: run.clients[0], binding, authorityKey: hook.result.current.projectSolar.scope.authorityKey })
    expect(original.families).toBe(hook.result.current.catalogState.catalog.families)
    expect(original.isCurrent()).toBe(true)
    const oldResponse = deferred()
    run.services.getCapabilities.mockImplementationOnce(() => oldResponse.promise)
    let pending
    act(() => { pending = hook.result.current.catalogActions.loadCatalog() })
    hook.rerender({ ...run.input, canonicalVersionId: V2 })
    const next = await receipt(hook)
    expect(original.isCurrent()).toBe(false)
    expect(next.client).toBe(run.clients[0])
    expect(next.binding).toEqual({ ...binding, input_version_id: V2 })
    expect(next.authorityKey).toBe(original.authorityKey)
    await act(async () => { oldResponse.resolve(catalogData('obsolete-version')); expect(await pending).toBeUndefined() })
    expect(hook.result.current.catalogState.projectCatalog).toBe(next)
    const leaseResponse = deferred()
    run.services.getCapabilities.mockImplementationOnce(() => leaseResponse.promise)
    act(() => { pending = hook.result.current.catalogActions.loadCatalog() })
    await act(async () => { await run.clients[0].acquireCheckout({ holder: 'fixture holder' }) })
    expect(next.isCurrent()).toBe(false)
    await act(async () => { leaseResponse.resolve(catalogData('obsolete-lease')); expect(await pending).toBeUndefined() })
    expect(hook.result.current.catalogState.projectCatalog).toBeNull()
    await act(async () => { await hook.result.current.catalogActions.loadCatalog() })
    const current = hook.result.current.catalogState.projectCatalog
    expect(current.client).toBe(run.clients[0])
    expect(current.binding).toEqual(next.binding)
    expect(current.authorityKey).toBe(next.authorityKey)
    expect(current.families).toBe(hook.result.current.catalogState.catalog.families)
    expect(current.families[0].family_id).toBe('original')
    expect(current.isCurrent()).toBe(true)
    expect(Object.isFrozen(current)).toBe(true)
    hook.unmount()
    expect(current.isCurrent()).toBe(false)
    expect(run.legacyFetch).not.toHaveBeenCalled()
  })
})
